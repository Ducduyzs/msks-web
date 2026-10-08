"""Regression tests for privacy, stale workers, cancellation and citation spans.

Database transactions are faked here; SQL locking/RLS still needs integration
verification against a disposable PostgreSQL/Supabase instance.
"""
import base64
import json
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from msks import dto, jobs, qa
from msks.api import decode_cursor, idempotent_job
from msks.errors import AppError, PermanentError
from msks.settings import Settings


def fake_transaction(conn):
    @contextmanager
    def transaction(*args, **kwargs):
        yield conn
    return transaction


@pytest.mark.parametrize("row", [
    None,
    {"attempt_version": 2, "state": "running", "live": True},
    {"attempt_version": 1, "state": "running", "live": False},
    {"attempt_version": 1, "state": "succeeded", "live": True},
    {"attempt_version": 1, "state": "queued", "live": True},
])
def test_obsolete_worker_cannot_emit_completion(monkeypatch, row):
    conn = MagicMock()
    conn.execute.return_value.fetchone.return_value = row
    monkeypatch.setattr(jobs.db, "tx", fake_transaction(conn))
    with pytest.raises(jobs.LeaseLost):
        jobs.complete({"id": "j", "attempt_version": 1})
    assert conn.execute.call_count == 1  # only ownership read; no status/event writes


def test_live_attempt_can_complete(monkeypatch):
    conn = MagicMock()
    conn.execute.return_value.fetchone.return_value = {"attempt_version": 1, "state": "running", "live": True}
    monkeypatch.setattr(jobs.db, "tx", fake_transaction(conn))
    jobs.complete({"id": "j", "workspace_id": "w", "attempt_version": 1})
    assert conn.execute.call_count == 3


def test_crash_attempts_count_towards_retry_limit(monkeypatch):
    conn = MagicMock()
    monkeypatch.setattr(jobs, "guarded", fake_transaction(conn))
    assert jobs.fail({"id": "j", "workspace_id": "w", "attempt_version": 3,
                      "max_attempts": 3, "retry_count": 0}, "error", "failed", True) is False


def test_sse_replay_rehydrates_deleted_evidence(monkeypatch):
    conn = MagicMock()
    conn.execute.return_value.fetchall.return_value = [{"id": "c"}]
    safe_claim = {"id": "c", "evidence": [{"source_deleted": True, "evidence_snapshot": ""}]}
    monkeypatch.setattr(dto, "claims_with_evidence", lambda conn, rows: ([safe_claim], []))
    row = {"seq": 4, "event_type": "claim_verified",
           "payload_json": {"claim": {"id": "c", "evidence": [{"evidence_snapshot": "PRIVATE OLD QUOTE"}]}}}
    event = dto.run_event_dto(conn, "r", row)
    assert event["claim"] == safe_claim
    assert "PRIVATE OLD QUOTE" not in json.dumps(event)
    assert event["seq"] == 4


def test_sse_missing_claim_never_falls_back_to_old_payload(monkeypatch):
    conn = MagicMock()
    conn.execute.return_value.fetchall.return_value = []
    monkeypatch.setattr(dto, "claims_with_evidence", lambda conn, rows: ([], []))
    event = dto.run_event_dto(conn, "r", {
        "seq": 1, "event_type": "claim_verified", "payload_json": {"claim": {"id": "c", "text": "OLD"}},
    })
    assert event["type"] == "stage"
    assert "OLD" not in json.dumps(event)


def test_cancel_after_verification_prevents_publication(monkeypatch):
    conn = MagicMock()
    conn.execute.return_value.fetchone.return_value = {"cancel_requested": True, "status": "running"}
    monkeypatch.setattr(qa, "guarded", fake_transaction(conn))
    state = qa.RunState(run={"id": "r"}, job_id="j", attempt=1)
    with pytest.raises(qa.Cancelled):
        qa.persist(state, "factoid", [], {}, {}, {}, {}, qa.Generation(False), qa.Generation(False),
                   {}, [], {}, "", None, False, 512)
    assert conn.execute.call_count == 1


def test_truncated_unicode_evidence_highlights_only_visible_quote():
    leaf = SimpleNamespace(text="😀 Intro. Exact fact. Hidden text.", char_start=100, char_end=132)
    # Derive end explicitly in code points, not JS UTF-16 units.
    leaf.char_end = leaf.char_start + len(leaf.text)
    block = SimpleNamespace(text="Exact fact.", char_start=109, char_end=120)
    assert qa.evidence_span("Exact fact.", block, leaf) == (109, 120)
    with pytest.raises(PermanentError):
        qa.evidence_span("Hidden text.", block, leaf)


def test_evidence_with_incorrect_offset_is_rejected():
    leaf = SimpleNamespace(text="Visible. Hidden.", char_start=0, char_end=16)
    block = SimpleNamespace(text="Visible.", char_start=1, char_end=9)
    with pytest.raises(PermanentError):
        qa.evidence_span("Visible.", block, leaf)


def test_idempotency_checks_conflicts_after_locking():
    conn = MagicMock()
    conn.execute.return_value.fetchone.return_value = {"request_hash": "first"}
    with pytest.raises(AppError) as caught:
        idempotent_job(conn, "w", "run", "same-key", "second")
    assert caught.value.code == "idempotency_conflict"
    assert "pg_advisory_xact_lock" in conn.execute.call_args_list[0].args[0]


@pytest.mark.parametrize("timestamp", ["not-a-date", "2026-10-08T12:00:00"])
def test_cursor_rejects_invalid_timestamp_before_database(timestamp):
    cursor = base64.urlsafe_b64encode(f"{timestamp}|6f1c2c1e-6c7a-4d5a-9a43-2c3b1f0e9b11".encode()).decode()
    with pytest.raises(AppError):
        decode_cursor(cursor)


def test_zero_threshold_is_not_silently_replaced():
    settings = Settings(_env_file=None, msks_nli_support_threshold=0, msks_nli_contradiction_threshold=0)
    assert settings.thresholds == (0, 0, False)


def test_threshold_outside_probability_range_is_rejected():
    with pytest.raises(ValueError):
        Settings(_env_file=None, msks_nli_support_threshold=1.1)
