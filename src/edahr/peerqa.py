"""PeerQA -> the paper/question record format produced by ``convert_qasper``.

PeerQA (Baumgaertner et al., NAACL 2025) ships ``papers.jsonl`` as one row per
GROBID element (title, heading, sentence, list_item, formula, figure, table)
with a per-paper element index ``idx``, paragraph index ``pidx`` and the last
heading, and ``qa.jsonl`` whose ``answer_evidence_mapped`` points at element
``idx`` values. Here elements are grouped into paragraphs (by ``pidx``) and
sections (by heading), keeping stable paragraph ids and character offsets so
the rest of the pipeline (leaf labelling, paragraph evidence F1) is unchanged.

Only ``answerable_mapped`` questions have gold evidence; the evidence of the
single author annotation becomes one reference paragraph set.
"""

from __future__ import annotations

from collections import OrderedDict
from typing import Iterable

BODY_TYPES = {"sentence", "list_item", "formula", "figure", "table"}


def _norm(text: object) -> str:
    return " ".join(str(text or "").split())


def safe_document_id(paper_id: str) -> str:
    return paper_id.replace("/", "_")


def safe_source(paper_id: str) -> str:
    """Short display/source name. Context headers render the source, and the
    shared packer budgets a fixed 24-token header allowance; NLPeer ids reach
    ~150 characters. Leaf ids depend on ``document_id``, not on the source."""
    import hashlib
    return "pq-" + hashlib.sha1(paper_id.encode("utf-8")).hexdigest()[:10] + ".peerqa"


def convert_peerqa(paper_rows: Iterable[dict], qa_rows: Iterable[dict]) -> tuple[list[dict], list[dict], dict]:
    by_paper: dict[str, list[dict]] = OrderedDict()
    for row in paper_rows:
        by_paper.setdefault(str(row["paper_id"]), []).append(row)

    papers: list[dict] = []
    element_to_paragraph: dict[tuple[str, int], str] = {}
    paragraph_text: dict[str, str] = {}
    for paper_id, rows in by_paper.items():
        rows = sorted(rows, key=lambda r: int(r["idx"]))
        title = next((_norm(r["content"]) for r in rows if r.get("type") == "title"), "")
        sections: list[dict] = []
        current: dict | None = None
        paragraphs: "OrderedDict[str, list[dict]]" = OrderedDict()

        def flush_section():
            if current is None or not paragraphs:
                return
            metadata, cursor, texts = [], 0, []
            for pid, items in paragraphs.items():
                text = _norm(" ".join(_norm(i["content"]) for i in items))
                if not text:
                    continue
                metadata.append({"paragraph_id": pid, "text": text,
                                 "char_start": cursor, "char_end": cursor + len(text)})
                paragraph_text[pid] = text
                texts.append(text)
                cursor += len(text) + 1
            if texts:
                current["paragraphs"] = metadata
                current["text"] = "\n".join(texts)
                sections.append(current)

        for row in rows:
            kind = row.get("type")
            if kind == "heading" or current is None:
                flush_section()
                heading = _norm(row["content"]) if kind == "heading" else _norm(row.get("last_heading")) or "Preamble"
                current = {"title": heading, "section_type": "document",
                           "position": len(sections)}
                paragraphs = OrderedDict()
                if kind == "heading":
                    continue
            if kind not in BODY_TYPES:
                continue
            pid = f"{paper_id}:p{row['pidx']}"
            paragraphs.setdefault(pid, []).append(row)
            element_to_paragraph[(paper_id, int(row["idx"]))] = pid
        flush_section()
        papers.append({"dataset": "peerqa", "split": "test", "paper_id": paper_id,
                       "document_id": safe_document_id(paper_id),
                       "source": safe_source(paper_id), "title": title, "sections": sections})

    known = {p["paper_id"] for p in papers}
    questions: list[dict] = []
    skipped = {"not_answerable_mapped": 0, "paper_missing": 0, "evidence_unmapped": 0}
    for qa in qa_rows:
        if not qa.get("answerable_mapped"):
            skipped["not_answerable_mapped"] += 1
            continue
        paper_id = str(qa["paper_id"])
        if paper_id not in known:
            skipped["paper_missing"] += 1
            continue
        pids: list[str] = []
        for item in qa.get("answer_evidence_mapped") or ():
            for idx in item.get("idx") or ():
                pid = element_to_paragraph.get((paper_id, int(idx)))
                if pid and pid not in pids:
                    pids.append(pid)
        if not pids:
            skipped["evidence_unmapped"] += 1
            continue
        texts = [paragraph_text[pid] for pid in pids]
        questions.append({
            "dataset": "peerqa", "split": "test", "paper_id": paper_id,
            "source": safe_source(paper_id), "question_id": str(qa["question_id"]),
            "query": _norm(qa["question"]), "answer": _norm(qa.get("answer_free_form")),
            "reference_answers": [_norm(qa.get("answer_free_form"))],
            "reference_evidence_sets": [texts], "reference_paragraph_sets": [pids],
            "gold_paragraph_ids": sorted(pids), "gold_quotes": texts,
            "citation_evaluable_source": True,
        })
    report = {"papers": len(papers), "questions": len(questions), "skipped": skipped,
              "paragraphs": len(paragraph_text)}
    return papers, questions, report


def mteb_to_peerqa_rows(corpus: Iterable[dict], queries: Iterable[dict],
                        qrels: Iterable[dict]) -> tuple[list[dict], list[dict]]:
    """Map the MTEB packaging of PeerQA (``mteb/PeerQA``) to PeerQA-style rows.

    MTEB ships the redistributable (NLPeer) papers as one corpus unit per GROBID
    element, id ``{paper_id}_{idx}``, with the last heading in ``title`` (empty
    for the paper title and for heading units), plus author evidence as qrels
    over unit ids. Each unit becomes its own evidence unit (``pidx = idx``), so
    evidence F1 is computed at that (sentence-like) granularity. MTEB has no
    free-form answers.
    """
    corpus = list(corpus)
    # A unit is a heading only if a later unit names it as its last heading;
    # other units with an empty heading are captions, tables or unsectioned
    # body text (GROBID), and stay evidence units.
    headings: dict[str, set[str]] = {}
    for unit in corpus:
        paper_id = str(unit["id"]).rsplit("_", 1)[0]
        if _norm(unit.get("title")):
            headings.setdefault(paper_id, set()).add(_norm(unit["title"]))
    rows: list[dict] = []
    for unit in corpus:
        paper_id, idx = str(unit["id"]).rsplit("_", 1)
        idx = int(idx)
        is_heading = not _norm(unit.get("title")) and _norm(unit["text"]) in headings.get(paper_id, ())
        kind = "title" if idx == 0 else ("heading" if is_heading else "sentence")
        rows.append({"idx": idx, "pidx": idx, "sidx": 0, "type": kind,
                     "content": unit["text"], "last_heading": unit.get("title") or None,
                     "paper_id": paper_id})
    evidence: dict[str, list[int]] = {}
    paper_of: dict[str, str] = {}
    for rel in qrels:
        if int(rel.get("score", 1)) <= 0:
            continue
        paper_id, idx = str(rel["corpus-id"]).rsplit("_", 1)
        evidence.setdefault(str(rel["query-id"]), []).append(int(idx))
        paper_of[str(rel["query-id"])] = paper_id
    qa: list[dict] = []
    for query in queries:
        qid = str(query["id"])
        if qid not in evidence:
            continue
        qa.append({"paper_id": paper_of[qid], "question_id": qid, "question": query["text"],
                   "answer_free_form": "", "answerable": True, "answerable_mapped": True,
                   "answer_evidence_mapped": [{"idx": sorted(set(evidence[qid]))}]})
    return rows, qa
