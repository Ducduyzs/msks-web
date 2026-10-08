"""V9 exploratory selectors. Gold labels are deliberately absent from this API.

Dependency groups are deterministic linguistic heuristics, not an asserted
semantic parser. All compression is extractive with section-relative offsets.
"""
from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass, replace
from typing import Callable

from .schemas import ContextBlock, Hierarchy, Level
from .text import SENTENCE, jaccard, normalize, token_set


@dataclass(frozen=True)
class Unit:
    uid: str
    text: str
    leaf_id: str
    section_id: str
    start: int
    end: int
    score: float


def sentence_units(hierarchy: Hierarchy, leaf_ids: list[str]) -> list[Unit]:
    """Deduplicate overlapping child sentences by section and exact offsets."""
    units: dict[tuple, Unit] = {}
    for leaf_id in leaf_ids:
        node = hierarchy.node(leaf_id)
        start = 0
        for boundary in [m.start() for m in SENTENCE.finditer(node.text)] + [len(node.text)]:
            raw = node.text[start:boundary]
            left = len(raw) - len(raw.lstrip())
            right = len(raw.rstrip())
            if right > left:
                a, b = node.char_start + start + left, node.char_start + start + right
                key = (node.section_id, a, b)
                uid = hashlib.sha256(repr(key).encode()).hexdigest()[:20]
                units.setdefault(key, Unit(uid, raw.strip(), leaf_id, node.section_id or '', a, b, 0.0))
            start = boundary
    return list(units.values())


def leaf_units(hierarchy: Hierarchy, ranking: list[tuple[str, float]]) -> list[Unit]:
    return [Unit(cid, hierarchy.node(cid).text, cid, hierarchy.node(cid).section_id or '',
                 hierarchy.node(cid).char_start, hierarchy.node(cid).char_end, score)
            for cid, score in ranking]


def dependency_groups(units: list[Unit], mode: str) -> dict[str, tuple[str, ...]]:
    """Bounded closure for discourse/condition/metric context within a section.

The window baseline always takes +/-1. Dependency mode uses at most two
nearby sentences, requiring cues plus lexical overlap for qualifiers. No
gold, reference answer, or verifier feedback participates in selection.
"""
    sections: dict[str, list[Unit]] = {}
    for unit in units:
        sections.setdefault(unit.section_id, []).append(unit)
    groups = {}
    anaphora = re.compile(r'^(this|these|those|it|they|such|the former|the latter|however|therefore|thus)\b', re.I)
    qualifier = re.compile(r'\b(only|unless|except|under|when|without|baseline|dataset|setting|evaluat|metric|compared|table)\w*', re.I)
    result_cue = re.compile(r'\d|\b(improv|outperform|result|accuracy|score|f1|bleu)\w*', re.I)
    for section in sections.values():
        section.sort(key=lambda u: (u.start, u.end))
        for i, unit in enumerate(section):
            members = {unit.uid}
            if mode == 'window':
                members.update(u.uid for u in section[max(0, i - 1):i + 2])
            elif mode == 'dependency':
                if i and anaphora.search(unit.text):
                    members.add(section[i - 1].uid)
                if result_cue.search(unit.text):
                    for other in section[max(0, i - 2):i + 3]:
                        if other.uid != unit.uid and qualifier.search(other.text):
                            if len(token_set(unit.text) & token_set(other.text)) >= 2:
                                members.add(other.uid)
                            if len(members) == 3:
                                break
            groups[unit.uid] = tuple(u.uid for u in section if u.uid in members)
    return groups


def select_units(units: list[Unit], query: str, budget: int, count: Callable[[str], int],
                 method: str = 'top', groups: dict | None = None,
                 max_items: int | None = None) -> list[Unit]:
    """Greedy set selection with exact rendered-content cost and no truncation.

Coverage is an explicitly lexical surrogate, not semantic sufficiency.
Unit counts include a conservative per-block header allowance.
"""
    if budget <= 0:
        raise ValueError('budget must be positive')
    by_id = {u.uid: u for u in units}
    remaining = sorted(units, key=lambda u: (-u.score, u.uid))
    selected: list[Unit] = []
    selected_ids: set[str] = set()
    covered: set[str] = set()
    query_words = token_set(query)
    words_by_id = {u.uid: token_set(u.text) for u in units}
    costs = {u.uid: count(u.text) + 24 for u in units}
    max_redundancy = {u.uid: 0.0 for u in units}
    used = 0
    while remaining and (max_items is None or len(selected) < max_items):
        choices = []
        for unit in remaining:
            group = groups.get(unit.uid, (unit.uid,)) if groups else (unit.uid,)
            new = [by_id[uid] for uid in group if uid not in selected_ids]
            if not new:
                continue
            cost = sum(costs[u.uid] for u in new)
            if used + cost > budget:
                continue
            if max_items is not None and len(selected) + len(new) > max_items:
                continue
            words = set().union(*(words_by_id[u.uid] for u in new))
            novelty = len((words & query_words) - covered) / max(1, len(query_words))
            redundancy = max_redundancy[unit.uid]
            if method == 'top':
                utility = unit.score
            elif method == 'mmr':
                utility = 0.7 * unit.score - 0.3 * redundancy
            else:
                utility = (unit.score + 0.35 * novelty - 0.15 * redundancy) / math.sqrt(max(1, cost))
            choices.append((utility, unit.uid, new, cost, words))
        if not choices:
            break
        _, uid, new, cost, words = max(choices, key=lambda x: (x[0], x[1]))
        selected.extend(new)
        selected_ids.update(u.uid for u in new)
        used += cost
        covered |= words & query_words
        remaining = [u for u in remaining if u.uid != uid and u.uid not in selected_ids]
        if method != 'top':
            for candidate in remaining:
                max_redundancy[candidate.uid] = max(max_redundancy[candidate.uid],
                    max((jaccard(words_by_id[candidate.uid], words_by_id[s.uid]) for s in new), default=0.0))
    return selected


def blocks_from_units(units: list[Unit], hierarchy: Hierarchy,
                      count: Callable[[str], int]) -> list[ContextBlock]:
    blocks = []
    for i, unit in enumerate(units, 1):
        node = hierarchy.node(unit.leaf_id)
        blocks.append(ContextBlock(f'C{i}', unit.leaf_id, Level.CHILD, unit.text,
            node.source, node.page_start, node.page_end, (unit.leaf_id,),
            unit.score, count(unit.text), unit.start, unit.end,
            unit.text != node.text))
    return blocks


def restrict_to_visible(hierarchy: Hierarchy, context: list[ContextBlock]) -> Hierarchy:
    """A verifier cannot read hidden parts of the original 220-token leaf.

Same-leaf blocks are combined in document order. Metadata retains only
paragraphs with text overlap with visible spans (diagnostic, not proof of
full paragraph coverage). Leaf IDs and the original gold mapping stay fixed.
"""
    nodes = dict(hierarchy.nodes)
    by_leaf: dict[str, list[ContextBlock]] = {}
    for block in context:
        by_leaf.setdefault(block.node_id, []).append(block)
    for leaf_id, blocks in by_leaf.items():
        original = nodes[leaf_id]
        blocks = sorted(blocks, key=lambda b: (b.char_start, b.char_end))
        text = '\n'.join(dict.fromkeys(b.text for b in blocks))
        paragraphs = original.metadata.get('paragraph_texts') or {}
        visible_paragraphs = {
            pid: paragraph for pid, paragraph in paragraphs.items()
            if any(normalize(b.text) in normalize(paragraph) or normalize(paragraph) in normalize(b.text)
                   for b in blocks)
        }
        nodes[leaf_id] = replace(original, text=text, embedding_text=text,
            metadata={**original.metadata, 'paragraph_texts': visible_paragraphs,
                      'paragraph_ids': tuple(visible_paragraphs)})
    return Hierarchy(nodes, hierarchy.child_ids)


def verify_visible(generation, context, hierarchy, verifier, settings, retrieved_ids):
    """Verify each claim only against its cited fragments, not other blocks."""
    from .schemas import Generation
    from .verification import verify_generation
    all_claims, all_evidence, traces = [], {}, []
    for index, claim in enumerate(generation.claims):
        cited = [b for b in context if b.context_id in claim.citations]
        visible = restrict_to_visible(hierarchy, cited)
        trace = []
        verified, evidence, _ = verify_generation(
            Generation(generation.answerable, (claim,), generation.reason), cited,
            visible, verifier, settings, retrieved_ids=retrieved_ids, verification_trace=trace)
        remap = {}
        for eid, item in evidence.items():
            new_id = f'E{len(all_evidence) + 1}'
            all_evidence[new_id] = replace(item, evidence_id=new_id)
            remap[eid] = new_id
        all_claims.extend(replace(c, citations=tuple(remap[e] for e in c.citations)) for c in verified.claims)
        for item in trace:
            item['claim_index'] = index
            traces.append(item)
    return (Generation(generation.answerable and bool(all_claims), tuple(all_claims),
                       generation.reason, generation.validation_errors), all_evidence, traces)


def diagnostics(record: dict, hierarchy: Hierarchy, pool_ids: list[str],
                context: list[ContextBlock], raw_generation, evidence: dict,
                gold_ids: set[str]) -> dict:
    """Gold-only diagnostic layer, called AFTER selection and generation."""
    visible = restrict_to_visible(hierarchy, context)
    cited_cids = {cid for claim in raw_generation.claims for cid in claim.citations}
    packed = {b.node_id for b in context}
    cited = {b.node_id for b in context if b.context_id in cited_cids}
    verified = {e.node_id for e in evidence.values()}
    full_leaf = {b.node_id for b in context if normalize(hierarchy.node(b.node_id).text) in normalize(b.text)}
    ref_sets = record.get('reference_evidence_sets') or []
    text = normalize(' '.join(b.text for b in context))
    nonempty = [r for r in ref_sets if r]
    def char_coverage(paragraph):
        p = normalize(paragraph)
        covered = set()
        for block in context:
            fragment = normalize(block.text)
            if p in fragment:
                return 1.0
            start = p.find(fragment)
            if start >= 0:
                covered.update(range(start, start + len(fragment)))
        return len(covered) / max(1, len(p))
    retained = [sum(char_coverage(p) for p in refs) / len(refs) for refs in nonempty]
    return {
        'pool_gold_recall': len(set(pool_ids) & gold_ids) / max(1, len(gold_ids)),
        'packed_leaf_touch_recall': len(packed & gold_ids) / max(1, len(gold_ids)),
        'packed_full_leaf_recall': len(full_leaf & gold_ids) / max(1, len(gold_ids)),
        'raw_cited_leaf_recall': len(cited & gold_ids) / max(1, len(gold_ids)),
        'verified_leaf_recall': len(verified & gold_ids) / max(1, len(gold_ids)),
        'gold_paragraph_char_coverage_best_ref': max(retained, default=0.0),
        'complete_evidence_set_visible': float(any(all(char_coverage(p) >= .999 for p in refs) for refs in nonempty)),
        'pool_ids': pool_ids,
        'visible_paragraph_ids': sorted({pid for cid in packed for pid in visible.node(cid).metadata.get('paragraph_texts', {})}),
        'context': [dict(context_id=b.context_id, node_id=b.node_id, text=b.text,
                         char_start=b.char_start, char_end=b.char_end) for b in context],
    }
