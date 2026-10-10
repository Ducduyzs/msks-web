"""Chọn khung hình slide/bảng (mục 5.3).

So mỗi frame lấy mẫu với frame **được chọn gần nhất** (không phải frame liền trước) để bắt được bảng
viết dần: thay đổi nhỏ giữa hai mẫu liên tiếp vẫn cộng dồn thành khác biệt so với frame đã chọn.
Mỗi frame được chọn có khoảng hiển thị [timestamp, frame chọn kế tiếp).

Chỉ số thay đổi = tỉ lệ điểm ảnh đổi rõ rệt (|Δ độ sáng| > PIXEL_DELTA) trên ảnh xám 320×180. Trung bình
độ lệch toàn ảnh không dùng được: slide nền trắng chỉ thêm một dòng chữ cho giá trị gần 0 (đo 09/10).
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np

PIXEL_DELTA = 0.12          # ~30/255: lớn hơn nhiễu nén video, nhỏ hơn nét chữ trên nền
SIGNATURE_SIZE = (320, 180)


@dataclass
class SelectedFrame:
    path: Path
    timestamp_ms: int
    visible_to_ms: int
    change: float


def signature(path: Path) -> np.ndarray:
    import cv2

    image = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
    if image is None:
        raise ValueError(f"không đọc được {path.name}")
    return cv2.resize(image, SIGNATURE_SIZE, interpolation=cv2.INTER_AREA).astype(np.float32) / 255.0


def change_ratio(current: np.ndarray, reference: np.ndarray) -> float:
    return float(np.mean(np.abs(current - reference) > PIXEL_DELTA))


def select_frames(
    samples: list[tuple[Path, int]],
    duration_ms: int,
    threshold: float,
    max_frames: int,
    sig=signature,
) -> tuple[list[SelectedFrame], int | None]:
    """Trả (frame được chọn, mốc ms bắt đầu phần chưa xử lý nếu hết budget frame)."""
    selected: list[SelectedFrame] = []
    reference: np.ndarray | None = None
    incomplete_from: int | None = None
    for path, timestamp in samples:
        current = sig(path)
        change = 1.0 if reference is None else change_ratio(current, reference)
        if reference is not None and change < threshold:
            continue
        if len(selected) >= max_frames:
            incomplete_from = timestamp
            break
        selected.append(SelectedFrame(path, timestamp, duration_ms, change))
        reference = current
    for frame, following in zip(selected, selected[1:]):
        frame.visible_to_ms = following.timestamp_ms
    if incomplete_from is not None and selected:
        selected[-1].visible_to_ms = incomplete_from
    return selected, incomplete_from
