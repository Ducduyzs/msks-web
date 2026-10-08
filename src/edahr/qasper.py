"""Deterministic QASPER v0.3 conversion with stable paper/question identity."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Iterable, Sequence

from .schemas import DocumentSection, ScientificDocument


def _unique_text(values: Iterable[object]) -> list[str]:
    output: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = " ".join(str(value or "").split())
        if text and text not in seen:
            seen.add(text)
            output.append(text)
    return output


# Marks a reference evidence string that names a real paragraph but differs
# from it in whitespace. The official evaluator compares raw strings exactly,
# so such a reference can never be matched by a paragraph prediction; keeping
# it (for the len() denominator) but unmatchable reproduces official scores.
OFFICIAL_UNMATCHABLE_PREFIX = "\u0000official-unmatchable:"


def _answer_text(answer: dict) -> str:
    """Reference string exactly as AllenAI's official evaluator builds it.

    Precedence extractive > free-form > yes/no, spans joined with ", " and
    *not* de-duplicated (repeated spans change token-F1 denominators).
    Verified per question by scripts/audit_evaluator.py.
    """
    if answer.get("unanswerable"):
        return "Unanswerable"
    spans = answer.get("extractive_spans") or ()
    if spans:
        return ", ".join(str(span) for span in spans)
    free_form = answer.get("free_form_answer")
    if free_form:
        return str(free_form)
    yes_no = answer.get("yes_no")
    if yes_no:
        return "Yes"
    if yes_no is not None:
        return "No"
    return ""


def convert_qasper(raw_path: str | Path, split: str) -> tuple[list[dict], list[dict], dict]:
    """Convert one official QASPER JSON file into paper and question records.

    Annotation references remain separate. Paragraph identifiers are stable
    within a release and permit official paragraph scoring independently from
    overlapping retrieval leaves.
    """
    source_path = Path(raw_path)
    raw = json.loads(source_path.read_text(encoding="utf-8-sig"))
    papers: list[dict] = []
    questions: list[dict] = []
    seen_question_ids: set[str] = set()

    for paper_id, paper in raw.items():
        source = f"{paper_id}.qasper"
        sections = []
        paragraph_lookup: dict[str, str] = {}
        raw_paragraphs = {
            str(value) for section in paper.get("full_text") or ()
            for value in section.get("paragraphs") or ()
        }
        if paper.get("abstract"):
            raw_paragraphs.add(str(paper["abstract"]))
        for position, section in enumerate(paper.get("full_text") or ()):
            paragraphs = [" ".join(str(value or "").split())
                          for value in section.get("paragraphs") or ()]
            paragraphs = [value for value in paragraphs if value]
            text = "\n".join(paragraphs).strip()
            if not text:
                continue
            paragraph_metadata = []
            cursor = 0
            for paragraph_position, paragraph in enumerate(paragraphs):
                paragraph_id = f"{paper_id}:section:{position}:paragraph:{paragraph_position}"
                start, end = cursor, cursor + len(paragraph)
                paragraph_metadata.append({
                    "paragraph_id": paragraph_id, "text": paragraph,
                    "char_start": start, "char_end": end,
                })
                paragraph_lookup.setdefault(paragraph, paragraph_id)
                cursor = end + 1
            sections.append({
                "title": str(section.get("section_name") or f"Section {position + 1}"),
                "text": text,
                "section_type": "document",
                "position": position,
                "paragraphs": paragraph_metadata,
            })
        abstract = str(paper.get("abstract") or "").strip()
        if abstract and not any(section["title"].casefold() == "abstract" for section in sections):
            paragraph_id = f"{paper_id}:abstract:paragraph:0"
            paragraph_lookup.setdefault(" ".join(abstract.split()), paragraph_id)
            sections.insert(0, {
                "title": "Abstract", "text": abstract,
                "section_type": "abstract", "position": -1,
                "paragraphs": [{
                    "paragraph_id": paragraph_id, "text": abstract,
                    "char_start": 0, "char_end": len(abstract),
                }],
            })
        papers.append({
            "dataset": "qasper", "split": split, "paper_id": paper_id,
            "document_id": paper_id, "source": source,
            "title": str(paper.get("title") or ""), "sections": sections,
        })

        for qa in paper.get("qas") or ():
            question_id = str(qa.get("question_id") or "")
            if not question_id:
                raise ValueError(f"QASPER question without question_id in paper {paper_id}")
            if question_id in seen_question_ids:
                raise ValueError(f"duplicate question_id in {split}: {question_id}")
            seen_question_ids.add(question_id)
            annotations = [item.get("answer") or {} for item in (qa.get("answers") or ())]
            references = [_answer_text(answer) for answer in annotations]
            # Kept as lists with duplicates: the official paragraph F1 divides
            # by len(list), not by the number of distinct paragraphs.
            evidence_sets = [
                [" ".join(str(quote or "").split()) for quote in answer.get("evidence") or ()]
                for answer in annotations
            ]
            # Distinct paragraphs per annotator (leaf labels, stratification).
            paragraph_sets = [
                list(dict.fromkeys(
                    paragraph_lookup[quote] for quote in evidence if quote in paragraph_lookup
                ))
                for evidence in evidence_sets
            ]
            evidence = _unique_text(quote for values in evidence_sets for quote in values)
            questions.append({
                "dataset": "qasper", "split": split,
                "paper_id": paper_id, "source": source,
                "question_id": question_id,
                "query": str(qa.get("question") or "").strip(),
                "answer": references[0] if references else "",
                "reference_answers": references,
                "reference_evidence_sets": [
                    [
                        quote if quote not in paragraph_lookup or raw_quote in raw_paragraphs
                        else OFFICIAL_UNMATCHABLE_PREFIX + quote
                        for raw_quote, quote in zip(answer.get("evidence") or (), evidence)
                    ]
                    for answer, evidence in zip(annotations, evidence_sets)
                ],
                "reference_paragraph_sets": paragraph_sets,
                "gold_paragraph_ids": sorted({item for values in paragraph_sets for item in values}),
                "gold_quotes": evidence,
                "citation_evaluable_source": bool(evidence),
                "all_annotations_unanswerable": bool(annotations) and all(
                    bool(answer.get("unanswerable")) for answer in annotations
                ),
                "annotation_count": len(annotations),
            })

    digest = hashlib.sha256(source_path.read_bytes()).hexdigest()
    report = {
        "dataset": "qasper", "split": split, "source": str(source_path.resolve()),
        "source_sha256": digest, "papers": len(papers), "questions": len(questions),
        "stable_question_ids": len(seen_question_ids),
        "questions_with_gold_quotes": sum(bool(row["gold_quotes"]) for row in questions),
        "questions_with_reference_answers": sum(bool(row["reference_answers"]) for row in questions),
        "all_annotations_unanswerable": sum(
            bool(row["all_annotations_unanswerable"]) for row in questions
        ),
    }
    report["gold_quote_question_rate"] = (
        report["questions_with_gold_quotes"] / max(1, report["questions"])
    )
    return papers, questions, report


def write_jsonl(rows: Sequence[dict], path: str | Path) -> Path:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    return output


def read_jsonl(path: str | Path) -> list[dict]:
    with Path(path).open(encoding="utf-8-sig") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def documents_from_paper_records(records: Sequence[dict]) -> list[ScientificDocument]:
    documents: list[ScientificDocument] = []
    for record in records:
        sections = tuple(
            DocumentSection(
                title=str(section["title"]), text=str(section["text"]),
                section_type=str(section.get("section_type") or "document"),
                metadata={
                    "qasper_position": section.get("position"),
                    "paragraphs": list(section.get("paragraphs") or ()),
                },
            )
            for section in record.get("sections") or ()
            if str(section.get("text") or "").strip()
        )
        documents.append(ScientificDocument(
            document_id=str(record["document_id"]),
            source=str(record["source"]), sections=sections,
            metadata={
                "dataset": record.get("dataset") or "qasper", "split": record.get("split"),
                "title": record.get("title"), "paper_id": record.get("paper_id"),
            },
        ))
    return documents
