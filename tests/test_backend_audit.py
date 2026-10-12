"""Backend regressions; external services are replaced with deterministic fakes."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from msks import api, api_deps, api_media, auth, indexing, qa, storage
from msks.errors import AppError, PermanentError, TransientError
from msks.media import pipeline
from msks.settings import get_settings


@contextmanager
def transaction(conn):
    yield conn


@pytest.mark.parametrize("method", ["post", "delete"])
def test_session_rejects_foreign_origin_before_changing_cookies(monkeypatch, method):
    verify = MagicMock()
    monkeypatch.setattr(api, "verify_token", verify)
    client = TestClient(api.app)
    response = client.request(method, "/api/auth/session", headers={"Origin": "https://evil.invalid"},
                              json={"access_token": "x" * 24} if method == "post" else None)
    assert response.status_code == 403
    assert "set-cookie" not in response.headers
    verify.assert_not_called()


def test_session_allows_configured_frontend(monkeypatch):
    monkeypatch.setattr(api, "verify_token", lambda _: auth.User("u", None, 2_000_000_000))
    response = TestClient(api.app).post("/api/auth/session", headers={"Origin": "http://localhost:5173"},
                                        json={"access_token": "x" * 24})
    assert response.status_code == 200
    assert auth.SESSION_COOKIE in response.cookies


def test_non_ascii_csrf_is_rejected_without_server_error():
    request = Request({"type": "http", "method": "POST", "scheme": "http", "server": ("testserver", 80),
                       "path": "/api/workspaces", "headers": [
                           (b"cookie", b"msks_session=token; csrf_token=expected"),
                           (b"x-csrf-token", b"\xff"),
                       ]})
    with pytest.raises(AppError) as exc:
        auth.current_user(request)
    assert exc.value.status == 403


def test_owned_row_lock_targets_resource_only():
    conn = MagicMock()
    api_deps.require_owned(conn, "source", "00000000-0000-0000-0000-000000000001",
                           auth.User("u", None, 0), "source", lock=True)
    assert "for update of t" in conn.execute.call_args.args[0].lower()


def test_expired_session_never_gets_new_upload_urls(monkeypatch):
    store = MagicMock()
    sizes = MagicMock()
    monkeypatch.setattr(api_media, "_media", lambda: store)
    monkeypatch.setattr(api_media, "_uploaded_sizes", sizes)
    session = dict(id="s", workspace_id="w", status="open", expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
                   filename="v.mp4", mime="video/mp4", expected_bytes=5, part_bytes=5, part_count=1,
                   language="vi", keep_original=False, caption_object_key=None, source_id=None)
    result = api_media.session_dto(session, with_urls=True)
    assert result["status"] == "expired"
    assert "upload_url" not in result["parts"][0]
    sizes.assert_not_called()
    store.signed_upload_url.assert_not_called()
    assert session["status"] == "open"


def test_cancel_cannot_delete_parts_of_completed_session(monkeypatch):
    conn, store = MagicMock(), MagicMock()
    monkeypatch.setattr(api_media.db, "tx", lambda: transaction(conn))
    read = MagicMock(return_value={"status": "completed"})
    monkeypatch.setattr(api_media, "_require_session", read)
    monkeypatch.setattr(api_media, "_media", lambda: store)
    with pytest.raises(AppError) as exc:
        api_media.cancel_media_session("s", auth.User("u", None, 0))
    assert exc.value.code == "session_closed"
    assert read.call_args.kwargs["lock"] is True
    conn.execute.assert_not_called()
    store.delete.assert_not_called()


def test_reindex_rejects_busy_source(monkeypatch):
    conn = MagicMock()
    monkeypatch.setattr(api_media.db, "tx", lambda: transaction(conn))
    read = MagicMock(return_value={"id": "s", "status": "ready"})
    monkeypatch.setattr(api_media, "_require_live_source", read)
    conn.execute.return_value.fetchone.return_value = {"busy": 1}
    with pytest.raises(AppError) as exc:
        api_media.reindex_source("s", api_media.ReindexIn(profile="product"), auth.User("u", None, 0))
    assert exc.value.code == "source_busy"
    assert read.call_args.kwargs["lock"] is True
    assert conn.execute.call_count == 1


def test_cancel_retries_storage_cleanup_after_previous_failure(monkeypatch):
    conn, store = MagicMock(), MagicMock()
    monkeypatch.setattr(api_media.db, "tx", lambda: transaction(conn))
    monkeypatch.setattr(api_media, "_require_session", lambda *a, **kw: {
        "id": "s", "status": "cancelled", "object_prefix": "up/", "part_count": 1, "caption_object_key": None})
    monkeypatch.setattr(api_media, "_media", lambda: store)
    assert api_media.cancel_media_session("s", auth.User("u", None, 0)) == {"status": "cancelled"}
    store.delete.assert_called_once_with(["up/part-0000"])


def test_media_quota_read_is_serialized_before_session_insert(monkeypatch):
    conn = MagicMock()
    monkeypatch.setattr(api_media.db, "tx", lambda: transaction(conn))
    monkeypatch.setattr(api_media, "require_workspace", lambda *a: {})
    monkeypatch.setattr(api_media, "session_dto", lambda *a, **kw: {})

    def usage(c, workspace):
        assert "public.workspace" in c.execute.call_args.args[0]
        assert "for update" in c.execute.call_args.args[0]
        return 0

    monkeypatch.setattr(api_media, "_media_usage", usage)
    api_media.create_media_session("w", api_media.MediaSessionIn(filename="v.mp4", mime="video/mp4", size_bytes=5),
                                    auth.User("u", None, 0))
    assert "insert into public.upload_session" in conn.execute.call_args.args[0]


def test_storage_listing_reads_all_pages_and_subfolders(monkeypatch):
    calls = []

    def post(url, *, headers, json, timeout):
        calls.append((json["prefix"], json["offset"]))
        if json["prefix"] == "root/sub":
            items = [{"name": "nested", "id": "n"}]
        elif json["offset"] == 0:
            items = [{"name": f"f{i}", "id": str(i)} for i in range(999)] + [{"name": "sub", "id": None}]
        else:
            items = [{"name": "last", "id": "last"}]
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: items)

    monkeypatch.setattr(storage.httpx, "post", post)
    keys = storage.BlobStore().list_keys("root")
    assert len(keys) == 1001
    assert "root/sub/nested" in keys and "root/last" in keys
    assert calls == [("root", 0), ("root/sub", 0), ("root", 1000)]


@pytest.mark.parametrize("fails", [True, False])
def test_sweep_records_deletion_only_after_storage_success(monkeypatch, fails):
    conn, store = MagicMock(), MagicMock()
    conn.execute.return_value.fetchall.side_effect = [
        [{"id": "session", "object_prefix": "up/", "part_count": 1, "caption_object_key": None}],
        [{"id": "asset", "object_key": "audio"}],
    ]
    monkeypatch.setattr(pipeline.db, "tx", lambda: transaction(conn))
    monkeypatch.setattr(pipeline, "BlobStore", lambda _: store)

    def delete(keys):
        assert all(not c.args[0].strip().lower().startswith("update") for c in conn.execute.call_args_list)
        assert keys == ["audio", "up/part-0000"]
        if fails:
            raise TransientError("storage unavailable")

    store.delete.side_effect = delete
    if fails:
        with pytest.raises(TransientError):
            pipeline.sweep()
        assert conn.execute.call_count == 2
    else:
        assert pipeline.sweep() == {"sessions_expired": 1, "assets_deleted": 1}
        assert conn.execute.call_count == 4


def test_publish_rechecks_workspace_quota_before_retiring_index(monkeypatch):
    conn, models = MagicMock(), MagicMock()
    models.encode.return_value = ([[1.0]], [{1: 1.0}])
    models.sbert_encode.return_value = [[1.0]]
    monkeypatch.setattr(indexing, "get_models", lambda: models)
    monkeypatch.setattr(indexing.jobs, "guarded", lambda job: transaction(conn))

    def execute(sql, params):
        result = MagicMock()
        if "returning id" in sql:
            result.fetchone.return_value = {"id": "generation"}
        elif "count(*)" in sql:
            result.fetchone.return_value = {"n": 1}
        elif "select deleted_at" in sql:
            result.fetchone.return_value = {"deleted_at": None}
        elif "count(distinct node_id)" in sql:
            result.fetchone.return_value = {"n": get_settings().max_leaves_per_workspace + 1}
        return result

    conn.execute.side_effect = execute
    with pytest.raises(PermanentError) as exc:
        indexing.embed_and_publish({}, "s", "w", "p", [indexing.LeafRow("n", "text", "text")], "product")
    assert exc.value.code == "workspace_leaf_limit"
    sql = [c.args[0] for c in conn.execute.call_args_list]
    assert any("public.workspace" in s and "for update" in s for s in sql)
    assert not any("set state = 'retired'" in s or "set state = 'active'" in s for s in sql)


def test_empty_retrieval_returns_no_context_without_loading_rank_models(monkeypatch):
    settings = get_settings()
    models, conn, persist = MagicMock(), MagicMock(), MagicMock()
    models.rerank.side_effect = AssertionError("empty rerank must not run")
    models.sbert_encode.side_effect = AssertionError("empty scope must not encode")
    monkeypatch.setattr(qa, "get_models", lambda: models)
    monkeypatch.setattr(qa, "guarded", lambda state: transaction(conn))
    monkeypatch.setattr(qa, "emit_stage", lambda *a: None)
    monkeypatch.setattr(qa, "check_alive", lambda *a: None)
    monkeypatch.setattr(qa, "candidate_ids", lambda *a: ([], False))
    monkeypatch.setattr(qa, "load_leaves", lambda *a: {})
    monkeypatch.setattr(qa, "token_counter", lambda *a: len)
    monkeypatch.setattr(qa, "persist", persist)
    qa.run_qa(qa.RunState(run={"id": "r", "config_hash": settings.config_hash(), "query": "What is RRF?",
                              "profile": "product", "budget_json": {"context_tokens": 512},
                              "scope_snapshot_json": {"sources": []}}, job_id="j", attempt=1))
    assert persist.call_args.args[13]["code"] == "no_context"
