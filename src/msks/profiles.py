"""Profile model (LECTURE_ARCHITECTURE.md mục 7).

Mỗi index generation thuộc đúng một profile; query và leaf luôn được encode cùng profile, không trộn
embedding hay ngưỡng giữa các profile. `product` giữ nguyên cấu hình MVP tài liệu (tiếng Anh);
`lecture_vi_v1` dùng SBERT/NLI đa ngôn ngữ — là **ứng viên đánh giá**, chưa có calibration.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass

from .errors import AppError
from .settings import Settings, get_settings

LECTURE_KINDS = ("video", "youtube", "drive")


@dataclass(frozen=True)
class ModelProfile:
    name: str
    language: str
    embedding_model: str
    reranker_model: str
    sbert_model: str
    nli_model: str
    support_threshold: float
    contradiction_threshold: float
    calibrated: bool
    calibration_artifact: str | None

    def digest(self) -> str:
        return hashlib.sha256(json.dumps(asdict(self), sort_keys=True).encode()).hexdigest()[:16]

    @property
    def embedding_revision(self) -> str:
        return f"{self.embedding_model}+{self.sbert_model}"


def _lecture_thresholds(settings: Settings) -> tuple[float, float, bool]:
    if (settings.lecture_calibration_artifact and settings.lecture_nli_support_threshold is not None
            and settings.lecture_nli_contradiction_threshold is not None):
        return settings.lecture_nli_support_threshold, settings.lecture_nli_contradiction_threshold, True
    if not settings.allow_uncalibrated:
        raise RuntimeError("Thiếu calibration artifact cho profile lecture_vi_v1.")
    support = settings.lecture_nli_support_threshold
    contradiction = settings.lecture_nli_contradiction_threshold
    return (support if support is not None else 0.25, contradiction if contradiction is not None else 0.50, False)


def get_profile(name: str, settings: Settings | None = None) -> ModelProfile:
    settings = settings or get_settings()
    if name == "product":
        support, contradiction, calibrated = settings.thresholds
        return ModelProfile(
            "product", "en", settings.embedding_model, settings.reranker_model, settings.sbert_model, settings.nli_model,
            support, contradiction, calibrated, settings.calibration_artifact if calibrated else None,
        )
    if name == "lecture_vi_v1":
        support, contradiction, calibrated = _lecture_thresholds(settings)
        return ModelProfile(
            "lecture_vi_v1", "vi+en", settings.embedding_model, settings.reranker_model,
            "sentence-transformers/paraphrase-multilingual-mpnet-base-v2",
            "MoritzLaurer/mDeBERTa-v3-base-mnli-xnli",
            support, contradiction, calibrated, settings.lecture_calibration_artifact if calibrated else None,
        )
    raise AppError(422, "unknown_profile", f"Profile {name!r} không tồn tại.")


PROFILE_NAMES = ("product", "lecture_vi_v1")


def default_index_profile(kind: str) -> str:
    return "lecture_vi_v1" if kind in LECTURE_KINDS else "product"


def choose_run_profile(kinds: list[str], requested: str | None) -> str:
    """Có bài giảng trong phạm vi → profile đa ngôn ngữ; ngược lại giữ profile tài liệu."""
    if requested:
        get_profile(requested)
        return requested
    return "lecture_vi_v1" if any(k in LECTURE_KINDS for k in kinds) else "product"
