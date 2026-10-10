"""Alignment theo thời gian + canonical text + source map (LECTURE_ARCHITECTURE.md mục 5.4, 6).

Canonical text gồm các section **một modality**: lời giảng/caption theo cửa sổ thời gian (cắt tại lúc
slide đổi hoặc khi quá dài) và chữ trên bảng theo từng frame. Không dùng LLM viết lại: text là bản chép
gốc đã chuẩn hóa khoảng trắng (đúng như edahr.text.normalize), nên mọi item có khoảng code point chính
xác trong canonical text → source map nhiều-nhiều tới segment/region/asset/thời gian.

Gần nhau về thời gian chỉ tạo **alignment_edge**, không phải quan hệ hỗ trợ evidence.
"""
from __future__ import annotations

import bisect
from dataclasses import dataclass, field

from edahr.schemas import DocumentSection, ScientificDocument
from edahr.text import normalize

ALIGNMENT_POLICY = "temporal-overlap-v1"
SECTION_POLICY = "slide-window-v1"


@dataclass
class SpeechItem:
    id: str
    text: str
    start_ms: int
    end_ms: int
    origin: str                     # human_caption | auto_caption | asr | manual_edit
    flags: list[str] = field(default_factory=list)
    reviewed: bool = False
    audio_asset_id: str | None = None


@dataclass
class BoardItem:
    id: str
    frame_asset_id: str
    timestamp_ms: int
    visible_from_ms: int
    visible_to_ms: int
    bbox: list[float]
    text: str
    confidence: float
    flags: list[str] = field(default_factory=list)
    reviewed: bool = False
    included: bool = True           # False: dưới ngưỡng tin cậy → không vào canonical


@dataclass
class Span:
    char_start: int
    char_end: int
    target_type: str                # segment | region
    target_id: str
    target_char_start: int
    target_char_end: int
    start_ms: int
    end_ms: int
    asset_id: str | None
    bbox: list[float] | None
    precision: str
    modality: str
    flags: list[str]
    reviewed: bool


@dataclass
class SectionPlan:
    title: str
    modality: str                   # speech | caption | board
    start_ms: int
    end_ms: int
    items: list[SpeechItem | BoardItem]


def fmt_ms(ms: int) -> str:
    seconds = ms // 1000
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes:02d}:{secs:02d}"


def speech_modality(origin: str) -> str:
    return "caption" if origin in ("human_caption", "auto_caption") else "speech"


def plan_sections(speech: list[SpeechItem], board: list[BoardItem], slide_starts: list[int], window_ms: int) -> list[SectionPlan]:
    """Cửa sổ lời giảng cắt tại mốc đổi slide/bảng hoặc khi vượt `window_ms`; mỗi frame có chữ là một section."""
    plans: list[SectionPlan] = []
    boundaries = sorted(set(slide_starts))
    current: list[SpeechItem] = []
    current_slot = None

    def flush() -> None:
        if current:
            modality = speech_modality(current[0].origin)
            label = "Phụ đề" if modality == "caption" else "Lời giảng"
            start, end = current[0].start_ms, current[-1].end_ms
            plans.append(SectionPlan(f"{label} {fmt_ms(start)}–{fmt_ms(end)}", modality, start, end, list(current)))
            current.clear()

    for item in sorted(speech, key=lambda s: (s.start_ms, s.end_ms)):
        if not normalize(item.text):
            continue
        slot = bisect.bisect_right(boundaries, item.start_ms)
        too_long = current and item.end_ms - current[0].start_ms > window_ms
        modality_changed = current and speech_modality(current[0].origin) != speech_modality(item.origin)
        if current and (slot != current_slot or too_long or modality_changed):
            flush()
        current.append(item)
        current_slot = slot
    flush()

    by_frame: dict[str, list[BoardItem]] = {}
    for region in board:
        if region.included and normalize(region.text):
            by_frame.setdefault(region.frame_asset_id, []).append(region)
    for regions in by_frame.values():
        first = regions[0]
        plans.append(SectionPlan(
            f"Bảng/slide {fmt_ms(first.timestamp_ms)}", "board",
            min(r.visible_from_ms for r in regions), max(r.visible_to_ms for r in regions), regions,
        ))
    order = {"speech": 0, "caption": 0, "board": 1}
    plans.sort(key=lambda p: (p.start_ms, order[p.modality]))
    return plans


def build_document(plans: list[SectionPlan], document_id: str, alias: str, language: str | None
                   ) -> tuple[ScientificDocument, list[Span], str]:
    """ScientificDocument cho edahr.HierarchyBuilder + source map theo code point của canonical text.

    Canonical text = các section (đã chuẩn hóa) nối bằng "\\n\\n" — đúng quy ước của msks.ingest.build_tree.
    """
    sections: list[DocumentSection] = []
    spans: list[Span] = []
    texts: list[str] = []
    offset = 0
    for plan in plans:
        parts: list[str] = []
        cursor = 0
        for item in plan.items:
            text = normalize(item.text)
            if parts:
                cursor += 1  # dấu cách nối hai item
            start = cursor
            cursor += len(text)
            parts.append(text)
            if isinstance(item, SpeechItem):
                spans.append(Span(offset + start, offset + cursor, "segment", item.id, 0, len(text), item.start_ms,
                                  item.end_ms, item.audio_asset_id, None, "segment", plan.modality, list(item.flags),
                                  item.reviewed))
            else:
                spans.append(Span(offset + start, offset + cursor, "region", item.id, 0, len(text), item.visible_from_ms,
                                  item.visible_to_ms, item.frame_asset_id, item.bbox, "region", "board", list(item.flags),
                                  item.reviewed))
        section_text = " ".join(parts)
        assert normalize(section_text) == section_text, "section text phải ở dạng đã chuẩn hóa"
        texts.append(section_text)
        sections.append(DocumentSection(
            title=plan.title, text=section_text, page_start=1, page_end=1,
            section_type=f"lecture_{plan.modality}",
            metadata={"modality": plan.modality, "start_ms": plan.start_ms, "end_ms": plan.end_ms},
        ))
        offset += len(section_text) + 2
    canonical = "\n\n".join(texts)
    document = ScientificDocument(document_id=document_id, source=alias, sections=tuple(sections),
                                  metadata={"parser": "msks-lecture", "parser_version": SECTION_POLICY, "language": language})
    return document, spans, canonical


def spans_in(spans: list[Span], char_start: int, char_end: int) -> list[Span]:
    return [s for s in spans if s.char_start < char_end and s.char_end > char_start]


def node_extras(nodes, spans: list[Span]) -> dict[str, dict]:
    """modality + khoảng thời gian cho từng node từ các span giao với nó (nhiều-nhiều, không ép một khoảng)."""
    out: dict[str, dict] = {}
    for node in nodes:
        hits = spans_in(spans, node.char_start, node.char_end)
        if not hits:
            continue
        modalities = {s.modality for s in hits}
        out[node.legacy_node_id] = {
            "modality": modalities.pop() if len(modalities) == 1 else None,
            "start_ms": min(s.start_ms for s in hits),
            "end_ms": max(s.end_ms for s in hits),
        }
    return out


def locate(spans: list[Span], char_start: int, char_end: int) -> dict:
    """Locator cho một evidence: mọi segment/region giao với đoạn được trích, kèm thời gian/asset/bbox."""
    hits = spans_in(spans, char_start, char_end)
    if not hits:
        return {"items": [], "precision": "none"}
    return {
        "start_ms": min(s.start_ms for s in hits),
        "end_ms": max(s.end_ms for s in hits),
        "precision": "segment" if all(s.target_type == "segment" for s in hits) else "region" if all(
            s.target_type == "region" for s in hits) else "mixed",
        "items": [
            {"type": s.target_type, "id": s.target_id, "start_ms": s.start_ms, "end_ms": s.end_ms,
             "asset_id": s.asset_id, "bbox": s.bbox, "flags": s.flags, "reviewed": s.reviewed}
            for s in hits
        ],
    }


def alignment_edges(speech: list[SpeechItem], board: list[BoardItem]) -> list[tuple[str, str, float]]:
    """(segment, region, tỉ lệ thời lượng segment trùng khoảng hiển thị của region)."""
    edges = []
    for segment in speech:
        length = max(1, segment.end_ms - segment.start_ms)
        for region in board:
            if not region.included:
                continue
            overlap = min(segment.end_ms, region.visible_to_ms) - max(segment.start_ms, region.visible_from_ms)
            if overlap > 0:
                edges.append((segment.id, region.id, round(overlap / length, 4)))
    return edges


def coverage(speech: list[SpeechItem], board: list[BoardItem], duration_ms: int | None, *, frames_selected: int,
             visual_incomplete_from: int | None, dropped_segments: int, has_audio: bool, has_video: bool,
             captions_used: bool) -> dict:
    merged = 0
    last_end = -1
    for item in sorted(speech, key=lambda s: s.start_ms):
        start = max(item.start_ms, last_end)
        if item.end_ms > start:
            merged += item.end_ms - start
        last_end = max(last_end, item.end_ms)
    flagged = sum(1 for s in speech if any(f in s.flags for f in ("low_confidence", "possible_repetition", "alignment_uncertain")))
    included = [r for r in board if r.included]
    return {
        "duration_ms": duration_ms,
        "speech_ms": merged,
        "speech_ratio": round(merged / duration_ms, 4) if duration_ms else None,
        "segments": len(speech),
        "segments_flagged": flagged,
        "flagged_ratio": round(flagged / len(speech), 4) if speech else None,
        "segments_dropped_as_non_speech": dropped_segments,
        "frames_selected": frames_selected,
        "board_regions": len(included),
        "board_regions_low_confidence": len(board) - len(included),
        "visual_incomplete_from_ms": visual_incomplete_from,
        "audio_read": has_audio and not captions_used,
        "captions_used": captions_used,
        "visual_read": has_video,
    }
