from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .schemas import DocumentSection, ScientificDocument


KNOWN = ("abstract", "introduction", "related work", "method", "methods", "experiments",
         "results", "discussion", "limitations", "conclusion", "references")

SECTION_LABELS = {"section_header", "title", "subtitle"}
TEXT_LABELS = {"text", "paragraph", "list_item", "footnote", "caption", "reference",
               "page_header", "page_footer", "empty", "code", "formula"}


def normalize_section_type(title: str) -> str:
    value = re.sub(r"^\d+(?:\.\d+)*\.?\s*", "", title).strip().casefold()
    return next((name.replace(" ", "_") for name in KNOWN if name in value), "document")


class DoclingScientificLoader:
    """Layout-aware PDF loader preserving page/char provenance from Docling.

    Heavy imports are intentionally lazy. The loader walks the structured
    Docling document model (items + provenance) instead of exporting plain
    markdown, so every section keeps its real page range, per-item page spans,
    bounding boxes and parser confidence whenever the backend exposes them.
    """

    def load(self, paths: list[str | Path]) -> list[ScientificDocument]:
        try:
            import docling
            from docling.document_converter import DocumentConverter
        except ImportError as exc:
            raise RuntimeError("Install project dependencies to use Docling ingestion") from exc
        converter = DocumentConverter()
        documents: list[ScientificDocument] = []
        for raw_path in paths:
            path = Path(raw_path)
            converted = converter.convert(str(path))
            document = converted.document
            version = getattr(docling, "__version__", "unknown")
            sections = self._structured_sections(document)
            structure = "docling-items"
            if not sections:
                sections = self._markdown_sections(document.export_to_markdown())
                structure = "markdown-fallback"
            documents.append(ScientificDocument(
                document_id=path.stem,
                source=path.name,
                sections=tuple(sections),
                metadata={
                    "path": str(path.resolve()),
                    "parser": "docling",
                    "parser_version": version,
                    "structure_source": structure,
                    "num_pages": len(getattr(document, "pages", {}) or {}),
                },
            ))
        return documents

    # ------------------------------------------------------------------
    # Structured extraction (preferred)
    # ------------------------------------------------------------------

    def _structured_sections(self, document: Any) -> list[DocumentSection]:
        entries = self._iter_items(document)
        if not entries:
            return []
        sections: list[DocumentSection] = []
        current_title = "Document"
        current_type = "document"
        pieces: list[tuple[str, int | None]] = []

        def flush() -> None:
            nonlocal pieces
            if not pieces:
                return
            body_parts: list[str] = []
            page_spans: list[tuple[int, int, int]] = []
            bboxes: list[Any] = []
            confidences: list[float] = []
            cursor = 0
            for text, page, bbox, confidence in pieces:
                if not text:
                    continue
                if body_parts:
                    separator = "\n"
                else:
                    separator = ""
                body_parts.append(separator + text if separator else text)
                start = cursor + len(separator)
                end = start + len(text)
                cursor = end
                if page is not None:
                    page_spans.append((start, end, int(page)))
                if bbox is not None:
                    bboxes.append(bbox)
                if confidence is not None:
                    confidences.append(confidence)
            joined = "".join(body_parts).strip()
            if not joined:
                pieces = []
                return
            normalized = re.sub(r"[ \t]+", " ", joined)
            offset_delta = 0
            adjusted_spans: list[tuple[int, int, int]] = []
            if normalized != joined:
                adjusted_spans = _remap_page_spans(joined, normalized, page_spans)
            else:
                adjusted_spans = page_spans
            pages = [page for _, _, page in adjusted_spans]
            confidence = sum(confidences) / len(confidences) if confidences else 1.0
            sections.append(DocumentSection(
                title=current_title,
                text=normalized.strip(),
                page_start=min(pages) if pages else 1,
                page_end=max(pages) if pages else 1,
                section_type=current_type,
                confidence=round(min(1.0, max(0.0, confidence)), 4),
                metadata={
                    "page_spans": adjusted_spans,
                    "bboxes": [_bbox_tuple(box) for box in bboxes][:64],
                    "num_items": len(pieces),
                },
            ))
            pieces = []

        for item in entries:
            label = str(getattr(item, "label", "") or "").rsplit(".", 1)[-1].lower()
            text, page, bbox, confidence = self._item_payload(item, document)
            if label in SECTION_LABELS:
                flush()
                current_title = (text or current_title).strip() or "Untitled Section"
                current_type = normalize_section_type(current_title)
                continue
            if not text:
                continue
            pieces.append((text, page, bbox, confidence))
        flush()

        merged: list[DocumentSection] = []
        preamble: list[DocumentSection] = []
        for section in sections:
            if section.title == "Document":
                preamble.append(section)
            else:
                merged.append(section)
        return preamble + merged or sections

    @staticmethod
    def _iter_items(document: Any) -> list[Any]:
        iterator = getattr(document, "iterate_items", None)
        if iterator is None:
            return []
        items: list[Any] = []
        try:
            for entry in iterator():
                items.append(entry[0] if isinstance(entry, tuple) else entry)
        except TypeError:
            return []
        return items

    @staticmethod
    def _item_payload(item: Any, document: Any) -> tuple[str, int | None, Any, float | None]:
        text = getattr(item, "text", None)
        if text is None and hasattr(item, "export_to_markdown"):
            try:
                text = item.export_to_markdown(document)
            except Exception:
                text = None
        if text is None and hasattr(item, "export_to_html"):
            try:
                text = re.sub(r"<[^>]+>", " ", item.export_to_html(document))
            except Exception:
                text = None
        provs = getattr(item, "prov", None) or []
        page = None
        bbox = None
        confidence = getattr(item, "confidence", None)
        if provs:
            first = provs[0]
            page = getattr(first, "page_no", None)
            bbox = getattr(first, "bbox", None)
            if confidence is None:
                confidence = getattr(first, "confidence", None)
        return (
            re.sub(r"\s+", " ", str(text)).strip() if text else "",
            page,
            bbox,
            float(confidence) if isinstance(confidence, (int, float)) else None,
        )

    # ------------------------------------------------------------------
    # Markdown fallback (structure unavailable)
    # ------------------------------------------------------------------

    @staticmethod
    def _markdown_sections(markdown: str) -> list[DocumentSection]:
        blocks = re.split(r"(?m)^(#{1,3})\s+(.+?)\s*$", markdown)
        sections: list[DocumentSection] = []
        if blocks and blocks[0].strip():
            sections.append(DocumentSection("Document", blocks[0].strip()))
        for index in range(1, len(blocks), 3):
            title = blocks[index + 1].strip()
            text = blocks[index + 2].strip() if index + 2 < len(blocks) else ""
            if text:
                sections.append(DocumentSection(title, text, section_type=normalize_section_type(title)))
        return sections or [DocumentSection("Document", markdown)]


def _bbox_tuple(bbox: Any) -> tuple[float, ...] | None:
    if bbox is None:
        return None
    for attr in ("as_tuple",):
        if hasattr(bbox, attr):
            try:
                return tuple(round(float(v), 2) for v in bbox.as_tuple())
            except Exception:
                return None
    try:
        return tuple(round(float(v), 2) for v in bbox)
    except Exception:
        return None


def _remap_page_spans(
    original: str, normalized: str, page_spans: list[tuple[int, int, int]]
) -> list[tuple[int, int, int]]:
    """Keep page attribution aligned after whitespace compression."""
    remapped: list[tuple[int, int, int]] = []
    search_from = 0
    for start, end, page in sorted(page_spans, key=lambda s: s[0]):
        snippet = original[start:end]
        probe = re.sub(r"\s+", " ", snippet).strip()
        if not probe:
            continue
        needle = probe[: min(len(probe), 48)]
        new_start = normalized.find(needle, search_from)
        if new_start < 0:
            new_start = normalized.find(needle)
        if new_start < 0:
            new_start = max(search_from, start - offset_guess(page_spans, start))
        search_from = max(search_from, new_start)
        new_end = min(len(normalized), new_start + len(probe))
        remapped.append((new_start, new_end, page))
    return remapped


def offset_guess(page_spans: list[tuple[int, int, int]], position: int) -> int:
    previous = [end for _, end, _ in page_spans if end <= position]
    return previous[-1] if previous else 0
