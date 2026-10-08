"""Hàng đợi job bền vững trong PostgreSQL (ARCHITECTURE.md mục 4.3).

Thay cho Celery + Redis ở MVP: worker nhận job bằng `FOR UPDATE SKIP LOCKED`, giữ lease có hạn,
gửi heartbeat. Mỗi lần nhận tăng `attempt_version`; mọi ghi kết quả kiểm tra version nên worker
cũ (lease đã hết) không ghi đè được. Lỗi tạm thời → thử lại với backoff + jitter, hữu hạn lần.
"""
from __future__ import annotations

import random
import threading
from contextlib import contextmanager
from typing import Any

from . import db
from .settings import get_settings


class LeaseLost(Exception):
    """An obsolete worker must stop without changing the new attempt."""


@contextmanager
def guarded(job: dict, *, states: tuple[str, ...] = ("running",)):
    with db.tx() as conn:
        row = conn.execute(
            "select attempt_version, state, lease_until > clock_timestamp() as live "
            "from public.job where id = %s for update", (job["id"],),
        ).fetchone()
        if (not row or row["attempt_version"] != job["attempt_version"]
                or row["state"] not in states or (row["state"] == "running" and not row["live"])):
            raise LeaseLost()
        yield conn

CLAIM_SQL = """
update public.job j
set state = 'running', attempt_version = j.attempt_version + 1, heartbeat_at = now(),
    lease_until = now() + make_interval(secs => %(lease)s), updated_at = now()
where j.id = (
  select id from public.job
  where attempt_version < max_attempts and (
       (state = 'queued' and (lease_until is null or lease_until <= now()))
       or (state = 'running' and lease_until < now()))
  order by created_at
  for update skip locked
  limit 1
)
returning j.*
"""


def claim_next() -> dict[str, Any] | None:
    with db.tx() as conn:
        # Crashes do not execute fail(), so retry_count alone cannot cap them.
        exhausted = conn.execute(
            """update public.job set state = 'failed', lease_until = null,
                   error_code = 'attempts_exhausted', error_json = %s
               where id in (
                   select id from public.job where state in ('running', 'queued')
                     and attempt_version >= max_attempts and lease_until <= now()
                   order by created_at for update skip locked limit 20
               ) returning *""",
            (db.jsonb({"code": "attempts_exhausted", "message": "Job hết số lần thử sau khi mất lease.", "retryable": False}),),
        ).fetchall()
        for job in exhausted:
            append_job_event(conn, job, "failed", job["error_json"])
            if job["kind"] == "run":
                run = conn.execute(
                    """update public.run set status = case when cancel_requested then 'cancelled' else 'failed' end,
                       error_code = 'attempts_exhausted', error_json = %s, finished_at = now()
                       where id = %s and status in ('queued', 'running') returning id, status""",
                    (db.jsonb(job["error_json"]), job["target_id"]),
                ).fetchone()
                if run:
                    db.append_run_event(conn, str(run["id"]), "done", {"status": run["status"]})
            elif job["kind"] == "ingest":
                conn.execute(
                    "update public.source set status = 'failed', error_json = %s where id = %s and deleted_at is null",
                    (db.jsonb(job["error_json"]), job["target_id"]),
                )
        return conn.execute(CLAIM_SQL, {"lease": get_settings().job_lease_seconds}).fetchone()


def heartbeat(job_id: str, attempt: int) -> bool:
    with db.tx() as conn:
        row = conn.execute(
            """update public.job set heartbeat_at = now(), lease_until = now() + make_interval(secs => %s)
               where id = %s and attempt_version = %s and state = 'running'
                 and lease_until > clock_timestamp() returning id""",
            (get_settings().job_lease_seconds, job_id, attempt),
        ).fetchone()
    return row is not None


def append_job_event(conn, job: dict, event_type: str, payload: dict) -> None:
    conn.execute(
        """insert into public.job_event (workspace_id, job_id, seq, event_type, payload_json)
           select %s, %s, coalesce(max(seq), 0) + 1, %s, %s from public.job_event where job_id = %s""",
        (job["workspace_id"], job["id"], event_type, db.jsonb(payload), job["id"]),
    )


def set_stage(job: dict, stage: str) -> None:
    with guarded(job) as conn:
        conn.execute(
            "update public.job set stage = %s where id = %s and attempt_version = %s",
            (stage, job["id"], job["attempt_version"]),
        )
        append_job_event(conn, job, "stage", {"stage": stage})


def complete(job: dict) -> None:
    with guarded(job) as conn:
        conn.execute(
            "update public.job set state = 'succeeded', lease_until = null where id = %s and attempt_version = %s",
            (job["id"], job["attempt_version"]),
        )
        append_job_event(conn, job, "succeeded", {})


def fail(job: dict, code: str, message: str, retryable: bool) -> bool:
    """Ghi lỗi; trả True nếu job được xếp lại để thử tiếp."""
    error = {"code": code, "message": message, "retryable": retryable}
    retry = retryable and job["attempt_version"] < job["max_attempts"]
    backoff = min(300, 5 * 2 ** job["retry_count"]) * (0.5 + random.random())
    with guarded(job) as conn:
        conn.execute(
            """update public.job set state = %s, retry_count = retry_count + %s, error_code = %s, error_json = %s,
               lease_until = case when %s then now() + make_interval(secs => %s) else null end
               where id = %s and attempt_version = %s""",
            ("queued" if retry else "failed", 1 if retryable else 0, code, db.jsonb(error),
             retry, backoff, job["id"], job["attempt_version"]),
        )
        append_job_event(conn, job, "retry" if retry else "failed", error)
    return retry


class Heartbeat:
    """Gia hạn lease định kỳ trong lúc xử lý job dài (parse PDF, nạp model)."""

    def __init__(self, job: dict, interval: float = 20.0):
        self.job, self.interval = job, interval
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        while not self.stop.wait(self.interval):
            try:
                if not heartbeat(str(self.job["id"]), self.job["attempt_version"]):
                    return
            except Exception:  # mạng chập chờn: lần sau thử lại; lease hết thì job được thu hồi
                continue

    def __enter__(self) -> "Heartbeat":
        self.thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.stop.set()
