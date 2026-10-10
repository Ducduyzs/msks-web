"""Đọc caption SRT/WebVTT và kiểm tra timing (mục 5.1, 5.2).

Parse được không có nghĩa là timing đúng: cue vượt thời lượng media, chồng lấn bất thường hoặc
ngược thứ tự bị gắn cờ `alignment_uncertain`; không tự sửa thời gian.
"""
from __future__ import annotations

import html
import re
from dataclasses import dataclass, field

from ..errors import PermanentError

_TIME = r"(?:(\d{1,2}):)?(\d{1,2}):(\d{2})[,.](\d{1,3})"
_CUE = re.compile(rf"{_TIME}\s*-->\s*{_TIME}")
_TAG = re.compile(r"<[^>]+>")


@dataclass
class Cue:
    start_ms: int
    end_ms: int
    text: str
    flags: list[str] = field(default_factory=list)


def _ms(hours: str | None, minutes: str, seconds: str, millis: str) -> int:
    return ((int(hours or 0) * 60 + int(minutes)) * 60 + int(seconds)) * 1000 + int(millis.ljust(3, "0")[:3])


def parse_captions(data: bytes) -> list[Cue]:
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise PermanentError("caption_invalid", "File phụ đề phải mã hóa UTF-8.") from exc
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    cues: list[Cue] = []
    for block in re.split(r"\n\s*\n", text):
        lines = [line for line in block.split("\n") if line.strip()]
        timing_index = next((i for i, line in enumerate(lines) if _CUE.search(line)), None)
        if timing_index is None:
            continue  # header WEBVTT, NOTE, STYLE, số thứ tự lẻ...
        match = _CUE.search(lines[timing_index])
        start = _ms(*match.groups()[:4])
        end = _ms(*match.groups()[4:])
        body = " ".join(_TAG.sub("", html.unescape(line)).strip() for line in lines[timing_index + 1:])
        body = re.sub(r"\s+", " ", body).strip()
        if body and end > start:
            cues.append(Cue(start, end, body))
    if not cues:
        raise PermanentError("caption_invalid", "Không đọc được cue nào có thời gian hợp lệ trong file phụ đề.")
    return cues


def validate_timing(cues: list[Cue], duration_ms: int | None) -> list[Cue]:
    """Gắn cờ cue nghi lệch thời gian; trả danh sách theo thứ tự thời gian."""
    ordered = sorted(cues, key=lambda c: (c.start_ms, c.end_ms))
    out_of_order = any(a.start_ms > b.start_ms for a, b in zip(cues, cues[1:]))
    for previous, cue in zip([None, *ordered], ordered):
        if duration_ms is not None and cue.end_ms > duration_ms + 2000:
            cue.flags.append("alignment_uncertain")
        if previous and cue.start_ms < previous.end_ms - 1000:
            cue.flags.append("overlapping_cue")
        if out_of_order:
            cue.flags.append("alignment_uncertain")
    return ordered
