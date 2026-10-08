from __future__ import annotations

from bisect import bisect_right
from hashlib import sha1

from .config import Settings
from .schemas import Hierarchy, Level, Node, ScientificDocument
from .text import normalize, pack_spans, token_estimate


def stable_id(*parts: str) -> str:
    return sha1("|".join(parts).encode("utf-8")).hexdigest()[:20]


def pack(text: str, target: int, overlap: int) -> list[str]:
    """Backward-compatible wrapper returning chunk texts only."""
    return [chunk for chunk, _, _ in pack_spans(text, target, overlap)]


class HierarchyBuilder:
    """Builds Document > Section > Parent > Child nodes with full provenance.

    Every node keeps its character span within its parent text plus the real
    page range resolved from Docling per-item ``page_spans`` metadata, so
    citations can be traced to physical pages rather than defaults.
    """

    def __init__(self, settings: Settings):
        self.settings = settings

    def build(self, documents: list[ScientificDocument]) -> Hierarchy:
        nodes: dict[str, Node] = {}
        child_ids: list[str] = []
        for document in documents:
            parser_version = str(document.metadata.get("parser_version", "unknown"))
            structure = str(document.metadata.get("structure_source", "unknown"))
            section_ids: list[str] = []
            document_evidence: list[str] = []
            document_pages: list[int] = []
            for section_pos, section in enumerate(document.sections):
                section_id = stable_id(
                    document.document_id, "section", str(section_pos), section.title
                )
                section_ids.append(section_id)
                page_spans = self._page_spans(section)
                local_children: list[str] = []
                paragraphs = section.metadata.get("paragraphs") or ()
                for position, (chunk, char_start, char_end) in enumerate(
                    pack_spans(
                        section.text,
                        self.settings.child_target_tokens,
                        self.settings.child_overlap_sentences,
                    )
                ):
                    child_id = stable_id(section_id, "child", str(position), chunk[:96])
                    local_children.append(child_id)
                    child_ids.append(child_id)
                    document_evidence.append(child_id)
                    page_start, page_end = self._pages_for_span(page_spans, char_start, char_end, section)
                    paragraph_ids = tuple(
                        str(paragraph["paragraph_id"])
                        for paragraph in paragraphs
                        if int(paragraph.get("char_start", 0)) < char_end
                        and int(paragraph.get("char_end", 0)) > char_start
                    )
                    paragraph_texts = {
                        str(paragraph["paragraph_id"]): str(paragraph.get("text") or "")
                        for paragraph in paragraphs
                        if str(paragraph.get("paragraph_id") or "") in paragraph_ids
                    }
                    document_pages.extend([page_start, page_end])
                    prefix = f"Document: {document.source}\nSection: {section.title}\n"
                    nodes[child_id] = Node(
                        node_id=child_id, level=Level.CHILD, document_id=document.document_id,
                        source=document.source, text=chunk, embedding_text=prefix + chunk,
                        page_start=page_start, page_end=page_end,
                        section_id=section_id, section_title=section.title,
                        section_type=section.section_type, position=position,
                        token_count=token_estimate(chunk),
                        char_start=char_start, char_end=char_end,
                        confidence=section.confidence,
                        metadata={
                            "parser": "docling",
                            "parser_version": parser_version,
                            "structure_source": structure,
                            "section_position": section_pos,
                            "paragraph_ids": paragraph_ids,
                            "paragraph_texts": paragraph_texts,
                        },
                    )
                parent_ids = self._parents(
                    nodes, document.document_id, document.source, section_id,
                    section.title, section.section_type, local_children, parser_version, structure,
                )
                section_text = normalize(section.text)
                nodes[section_id] = Node(
                    node_id=section_id, level=Level.SECTION, document_id=document.document_id,
                    source=document.source, text=section_text,
                    embedding_text=f"Document: {document.source}\nSection: {section.title}\n{section_text}",
                    page_start=section.page_start, page_end=section.page_end,
                    section_id=section_id, section_title=section.title,
                    section_type=section.section_type, parent_id=document.document_id,
                    child_ids=tuple(parent_ids), evidence_child_ids=tuple(local_children),
                    position=section_pos, token_count=token_estimate(section_text),
                    char_start=0, char_end=len(section_text),
                    confidence=section.confidence,
                    metadata={
                        "parser_version": parser_version,
                        "page_spans": [list(map(int, span)) for span in page_spans],
                        **({"bboxes": section.metadata["bboxes"]}
                           if section.metadata.get("bboxes") else {}),
                    },
                )
            whole = normalize("\n\n".join(section.text for section in document.sections))
            nodes[document.document_id] = Node(
                node_id=document.document_id, level=Level.DOCUMENT,
                document_id=document.document_id, source=document.source, text=whole,
                embedding_text=f"Document: {document.source}\n{whole}",
                page_start=min(document_pages) if document_pages else min(
                    (s.page_start for s in document.sections), default=1),
                page_end=max(document_pages) if document_pages else max(
                    (s.page_end for s in document.sections), default=1),
                child_ids=tuple(section_ids), evidence_child_ids=tuple(document_evidence),
                char_start=0, char_end=len(whole),
                token_count=token_estimate(whole), metadata=document.metadata,
            )
        return Hierarchy(nodes=nodes, child_ids=tuple(child_ids))

    def _parents(
        self, nodes, document_id, source, section_id, title, section_type,
        child_ids, parser_version, structure,
    ):
        size = max(2, self.settings.children_per_parent)
        overlap = min(self.settings.parent_overlap_children, size - 1)
        stride = size - overlap
        parents: list[str] = []
        for pos, start in enumerate(range(0, len(child_ids), stride)):
            group = child_ids[start:start + size]
            if not group:
                continue
            parent_id = stable_id(section_id, "parent", str(pos))
            parents.append(parent_id)
            for child_id in group:
                nodes[child_id] = nodes[child_id].replaced(parent_id=parent_id)
            text = normalize(" ".join(nodes[c].text for c in group))
            nodes[parent_id] = Node(
                node_id=parent_id, level=Level.PARENT, document_id=document_id,
                source=source, text=text, embedding_text=f"Section: {title}\n{text}",
                page_start=min(nodes[c].page_start for c in group),
                page_end=max(nodes[c].page_end for c in group),
                section_id=section_id, section_title=title, section_type=section_type,
                parent_id=section_id, child_ids=tuple(group), evidence_child_ids=tuple(group),
                position=pos, token_count=token_estimate(text),
                char_start=min(nodes[c].char_start for c in group),
                char_end=max(nodes[c].char_end for c in group),
                confidence=min(nodes[c].confidence for c in group),
                metadata={"parser_version": parser_version, "structure_source": structure},
            )
            if start + size >= len(child_ids):
                break
        return parents

    @staticmethod
    def _page_spans(section) -> list[tuple[int, int, int]]:
        raw = section.metadata.get("page_spans") or ()
        spans: list[tuple[int, int, int]] = []
        for entry in raw:
            try:
                start, end, page = int(entry[0]), int(entry[1]), int(entry[2])
            except (TypeError, ValueError, IndexError):
                continue
            spans.append((start, end, page))
        return sorted(spans, key=lambda s: s[0])

    @staticmethod
    def _pages_for_span(
        page_spans: list[tuple[int, int, int]],
        char_start: int,
        char_end: int,
        section,
    ) -> tuple[int, int]:
        if not page_spans:
            return section.page_start, section.page_end
        covered = [
            page
            for start, end, page in page_spans
            if start < char_end and end > char_start
        ]
        if covered:
            return min(covered), max(covered)
        index = bisect_right([s for s, _, _ in page_spans], char_start) - 1
        if index >= 0:
            page = page_spans[index][2]
            return page, page
        return section.page_start, section.page_end
