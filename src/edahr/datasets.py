"""QASPER dataset loading (Wadhwa et al., NAACL 2021).

QASPER ships parsed paper text (no PDFs), so ingestion builds
:class:`ScientificDocument` objects directly from ``full_text.sections``
while keeping the rest of the pipeline unchanged. Page provenance is not
available in the source data; sections default to a synthetic page 1, so
page-level metrics are only meaningful on the PDF corpus.

Gold answers: questions with non-empty ``extractive_spans`` (verbatim document
substrings) or a non-empty ``free_form_answer`` are kept; yes/no and
unanswerable items are skipped. Extractive spans double as gold quotes for
scoped auto-labelling of evidence children.
"""

from __future__ import annotations

import json
import tarfile
from pathlib import Path
from typing import Iterator, Mapping
from urllib.request import urlretrieve

from .schemas import DocumentSection, ScientificDocument

S3 = "https://qasper-dataset.s3.us-west-2.amazonaws.com/{archive}"
ARCHIVES = {
    "train": ("qasper-train-dev-v0.3.tgz", "qasper-train-v0.3.json"),
    "dev": ("qasper-train-dev-v0.3.tgz", "qasper-dev-v0.3.json"),
    "test": ("qasper-test-and-evaluator-v0.3.tgz", "qasper-test-v0.3.json"),
}


def ensure_split(data_dir: str | Path, split: str = "dev") -> Path:
    """Download and extract one QASPER split; returns the JSON path."""
    if split not in ARCHIVES:
        raise ValueError(f"unknown split {split!r}")
    directory = Path(data_dir)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / ARCHIVES[split][1]
    if target.exists():
        return target
    archive_name, inner_name = ARCHIVES[split]
    archive = directory / archive_name
    print(f"[qasper] downloading {archive.name} ...", flush=True)
    urlretrieve(S3.format(archive=archive_name), archive)
    with tarfile.open(archive) as handle:
        member = next(
            m for m in handle.getmembers() if Path(m.name).name == inner_name
        )
        extracted = handle.extractfile(member)
        target.write_bytes(extracted.read())
    return target


def load_papers(json_path: str | Path) -> dict[str, ScientificDocument]:
    """Parse every paper into a ScientificDocument keyed by paper id."""
    payload = json.loads(Path(json_path).read_text(encoding="utf-8"))
    documents: dict[str, ScientificDocument] = {}
    for paper_id, record in payload.items():
        sections: list[DocumentSection] = []
        title = str(record.get("title") or paper_id)
        abstract_text = str(record.get("abstract") or "").strip()
        if abstract_text:
            sections.append(DocumentSection("Abstract", abstract_text))
        full_text = record.get("full_text") or []
        if isinstance(full_text, dict):
            full_text = full_text.get("sections") or []
        for position, section in enumerate(full_text):
            name = str(section.get("section_name") or f"Section {position}")
            paragraphs = section.get("paragraphs") or []
            body = "\n".join(
                str(paragraph if isinstance(paragraph, str)
                    else (paragraph.get("text") or "")).strip()
                for paragraph in paragraphs
            ).strip()
            if body:
                sections.append(DocumentSection(name, body))
        if not sections:
            continue
        documents[paper_id] = ScientificDocument(
            document_id=paper_id,
            source=f"{paper_id}.pdf",
            sections=tuple(sections),
            metadata={"parser": "qasper-json", "num_sections": len(sections)},
        )
    return documents


def iter_questions(
    documents: Mapping[str, ScientificDocument],
    raw_payload: dict | None = None,
) -> Iterator[dict]:
    """Yield answerable questions with gold answers and verbatim gold quotes.

    Gold quotes prefer ``highlighted_evidence`` (verbatim document substrings)
    and fall back to ``extractive_spans``; both feed scoped auto-labelling.
    """
    if raw_payload is None:
        raise ValueError("iter_questions needs the raw QASPER payload")
    for paper_id, record in raw_payload.items():
        if paper_id not in documents:
            continue
        source = documents[paper_id].source
        for entry in record.get("qas") or []:
            query = str(entry.get("question") or "").strip()
            if not query:
                continue
            for answer in entry.get("answers") or []:
                inner = answer.get("answer") or {}
                if inner.get("unanswerable"):
                    continue
                spans = [
                    str(span).strip()
                    for span in inner.get("extractive_spans") or []
                    if str(span).strip()
                ]
                highlighted = [
                    str(item).strip()
                    for item in inner.get("highlighted_evidence")
                    or inner.get("evidence") or []
                    if str(item).strip()
                ]
                free = str(inner.get("free_form_answer") or "").strip()
                if not spans and not free and not highlighted:
                    continue
                gold_answer = " ".join(spans) if spans else free
                yield {
                    "query": query,
                    "question_id": str(entry.get("question_id") or ""),
                    "source": source,
                    "gold_answer": gold_answer,
                    "gold_quotes": spans or highlighted,
                    "yes_no": inner.get("yes_no"),
                }
                break  # first usable annotation per question
