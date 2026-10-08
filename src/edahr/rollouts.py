"""Counterfactual rollout harness for attribution-risk-aware policies.

For every (query, candidate-group) pair we materialise the three branches of
the action space and execute each one through the *same* retriever, reranker,
generator and verifier:

    KEEP     keep the retrieved children;
    EXPAND   replace them by their shared parent (one level up);
    SECTION  replace them by their section (two levels up).

Each branch is scored with the attribution-risk reward::

    R = w_e * evidence_coverage - lambda * AR(S) - beta * tokens_norm
        - gamma * latency_norm          [+ w_a * answer_quality when gold exists]

The branch rewards yield binary oracle labels per level step
(``EXPAND parent over KEEP`` / ``EXPAND section over KEEP``) which
:mod:`edahr.training` turns into a TorchScript policy consumed by
:class:`edahr.policy.AdaptiveMergePolicy(checkpoint=...)`.

Gold answers and leaf evidence are carried per question record, avoiding
collisions when different papers contain identical question text.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Sequence

from .attribution import attribution_metrics
from .config import Settings
from .context import assemble_context
from .evaluation import answer_token_f1
from .interfaces import scoped_search
from .pipeline import AdaptiveHierarchicalPipeline, classify_query
from .policy import features_for_candidate, members_at_level
from .schemas import Level, QueryType
from .verification import verify_generation


def _set_f1(predicted: set[str], reference: set[str]) -> float:
    if not predicted and not reference:
        return 1.0
    overlap = len(predicted & reference)
    if not overlap:
        return 0.0
    precision = overlap / len(predicted)
    recall = overlap / len(reference)
    return 2 * precision * recall / (precision + recall)


@dataclass
class RewardWeights:
    answer_quality: float = 0.50
    # v5 attribution-aware terms (Rescue/HarmfulDrift/Citation P&R)
    citation_recall_w: float = 0.5
    citation_precision_w: float = 0.7
    rescue_w: float = 0.5
    harmful_lambda: float = 1.0
    ambiguity_lambda: float = 0.3
    empty_evidence_lambda: float = 0.40
    precision_epsilon: float = 0.02   # constraint: no worse than KEEP by this
    harmful_delta: float = 0.05       # constraint: harmful rate ceiling
    # legacy pilot terms
    evidence_recall: float = 0.60
    citation_quality: float = 0.60
    attribution_risk_lambda: float = 0.80
    tokens_beta: float = 0.30
    latency_gamma: float = 0.05
    label_margin_tau: float = 0.02


@dataclass
class RolloutRow:
    query: str
    question_id: str
    source: str | None
    citation_evaluable: bool
    query_type: str
    parent_id: str
    section_id: str
    features: list[float] = field(default_factory=list)
    members: dict[str, float] = field(default_factory=dict)
    branches: dict[str, dict] = field(default_factory=dict)
    label_parent: int = 0
    label_section: int = 0

    def to_dict(self) -> dict:
        return {
            "query": self.query,
            "question_id": self.question_id,
            "source": self.source,
            "citation_evaluable": self.citation_evaluable,
            "query_type": self.query_type,
            "parent_id": self.parent_id,
            "section_id": self.section_id,
            "features": self.features,
            "members": self.members,
            "branches": self.branches,
            "label_parent": self.label_parent,
            "label_section": self.label_section,
        }


class RolloutRunner:
    """Executes counterfactual branches and emits labelled training rows."""

    def __init__(
        self,
        pipeline: AdaptiveHierarchicalPipeline,
        settings: Settings | None = None,
        weights: RewardWeights | None = None,
        max_groups_per_query: int = 4,
        gold_answers: Mapping[str, float | str] | None = None,
        samples: int = 1,
        gold_child_map: Mapping[str, Sequence[str]] | None = None,
    ):
        self.pipeline = pipeline
        self.hierarchy = pipeline.hierarchy
        self.settings = settings or pipeline.settings
        self.weights = weights or RewardWeights()
        self.max_groups_per_query = max_groups_per_query
        self.samples = max(1, samples)
        self.gold_child_map: dict[str, list[str]] = {
            query: [str(v) for v in values]
            for query, values in (gold_child_map or {}).items()
        }
        self.gold_answers: dict[str, str] = {
            query: str(value)
            for query, value in (gold_answers or {}).items()
        }

    # ------------------------------------------------------------------

    def run(self, queries: Sequence[str], out_path: str | Path) -> list[dict]:
        return self.run_pairs([(query, None) for query in queries], out_path)

    def run_pairs(
        self, pairs: Sequence[tuple[str, str | None]], out_path: str | Path
    ) -> list[dict]:
        """Run rollouts for ``(query, source_filter)`` pairs.

        ``source_filter`` restricts retrieval to a single paper, matching the
        one-document-at-a-time scope of the method.
        """
        records = [
            {"query": query, "source": source, "question_id": ""}
            for query, source in pairs
        ]
        return self.run_records(records, out_path)

    def run_records(
        self, records: Sequence[Mapping], out_path: str | Path, *, append: bool = False
    ) -> list[dict]:
        """Run records and flush each completed question for crash-safe resume."""
        rows: list[dict] = []
        out = Path(out_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        mode = "a" if append else "w"
        with out.open(mode, encoding="utf-8") as handle:
            for record in records:
                query = str(record["query"])
                source = record.get("source")
                references = record.get("reference_answers") or [
                    record.get("answer") or record.get("gold_answer") or ""
                ]
                fresh = self.run_query(
                    query, str(source) if source is not None else None,
                    question_id=str(record.get("question_id") or ""),
                    gold_answers=[str(value) for value in references if str(value).strip()],
                    gold_children=record.get("gold_child_ids") or record.get("gold") or (),
                    gold_child_sets=record.get("reference_child_sets") or (),
                )
                for row in fresh:
                    handle.write(json.dumps(row, ensure_ascii=False) + "\n")
                handle.flush()
                rows.extend(fresh)
                print(f"[rollouts] {len(rows)} new rows after {query[:48]!r}", flush=True)
        return rows

    def run_query(
        self,
        query: str,
        source: str | None = None,
        *,
        question_id: str = "",
        gold_answers: Sequence[str] | None = None,
        gold_children: Sequence[str] | None = None,
        gold_child_sets: Sequence[Sequence[str]] | None = None,
    ) -> list[dict]:
        hierarchy = self.hierarchy
        settings = self.settings

        query_type = classify_query(query)
        initial = scoped_search(
            self.pipeline.retriever,
            hierarchy,
            query,
            settings.candidate_k,
            source,
        )
        pool = initial[: settings.rerank_k]
        if pool:
            scores = self.pipeline.reranker.score(
                query, [hierarchy.node(hit.node_id).text for hit in pool]
            )
        else:
            scores = [hit.score for hit in pool]
        reranked = sorted(
            (
                (hit.node_id, float(score))
                for hit, score in zip(pool, scores)
            ),
            key=lambda item: item[1],
            reverse=True,
        )
        score_cache: dict[str, float] = dict(reranked)
        member_scores: dict[str, float] = dict(reranked)
        retrieved_child_ids = [node_id for node_id, _ in reranked]

        groups = self._candidate_groups(member_scores)
        rows: list[dict] = []
        for parent_id, members in groups[: self.max_groups_per_query]:
            candidate_score = score_cache.get(parent_id, 0.0)
            section_id = hierarchy.node(parent_id).parent_id
            features = features_for_candidate(
                hierarchy, parent_id, members, query_type,
                settings.context_token_budget, candidate_score,
                len(members_at_level(hierarchy, parent_id, Level.CHILD)),
                query=query,
            )
            references = list(gold_answers or ())
            if not references and self.gold_answers.get(query):
                references = [self.gold_answers[query]]
            gold_ids = list(gold_children) if gold_children is not None else self.gold_child_map.get(query, [])
            gold_sets = [set(values) for values in (gold_child_sets or ()) if values]
            if not gold_sets and gold_ids:
                gold_sets = [set(gold_ids)]
            branches = {
                "keep": self._execute(query, query_type, members, retrieved_child_ids, references),
                "parent": self._execute(
                    query, query_type, {parent_id: candidate_score}, retrieved_child_ids, references
                ),
            }
            if section_id and section_id in hierarchy.nodes:
                section_score = self._score_cached(query, section_id, score_cache)
                branches["section"] = self._execute(
                    query, query_type, {section_id: section_score}, retrieved_child_ids, references
                )
            tau = self.weights.label_margin_tau

            if gold_sets:
                # v5 attribution-aware reward: Rescue / HarmfulDrift / P&R.
                R_ids = set(members)

                def v5_metrics(branch: dict) -> dict:
                    V = set(branch.get("verified_ids") or [])
                    G_ids = max(gold_sets, key=lambda candidate: _set_f1(V, candidate))
                    precision = len(V & G_ids) / max(1, len(V))
                    recall = len(V & G_ids) / max(1, len(G_ids))
                    rescue = len((V - R_ids) & G_ids)
                    harmful = len(((V - R_ids) - G_ids)) / max(1, len(V))
                    return {
                        "retrieved_ids": sorted(R_ids),
                        "verified_ids": sorted(V),
                        "gold_ids": sorted(G_ids),
                        "citation_precision": round(precision, 4),
                        "citation_recall": round(recall, 4),
                        "rescue": rescue,
                        "harmful_rate": round(harmful, 4),
                        "ambiguity": float(branch.get("ambiguous_rate", 0.0)),
                        "empty_evidence": int(not V),
}

            weights = self.weights
            for name_branch, branch in branches.items():
                metrics_v5 = v5_metrics(branch)
                branch["v5"] = metrics_v5
                branch["reward"] = round(
                    weights.citation_recall_w * metrics_v5["citation_recall"]
                    + weights.citation_precision_w * metrics_v5["citation_precision"]
                    + weights.rescue_w * metrics_v5["rescue"] / max(1, len(metrics_v5["gold_ids"]))
                    - weights.harmful_lambda * metrics_v5["harmful_rate"]
                    - weights.ambiguity_lambda * metrics_v5["ambiguity"]
                    - weights.empty_evidence_lambda * metrics_v5["empty_evidence"]
                    - weights.tokens_beta * min(1.0, branch["tokens"] / max(1, settings.context_token_budget))
                    - weights.latency_gamma * min(1.0, branch["latency_ms"] / 5000.0),
                    6,
                )
        else:
            # Legacy reward without gold (retrieval/verification only)
            weights = self.weights
            for name_branch, branch in branches.items():
                branch["reward"] = round(
                    weights.evidence_recall * branch["coverage"]
                    + weights.citation_quality * branch["citation_quality"]
                    - weights.attribution_risk_lambda * branch["attribution_risk"]
                    - weights.tokens_beta * min(1.0, branch["tokens"] / max(1, settings.context_token_budget))
                    - weights.latency_gamma * min(1.0, branch["latency_ms"] / 5000.0),
                    6,
                )

            row = RolloutRow(
                query=query,
                question_id=question_id,
                source=source,
                citation_evaluable=bool(gold_sets),
                query_type=query_type.value,
                parent_id=parent_id,
                section_id=section_id or "",
                features=[round(v, 6) for v in features.vector()],
                members={k: round(v, 6) for k, v in members.items()},
                branches=branches,
                label_parent=int(branches["parent"]["reward"] > branches["keep"]["reward"] + tau),
                label_section=(
                    int(branches.get("section", {}).get("reward", -1e9)
                        > branches["keep"]["reward"] + tau)
                    if "section" in branches else 0
                ),
            ).to_dict()
            if gold_sets:
                row["retrieved"] = sorted(members)
                row["gold"] = sorted(set().union(*gold_sets))
            rows.append(row)
        return rows

    # ------------------------------------------------------------------

    def _candidate_groups(
        self, member_scores: Mapping[str, float]
    ) -> list[tuple[str, dict[str, float]]]:
        hierarchy = self.hierarchy
        parents = [
            node.node_id
            for node in hierarchy.nodes.values()
            if node.level.value == "parent"
            and any(child in member_scores for child in node.child_ids)
        ]
        groups: list[tuple[str, dict[str, float], float]] = []
        min_hits = max(2, self.settings.min_child_hits)
        for parent_id in parents:
            slots = members_at_level(hierarchy, parent_id, Level.CHILD)
            members = {
                slot: float(member_scores[slot])
                for slot in slots
                if slot in member_scores
            }
            if len(members) < min_hits:
                continue
            groups.append((parent_id, members, max(members.values())))
        groups.sort(key=lambda item: item[2], reverse=True)
        return [(parent_id, members) for parent_id, members, _ in groups]

    def _score_cached(self, query: str, node_id: str, cache: dict[str, float]) -> float:
        if node_id not in cache:
            values = self.pipeline.reranker.score(query, [self.hierarchy.node(node_id).text])
            cache[node_id] = float(values[0])
        return cache[node_id]

    def _execute(
        self,
        query: str,
        query_type: QueryType,
        selected_scores: Mapping[str, float],
        retrieved_child_ids: Sequence[str],
        gold_answers: Sequence[str] | None = None,
    ) -> dict:
        hierarchy = self.hierarchy
        settings = self.settings
        supports: list[tuple[str, float]] = []

        context = assemble_context(hierarchy, selected_scores, query_type, settings)
        t0 = time.perf_counter()
        sample_rows: list[dict] = []
        supports: list[tuple[str, float]] = []
        for _ in range(self.samples):
            sample_supports: list[tuple[str, float]] = []
            raw_generation = None
            last_error: Exception | None = None
            for attempt in range(2):
                try:
                    raw_generation = self.pipeline.generator.generate(query, context)
                    break
                except Exception as exc:
                    last_error = exc
                    time.sleep(5 * (attempt + 1))
            if raw_generation is None:
                raise RuntimeError(f"rollout generation failed twice: {last_error}")
            generation, evidence, verification_metrics = verify_generation(
                raw_generation, context, hierarchy, self.pipeline.verifier,
                settings, claim_supports=sample_supports,
                retrieved_ids=set(retrieved_child_ids),
            )
            latency_ms = (time.perf_counter() - t0) * 1000.0 / self.samples
            answer_text = " ".join(claim.text for claim in generation.claims).strip()
            answer_f1 = (
                max(answer_token_f1(answer_text, gold) for gold in gold_answers)
                if gold_answers and answer_text else 0.0
            )
            supports.extend(sample_supports)

            descendant_children: set[str] = set()
            for block in context:
                frontier = list(block.evidence_ids) or [block.node_id]
                while frontier:
                    node_id = frontier.pop()
                    node = hierarchy.nodes.get(node_id)
                    if node is None:
                        continue
                    if node.level.value == "child":
                        descendant_children.add(node_id)
                    frontier.extend(node.child_ids)
            covered = len(descendant_children.intersection(retrieved_child_ids))
            coverage = covered / max(1, len(set(retrieved_child_ids)))

            att = attribution_metrics(
                sample_supports,
                len(raw_generation.claims),
                len(generation.claims),
                settings.nli_support_threshold,
            )
            support_values = [float(item.support_score) for item in evidence.values()]
            citation_quality = (
                sum(support_values) / len(support_values) * att["citation_survival_rate"]
                if support_values else 0.0
            )
            sample_rows.append({
                "answer_f1": answer_f1,
                "coverage": coverage,
                "citation_quality": citation_quality,
                "attribution_risk": att["attribution_risk"],
                "unsupported_claim_rate": att["unsupported_claim_rate"],
                "citation_survival_rate": att["citation_survival_rate"],
                "generated_claims": float(len(raw_generation.claims)),
                "verified_claims": float(len(generation.claims)),
                "latency_ms": latency_ms,
            })
        tokens = sum(block.token_count for block in context)
        keys = ("answer_f1", "coverage", "citation_quality", "attribution_risk",
                "unsupported_claim_rate", "citation_survival_rate",
                "generated_claims", "verified_claims", "latency_ms")
        means = {
            key: sum(row[key] for row in sample_rows) / len(sample_rows)
            for key in keys
        }
        weights = self.weights
        reward = (
            weights.answer_quality * means["answer_f1"]
            + weights.evidence_recall * means["coverage"]
            + weights.citation_quality * means["citation_quality"]
            - weights.attribution_risk_lambda * means["attribution_risk"]
            - weights.tokens_beta * min(1.0, tokens / max(1, settings.context_token_budget))
            - weights.latency_gamma * min(1.0, means["latency_ms"] / 5000.0)
        )
        support_mean = (
            sum(score for _, score in supports) / len(supports) if supports else 0.0
        )
        return {
            "reward": round(reward, 6),
            "samples": self.samples,
            "answer_f1": round(means["answer_f1"], 4),
            "verified_ids": sorted({ev.node_id for ev in evidence.values()}),
            "tokens": int(tokens),
            "latency_ms": round(means["latency_ms"], 1),
            "context_blocks": len(context),
            "generated_claims": round(means["generated_claims"], 2),
            "verified_claims": round(means["verified_claims"], 2),
            "mean_support": round(support_mean, 4),
            "attribution_risk": round(means["attribution_risk"], 4),
            "unsupported_claim_rate": round(means["unsupported_claim_rate"], 4),
            "citation_survival_rate": round(means["citation_survival_rate"], 4),
            "coverage": round(means["coverage"], 4),
            "citation_quality": round(means["citation_quality"], 4),
            "ambiguous_rate": round(
                float(verification_metrics.get("ambiguous_claims", 0.0))
                / max(1.0, means["generated_claims"]), 4),
        }
