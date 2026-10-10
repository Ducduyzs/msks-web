"""OCR vùng chữ trên frame (mục 5.3) — adapter, chọn engine bằng cấu hình.

Mỗi dòng là một vùng có bbox theo pixel **frame gốc** (đổi ngược phép scale lúc lấy mẫu), text thô và
độ tin. Vùng dưới ngưỡng tin cậy vẫn được lưu để xem lại nhưng không đưa vào canonical text.
VLM chưa bật trong MVP: không có mô tả sinh thêm nào được dùng làm evidence.

Đo ngày 09/10 trên slide tiếng Việt: RapidOCR (PP-OCRv6 mặc định) **bỏ mất chữ có dấu** ("với" → "vi")
nhưng vẫn báo độ tin 0,97–0,99; EasyOCR (vi+en) đọc đúng dấu, độ tin 0,70–0,80. Vì vậy mặc định là
EasyOCR; độ tin của hai engine không cùng thang nên ngưỡng gắn theo engine.
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

MIN_CONFIDENCE = {"easyocr": 0.4, "rapidocr": 0.5}


@dataclass
class Region:
    bbox: list[float]
    text: str
    confidence: float


@lru_cache
def _easyocr(languages: tuple[str, ...], gpu: bool):
    import easyocr

    return easyocr.Reader(list(languages), gpu=gpu, verbose=False)


@lru_cache
def _rapidocr():
    from rapidocr import RapidOCR

    return RapidOCR()


def languages_for(language: str | None) -> tuple[str, ...]:
    # Ngôn ngữ lời giảng không quyết định chữ trên slide (giảng tiếng Anh, slide tiếng Việt và ngược lại);
    # model tiếng Việt của EasyOCR đọc được cả chữ Latin không dấu.
    return ("vi", "en")


def read_frame(path: Path, scale: float, engine: str = "easyocr", language: str | None = None, gpu: bool = True) -> list[Region]:
    """`scale` = chiều rộng frame lấy mẫu / chiều rộng video gốc."""
    raw: list[tuple[list, str, float]] = []
    if engine == "easyocr":
        for box, text, score in _easyocr(languages_for(language), gpu).readtext(str(path)):
            raw.append((box, text, float(score)))
    elif engine == "rapidocr":
        result = _rapidocr()(str(path))
        if getattr(result, "boxes", None) is not None:
            raw = [(box, text, float(score)) for box, text, score in zip(result.boxes, result.txts, result.scores)]
    else:
        raise ValueError(f"OCR engine không hỗ trợ: {engine}")
    regions: list[Region] = []
    for box, text, score in raw:
        text = " ".join(str(text).split())
        if not text:
            continue
        xs = [float(p[0]) / scale for p in box]
        ys = [float(p[1]) / scale for p in box]
        regions.append(Region([round(min(xs), 1), round(min(ys), 1), round(max(xs), 1), round(max(ys), 1)], text, score))
    # Thứ tự đọc: trên → dưới, trái → phải (gom dòng theo nửa chiều cao chữ).
    regions.sort(key=lambda r: (round(r.bbox[1] / max(1.0, (r.bbox[3] - r.bbox[1]) / 2)), r.bbox[0]))
    return regions


def release() -> None:
    _easyocr.cache_clear()
    _rapidocr.cache_clear()


def engine_version(engine: str) -> str:
    try:
        module = __import__(engine)
        return f"{engine}-{getattr(module, '__version__', 'unknown')}"
    except ImportError:  # pragma: no cover
        return f"{engine}-missing"
