"""Deterministic SciFact conversion for out-of-domain citation evaluation."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


def read_jsonl(path: str | Path) -> list[dict]:
    with Path(path).open(encoding="utf-8-sig") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def convert_scifact(corpus_path: str | Path, claims_path: str | Path,
                    split: str = "dev") -> tuple[list[dict], list[dict], dict]:
    corpus_file, claims_file = Path(corpus_path), Path(claims_path)
    corpus = {int(row["doc_id"]): row for row in read_jsonl(corpus_file)}
    questions: list[dict] = []
    used_documents: set[int] = set()

    for claim in read_jsonl(claims_file):
        for document_key, rationales in (claim.get("evidence") or {}).items():
            document_id = int(document_key)
            document = corpus[document_id]
            sentence_ids = sorted({
                int(sentence_id)
                for rationale in rationales
                for sentence_id in rationale.get("sentences") or ()
            })
            labels = {str(rationale["label"]) for rationale in rationales}
            if len(labels) != 1:
                raise ValueError(
                    f"claim {claim['id']} has conflicting labels for document {document_id}"
                )
            label = labels.pop()
            answer = {"SUPPORT": "supported", "CONTRADICT": "contradicted"}[label]
            abstract = list(document.get("abstract") or [])
            gold_quotes = [abstract[index] for index in sentence_ids]
            used_documents.add(document_id)
            questions.append({
                "dataset": "scifact", "split": split,
                "paper_id": str(document_id),
                "source": f"{document_id}.scifact",
                "question_id": f"scifact-{claim['id']}-{document_id}",
                "query": (
                    "Is the following scientific claim supported or contradicted? "
                    + str(claim["claim"])
                ),
                "answer": answer,
                "reference_answers": [answer],
                "gold_quotes": gold_quotes,
                "citation_evaluable_source": bool(gold_quotes),
                "scifact_claim_id": int(claim["id"]),
                "scifact_label": label,
                "gold_sentence_ids": sentence_ids,
            })

    papers = []
    for document_id in sorted(used_documents):
        document = corpus[document_id]
        papers.append({
            "dataset": "scifact", "split": split,
            "paper_id": str(document_id), "document_id": str(document_id),
            "source": f"{document_id}.scifact", "title": str(document.get("title") or ""),
            "sections": [{
                "title": "Abstract",
                "text": "\n".join(str(sentence) for sentence in document.get("abstract") or []),
                "section_type": "abstract", "position": 0,
            }],
        })

    report = {
        "schema_version": 1, "dataset": "scifact", "split": split,
        "corpus_sha256": hashlib.sha256(corpus_file.read_bytes()).hexdigest(),
        "claims_sha256": hashlib.sha256(claims_file.read_bytes()).hexdigest(),
        "papers": len(papers), "evaluable_claim_document_pairs": len(questions),
        "support_pairs": sum(row["scifact_label"] == "SUPPORT" for row in questions),
        "contradict_pairs": sum(row["scifact_label"] == "CONTRADICT" for row in questions),
    }
    return papers, questions, report