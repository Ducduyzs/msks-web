"""ASR bằng faster-whisper (mục 5.2), model có sẵn — không tự huấn luyện.

Timestamp theo segment (không hứa word-level). VAD bỏ đoạn im lặng; không điều kiện hóa trên câu
trước để giảm lặp/bịa. Đoạn nghi bịa trên vùng im lặng bị loại và đếm vào coverage, không âm thầm giữ.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from ..errors import TransientError

LOW_LOGPROB = -1.0
HIGH_COMPRESSION = 2.4
SILENCE_NO_SPEECH = 0.8
END_TOLERANCE_MS = 1000

# Câu Whisper hay bịa ở đuôi im lặng (học từ phụ đề YouTube). Đo 09/10 trên bài giảng tiếng Việt tổng hợp: turbo thêm
# "Hẹn gặp lại các bạn trong những video tiếp theo." với avg_logprob -0.27, no_speech 0.0 — ngưỡng tin cậy không bắt
# được — và segment kết thúc ở 79,7 s trong khi video dài 53,4 s.
STOCK_PHRASES = re.compile(
    r"hẹn gặp lại (?:các bạn )?(?:trong|ở) (?:những |các )?(?:video|clip|tập) (?:tiếp theo|sau)"
    r"|cảm ơn (?:các bạn )?(?:đã )?(?:theo dõi|xem video|lắng nghe)(?: video)?"
    r"|(?:hãy |nhớ )?(?:subscribe|đăng ký)(?: cho)? kênh[\w ]*?(?= |$)"
    r"|ghiền mì gõ|để không bỏ lỡ những video hấp dẫn"
    r"|thanks? (?:you )?for watching|please subscribe|subtitles by the amara\.org community",
)


GLOSSARY_MAX_TERMS = 40
GLOSSARY_MAX_TERM_CHARS = 60
# Whisper chỉ giữ ~224 token prompt; glossary dài còn dễ làm model chèn thuật ngữ vào đoạn im lặng.
GLOSSARY_MAX_CHARS = 400


def clean_glossary(terms: list[str]) -> list[str]:
    """Thuật ngữ cho `initial_prompt`: bỏ ký tự điều khiển, gộp khoảng trắng, bỏ trùng (không phân biệt hoa thường).
    Vượt giới hạn → ValueError (API trả 422), không âm thầm cắt."""
    out, seen = [], set()
    for raw in terms:
        term = " ".join("".join(ch for ch in unicodedata.normalize("NFC", raw)
                                if unicodedata.category(ch)[0] != "C").split())
        if not term or term.casefold() in seen:
            continue
        if len(term) > GLOSSARY_MAX_TERM_CHARS:
            raise ValueError(f"Thuật ngữ dài quá {GLOSSARY_MAX_TERM_CHARS} ký tự: {term[:30]}…")
        seen.add(term.casefold())
        out.append(term)
    if len(out) > GLOSSARY_MAX_TERMS:
        raise ValueError(f"Tối đa {GLOSSARY_MAX_TERMS} thuật ngữ.")
    if len(", ".join(out)) > GLOSSARY_MAX_CHARS:
        raise ValueError(f"Glossary dài quá {GLOSSARY_MAX_CHARS} ký tự; giữ các thuật ngữ hay bị chép sai nhất.")
    return out


def glossary_prompt(terms: list[str] | None, fallback: str | None = None) -> str | None:
    """Glossary của workspace nếu có, không thì `MSKS_ASR_GLOSSARY` toàn cục. Đo 09/10 trên bài giảng mẫu:
    WER 5,8 % → 3,6 % với 7 thuật ngữ (analysis/vi_lecture_eval.md)."""
    return ", ".join(terms) + "." if terms else (fallback or None)


def _plain(text: str) -> str:
    text = unicodedata.normalize("NFC", text).lower()
    return " ".join(re.sub(r"[^\w\s.]", " ", text).replace(".", " ").split())


def screen_segment(start_ms: int, end_ms: int, text: str, duration_ms: int | None) -> tuple[bool, int, list[str]]:
    """(giữ?, end_ms đã kẹp, cờ thêm). Loại segment bắt đầu sau khi media hết, hoặc chỉ gồm câu bịa quen thuộc;
    segment vượt quá cuối media bị kẹp và gắn cờ (claim số liệu trên đó bị loại, như các cờ chất lượng khác)."""
    flags = []
    if duration_ms is not None:
        if start_ms >= duration_ms:
            return False, end_ms, []
        if end_ms > duration_ms + END_TOLERANCE_MS:
            end_ms = duration_ms
            flags.append("beyond_media_end")
    plain = _plain(text)
    if STOCK_PHRASES.search(plain):
        if not STOCK_PHRASES.sub(" ", plain).strip():
            return False, end_ms, []
        flags.append("stock_phrase")
    return True, end_ms, flags


@dataclass
class Segment:
    start_ms: int
    end_ms: int
    text: str
    avg_logprob: float
    no_speech_prob: float
    compression_ratio: float
    flags: list[str] = field(default_factory=list)


@dataclass
class AsrResult:
    segments: list[Segment]
    language: str | None
    language_probability: float | None
    dropped: int
    model: str


def quality_flags(avg_logprob: float, no_speech_prob: float, compression_ratio: float) -> list[str]:
    flags = []
    if avg_logprob < LOW_LOGPROB:
        flags.append("low_confidence")
    if compression_ratio > HIGH_COMPRESSION:
        flags.append("possible_repetition")
    if no_speech_prob > 0.5:
        flags.append("possible_non_speech")
    return flags


def load_ctranslate2_cuda() -> None:
    """CTranslate2 cần cuBLAS/cuDNN của CUDA 12. Trên Windows các DLL này đi kèm torch: import torch trước
    để thêm thư mục DLL, nếu không faster-whisper lỗi `cublas64_12.dll is not found` (đo 09/10)."""
    try:
        import torch  # noqa: F401
    except ImportError:  # pragma: no cover — máy chỉ có CUDA toolkit hệ thống
        pass


def transcribe(wav: Path, model_name: str, device: str, compute_type: str, language: str | None,
               glossary: str | None, duration_ms: int | None = None) -> AsrResult:
    load_ctranslate2_cuda()
    from faster_whisper import WhisperModel

    try:
        model = WhisperModel(model_name, device="cuda" if device.startswith("cuda") else "cpu",
                             compute_type=compute_type if device.startswith("cuda") else "int8")
    except RuntimeError as exc:  # thiếu VRAM / CUDA bận → thử lại sau
        raise TransientError(f"ASR: không nạp được model ({exc})") from exc
    try:
        iterator, info = model.transcribe(
            str(wav),
            language=None if language in (None, "auto") else language,
            vad_filter=True,
            condition_on_previous_text=False,
            beam_size=5,
            initial_prompt=glossary or None,
        )
        segments: list[Segment] = []
        dropped = 0
        for item in iterator:
            text = " ".join(item.text.split())
            if not text:
                continue
            # Nghi bịa trên vùng im lặng: xác suất không-phải-lời cao VÀ độ tin thấp.
            if item.no_speech_prob > SILENCE_NO_SPEECH and item.avg_logprob < LOW_LOGPROB:
                dropped += 1
                continue
            start, end = int(round(item.start * 1000)), int(round(item.end * 1000))
            keep, end, extra = screen_segment(start, end, text, duration_ms)
            if not keep:
                dropped += 1
                continue
            if end <= start:
                end = start + 1
            segments.append(Segment(start, end, text, item.avg_logprob, item.no_speech_prob, item.compression_ratio,
                                    quality_flags(item.avg_logprob, item.no_speech_prob, item.compression_ratio) + extra))
        return AsrResult(segments, info.language, info.language_probability, dropped, model_name)
    finally:
        del model
