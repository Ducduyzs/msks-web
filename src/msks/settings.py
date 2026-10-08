"""Cấu hình backend (ARCHITECTURE.md mục 13), đọc từ biến môi trường và file .env.

Thứ tự ưu tiên: biến môi trường > .env ở gốc dự án. Không đọc cấu hình frontend.
"""
from __future__ import annotations

import hashlib
import json
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Supabase / Postgres
    database_url: str
    direct_url: str | None = None
    supabase_url: str
    supabase_secret_key: str
    supabase_publishable_key: str | None = None
    supabase_jwks_url: str | None = None
    storage_bucket: str = Field("documents", alias="msks_storage_bucket")

    # --- HTTP / phiên
    cookie_secure: bool = Field(False, alias="msks_cookie_secure")
    cors_origins: str = Field("http://localhost:5173", alias="msks_cors_origins")

    # --- Tính năng sau MVP (mục 1.4, 16)
    enable_synthesis: bool = Field(False, alias="msks_enable_synthesis")
    enable_remote_sources: bool = Field(False, alias="msks_enable_remote_sources")
    enable_corroboration: bool = Field(False, alias="msks_enable_corroboration")

    # --- Model (mục 2, 13)
    device: str = Field("cuda", alias="msks_device")
    use_fp16: bool = True
    embedding_model: str = "BAAI/bge-m3"
    reranker_model: str = "BAAI/bge-reranker-v2-m3"
    sbert_model: str = "sentence-transformers/multi-qa-mpnet-base-cos-v1"
    nli_model: str = "MoritzLaurer/DeBERTa-v3-base-mnli-fever-anli"
    llm_provider: Literal["openai", "none"] = Field("none", alias="msks_llm_provider")
    llm_model: str = Field("gpt-4o-mini", alias="msks_llm_model")
    openai_api_key: str | None = None

    # --- Truy xuất và đóng gói (mục 7.2)
    ranker: Literal["agreement", "cross_encoder"] = "agreement"
    rrf_k: int = 60
    candidate_k: int = 80
    candidate_dense_k: int = 80
    candidate_sparse_k: int = 80
    candidate_rrf_k: int = 60
    full_rerank_max_leaves: int = 600
    final_context_k: int = 8
    context_dedup_threshold: float = 0.85
    knapsack_token_bucket: int = 64
    header_allowance_tokens: int = 24
    max_source_share: float = 0.65
    comparative_min_sources: int = 2
    answer_word_limit: int = 120

    # --- Kiểm chứng (mục 8.1). None = chưa hiệu chỉnh.
    nli_support_threshold: float | None = Field(None, alias="msks_nli_support_threshold", ge=0, le=1)
    nli_contradiction_threshold: float | None = Field(None, alias="msks_nli_contradiction_threshold", ge=0, le=1)
    calibration_artifact: str | None = Field(None, alias="msks_calibration_artifact")
    # Chỉ cho môi trường phát triển: dùng ngưỡng lịch sử v11 khi chưa có calibration, gắn nhãn thử nghiệm.
    allow_uncalibrated: bool = Field(True, alias="msks_allow_uncalibrated")
    claim_confidence_threshold: float = 0.55
    max_children_per_claim: int = 10
    max_evidence_per_claim: int = 1
    evidence_margin: float = 0.05
    sibling_threshold_delta: float = 0.10

    # --- Giới hạn vận hành (mục 3.2, 13)
    max_upload_bytes: int = 50 * 1024 * 1024
    max_pages: int = 300
    max_leaves_per_workspace: int = 20_000
    run_timeout_seconds: int = 180
    max_generated_claims: int = 12
    max_job_attempts: int = 3
    job_lease_seconds: int = 120

    # --- Chia đoạn (giữ nguyên v11)
    child_target_tokens: int = 220
    child_overlap_sentences: int = 1
    children_per_parent: int = 4
    parent_overlap_children: int = 1

    @property
    def migration_url(self) -> str:
        return self.direct_url or self.database_url

    @property
    def thresholds(self) -> tuple[float, float, bool]:
        """(support, contradiction, calibrated). Không có calibration → ngưỡng v11, nhãn thử nghiệm."""
        if self.calibration_artifact and self.nli_support_threshold is not None and self.nli_contradiction_threshold is not None:
            return self.nli_support_threshold, self.nli_contradiction_threshold, True
        if not self.allow_uncalibrated:
            raise RuntimeError("Thiếu calibration artifact cho ngưỡng NLI (msks_calibration_artifact).")
        return (
            self.nli_support_threshold if self.nli_support_threshold is not None else 0.25,
            self.nli_contradiction_threshold if self.nli_contradiction_threshold is not None else 0.50,
            False,
        )

    def public_config(self) -> dict:
        """Cấu hình đã resolve, không có bí mật — dùng cho config_hash và provenance."""
        secret = {"database_url", "direct_url", "supabase_secret_key", "openai_api_key", "supabase_publishable_key"}
        return {k: v for k, v in self.model_dump().items() if k not in secret}

    def config_hash(self) -> str:
        payload = json.dumps(self.public_config(), sort_keys=True, default=str)
        return hashlib.sha256(payload.encode()).hexdigest()[:16]


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]


@lru_cache
def code_hash() -> str:
    """Hash mã nguồn msks + checksum snapshot EDAHR (mục 15 — tái lập)."""
    digest = hashlib.sha256()
    for path in sorted((ROOT / "src" / "msks").rglob("*.py")):
        digest.update(path.read_bytes())
    checksum = ROOT / "src" / "EDAHR_CHECKSUMS.sha256"
    if checksum.exists():
        digest.update(checksum.read_bytes())
    return digest.hexdigest()[:16]


EDAHR_COMMIT = "b28e650d49865fbb9c9c19f350f0a2670061e728"
