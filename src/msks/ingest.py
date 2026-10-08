"""Parse → canonical text → cây node có offset code point (ARCHITECTURE.md mục 5.4, 6).

Canonical text của một parse revision = các section đã chuẩn hóa khoảng trắng (đúng như
edahr.text.pack_spans) nối bằng "\\n\\n". Offset leaf của edahr tương đối với section; ở đây cộng
offset section để mọi char_start/char_end trỏ vào canonical text. Python str đánh chỉ số theo
code point nên offset khớp hợp đồng với frontend.
"""
from __future__ import annotations

import hashlib
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from edahr.config import Settings as EdahrSettings
from edahr.hierarchy import HierarchyBuilder
from edahr.ingestion import DoclingScientificLoader
from edahr.schemas import DocumentSection, Level, ScientificDocument
from edahr.text import normalize

from .errors import PermanentError
from .settings import Settings


@dataclass
class NodeRow:
    legacy_node_id: str
    level: str
    text: str
    token_count: int
    position: int
    char_start: int
    char_end: int
    page_start: int | None
    page_end: int | None
    section_legacy: str | None = None
    parent_legacy: str | None = None
    paragraph_ids: list[str] = field(default_factory=list)
    embedding_text: str = ""


@dataclass
class Tree:
    canonical_text: str
    pages: list[dict] | None
    page_count: int | None
    nodes: list[NodeRow]
    edges: list[tuple[str, str, int]]  # (parent legacy, child legacy, ordinal)
    parser: str
    parser_version: str

    @property
    def leaves(self) -> list[NodeRow]:
        return [n for n in self.nodes if n.level == "child"]


def sniff(filename: str, data: bytes) -> tuple[str, str]:
    """(kind, mime) theo phần mở rộng + nội dung thực; từ chối khi không khớp (mục 15)."""
    extension = Path(filename).suffix.lower()
    if extension == ".pdf":
        if not data.startswith(b"%PDF-"):
            raise PermanentError("unsupported_media_type", "File .pdf không có chữ ký PDF hợp lệ.")
        return "pdf", "application/pdf"
    if extension in (".md", ".markdown", ".txt"):
        try:
            data.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise PermanentError("unsupported_media_type", "File văn bản phải mã hóa UTF-8.") from exc
        return ("markdown", "text/markdown") if extension != ".txt" else ("txt", "text/plain")
    raise PermanentError("unsupported_media_type", f"Định dạng {extension or '(không rõ)'} chưa được hỗ trợ trong MVP.")


def parse(kind: str, data: bytes, title: str, document_id: str, alias: str, max_pages: int) -> tuple[ScientificDocument, str, str]:
    if kind == "pdf":
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "document.pdf"
            path.write_bytes(data)
            try:
                document = DoclingScientificLoader().load([path])[0]
            except Exception as exc:  # Docling ném nhiều loại lỗi cho PDF hỏng
                raise PermanentError("pdf_parse_failed", f"Không đọc được PDF: {type(exc).__name__}") from exc
        pages = int(document.metadata.get("num_pages") or 0)
        if pages > max_pages:
            raise PermanentError("too_many_pages", f"PDF có {pages} trang, vượt giới hạn {max_pages}.")
        if not any(normalize(s.text) for s in document.sections):
            raise PermanentError("pdf_no_text_layer", "PDF không có lớp văn bản (bản scan). OCR chưa được hỗ trợ trong MVP.")
        document = ScientificDocument(document_id=document_id, source=alias, sections=document.sections, metadata=document.metadata)
        return document, "docling", str(document.metadata.get("parser_version", "unknown"))
    text = data.decode("utf-8-sig")
    if kind == "markdown":
        sections = DoclingScientificLoader._markdown_sections(text)
    else:
        sections = [DocumentSection("Document", text)]
    sections = [s for s in sections if normalize(s.text)]
    if not sections:
        raise PermanentError("empty_document", "Tài liệu không có nội dung văn bản.")
    document = ScientificDocument(document_id=document_id, source=alias, sections=tuple(sections),
                                  metadata={"parser": "msks-text", "parser_version": "1", "title": title})
    return document, "msks-text", "1"


def build_tree(document: ScientificDocument, settings: Settings, paginated: bool, parser: str, parser_version: str) -> Tree:
    edahr_settings = EdahrSettings(
        child_target_tokens=settings.child_target_tokens,
        child_overlap_sentences=settings.child_overlap_sentences,
        children_per_parent=settings.children_per_parent,
        parent_overlap_children=settings.parent_overlap_children,
    )
    hierarchy = HierarchyBuilder(edahr_settings).build([document])
    section_texts = [normalize(s.text) for s in document.sections]
    offsets: list[int] = []
    cursor = 0
    for text in section_texts:
        offsets.append(cursor)
        cursor += len(text) + 2
    canonical = "\n\n".join(section_texts)

    section_pos = {n.node_id: n.position for n in hierarchy.nodes.values() if n.level == Level.SECTION}
    rows: list[NodeRow] = []
    edges: list[tuple[str, str, int]] = []
    for node in hierarchy.nodes.values():
        pages = (node.page_start, node.page_end) if paginated else (None, None)
        if node.level == Level.DOCUMENT:
            start, end, text = 0, len(canonical), canonical
        elif node.level == Level.SECTION:
            off = offsets[node.position]
            start, end, text = off, off + len(section_texts[node.position]), node.text
        else:
            off = offsets[section_pos[node.section_id]]
            start, end, text = off + node.char_start, off + node.char_end, node.text
            if node.level == Level.CHILD and canonical[start:end] != node.text:
                raise PermanentError("offset_mismatch", "Offset leaf không khớp canonical text (lỗi nội bộ chunker).")
        rows.append(NodeRow(
            legacy_node_id=node.node_id,
            level=node.level.value,
            text=text,
            token_count=node.token_count,
            position=node.position,
            char_start=start,
            char_end=end,
            page_start=pages[0],
            page_end=pages[1],
            section_legacy=node.section_id if node.level in (Level.PARENT, Level.CHILD) else None,
            parent_legacy=node.parent_id if node.level in (Level.PARENT, Level.CHILD) else None,
            paragraph_ids=list(node.metadata.get("paragraph_ids", ()) or ()),
            embedding_text=node.embedding_text,
        ))
        for ordinal, child in enumerate(node.child_ids):
            edges.append((node.node_id, child, ordinal))

    page_blocks = _page_partition(document, offsets, len(canonical)) if paginated else None
    page_count = int(document.metadata.get("num_pages") or 0) or (len(page_blocks) if page_blocks else None)
    return Tree(canonical, page_blocks, page_count if paginated else None, rows, edges, parser, parser_version)


def _page_partition(document: ScientificDocument, offsets: list[int], length: int) -> list[dict]:
    """Chia canonical text thành các khối liên tiếp theo trang; mọi ký tự thuộc đúng một trang."""
    first_start: dict[int, int] = {}
    for section, off in zip(document.sections, offsets):
        spans = section.metadata.get("page_spans") or ()
        found = False
        for entry in spans:
            try:
                start, page = int(entry[0]), int(entry[2])
            except (TypeError, ValueError, IndexError):
                continue
            first_start[page] = min(first_start.get(page, off + start), off + start)
            found = True
        if not found:
            first_start.setdefault(int(section.page_start or 1), off)
    if not first_start:
        return [{"page": 1, "char_start": 0, "char_end": length}]
    ordered = sorted(first_start.items(), key=lambda item: (item[1], item[0]))
    blocks = []
    for index, (page, start) in enumerate(ordered):
        start = 0 if index == 0 else start
        end = ordered[index + 1][1] if index + 1 < len(ordered) else length
        if end > start:
            blocks.append({"page": page, "char_start": start, "char_end": end})
    return blocks


# ------------------------------------------------------------- fingerprint

_WORD = re.compile(r"\w+", re.UNICODE)
_PERMUTATIONS = 64
_MASK = (1 << 61) - 1


def minhash(text: str) -> list[int]:
    """MinHash trên shingle 5 từ (mục 6.2). Đủ cho phát hiện bản sao, không chứng minh độc lập."""
    words = _WORD.findall(text.casefold())
    shingles = {" ".join(words[i:i + 5]) for i in range(max(1, len(words) - 4))}
    hashes = [int.from_bytes(hashlib.blake2b(s.encode(), digest_size=8).digest(), "big") for s in shingles]
    signature = []
    for seed in range(_PERMUTATIONS):
        a, b = 2 * seed + 1, seed * 7919 + 3
        signature.append(min(((a * h + b) & _MASK) for h in hashes) if hashes else 0)
    return signature


def fingerprint_hex(signature: list[int]) -> str:
    return ",".join(format(v, "x") for v in signature)


def jaccard_estimate(first: str, second: str) -> float:
    a, b = first.split(","), second.split(",")
    if len(a) != len(b) or not a:
        return 0.0
    return sum(x == y for x, y in zip(a, b)) / len(a)


def sha256(data: bytes | str) -> str:
    return hashlib.sha256(data.encode() if isinstance(data, str) else data).hexdigest()
