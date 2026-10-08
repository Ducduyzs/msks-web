"""Kiểm tra schema + RLS trên database thật, trong một transaction luôn ROLLBACK.

    python db/verify_schema.py

Không để lại dữ liệu: tạo user/workspace thử, kiểm tra, rồi hoàn tác.
"""
from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _env import migration_url  # noqa: E402

results: list[tuple[bool, str]] = []


def check(ok: bool, label: str) -> None:
    results.append((ok, label))
    print(("  OK  " if ok else " FAIL ") + label)


def expect_error(conn: psycopg.Connection, sql: str, params: tuple, label: str, sqlstate: str) -> None:
    try:
        with conn.transaction():  # savepoint
            conn.execute(sql, params)
    except psycopg.Error as error:
        check(error.sqlstate == sqlstate, f"{label} (sqlstate {error.sqlstate})")
        return
    check(False, f"{label} — không bị chặn")


def as_user(conn: psycopg.Connection, user_id: str | None) -> None:
    if user_id is None:
        conn.execute("set local role anon")
        conn.execute("select set_config('request.jwt.claims', '{\"role\":\"anon\"}', true)")
    else:
        conn.execute("set local role authenticated")
        claims = json.dumps({"sub": user_id, "role": "authenticated"})
        conn.execute("select set_config('request.jwt.claims', %s, true)", (claims,))


def main() -> None:
    conn = psycopg.connect(migration_url(), connect_timeout=20)
    try:
        u1, u2 = str(uuid.uuid4()), str(uuid.uuid4())
        for u in (u1, u2):
            conn.execute(
                "insert into auth.users (id, email, aud, role) values (%s, %s, 'authenticated', 'authenticated')",
                (u, f"verify-{u[:8]}@example.invalid"),
            )
        ws_a = conn.execute("insert into workspace (owner_id, name) values (%s, 'A') returning id", (u1,)).fetchone()[0]
        ws_b = conn.execute("insert into workspace (owner_id, name) values (%s, 'B') returning id", (u2,)).fetchone()[0]

        alias = conn.execute("select next_source_alias(%s)", (ws_a,)).fetchone()[0]
        check(alias == "S1", "next_source_alias cấp S1 cho workspace mới")
        src = conn.execute(
            "insert into source (workspace_id, alias, kind, title, origin_group_id, status) "
            "values (%s, %s, 'pdf', 'Bài A', 'g1', 'ready') returning id",
            (ws_a, alias),
        ).fetchone()[0]
        check(conn.execute("select next_source_alias(%s)", (ws_a,)).fetchone()[0] == "S2", "alias kế tiếp là S2")
        dr = conn.execute(
            "insert into document_revision (workspace_id, source_id, revision_no, mime, object_key, sha256, byte_size) "
            "values (%s, %s, 1, 'application/pdf', 'k', repeat('a', 64), 10) returning id",
            (ws_a, src),
        ).fetchone()[0]
        pr = conn.execute(
            "insert into parse_revision (workspace_id, document_revision_id, parser, parser_version, normalizer_version, "
            "chunker_version, config_hash, page_count, leaf_count, status) "
            "values (%s, %s, 'docling', '2', '1', '1', 'c', 3, 1, 'succeeded') returning id",
            (ws_a, dr),
        ).fetchone()[0]
        node = conn.execute(
            "insert into node (workspace_id, parse_revision_id, legacy_node_id, level, position, text, text_sha256, "
            "token_count, page_start, page_end, char_start, char_end) "
            "values (%s, %s, 'abc', 'child', 0, 'Văn bản 📌', 'h', 3, 1, 1, 0, 9) returning id",
            (ws_a, pr),
        ).fetchone()[0]

        print("Ràng buộc toàn vẹn:")
        expect_error(
            conn,
            "insert into node (workspace_id, parse_revision_id, legacy_node_id, level, position, text, text_sha256, "
            "token_count, char_start, char_end) values (%s, %s, 'x', 'child', 0, 't', 'h', 1, 0, 1)",
            (ws_b, pr),
            "node của workspace B không tham chiếu được parse revision của A",
            "23503",
        )
        expect_error(
            conn,
            "insert into source (workspace_id, alias, kind, title, origin_group_id) values (%s, 'S1', 'pdf', 't', 'g')",
            (ws_a,),
            "alias trùng trong workspace bị từ chối",
            "23505",
        )
        expect_error(
            conn,
            "insert into node (workspace_id, parse_revision_id, legacy_node_id, level, position, text, text_sha256, "
            "token_count, char_start, char_end) values (%s, %s, 'y', 'child', 0, 't', 'h', 1, 5, 2)",
            (ws_a, pr),
            "char_end < char_start bị từ chối",
            "23514",
        )

        run = conn.execute(
            "insert into run (workspace_id, mode, query, config_hash, code_hash, budget_json, created_by) "
            "values (%s, 'qa', 'Câu hỏi thử?', 'c', 'k', '{\"context_tokens\":2048}', %s) returning id",
            (ws_a, u1),
        ).fetchone()[0]
        expect_error(
            conn,
            "update run set status = 'succeeded' where id = %s",
            (run,),
            "run kết thúc bắt buộc có finished_at",
            "23514",
        )
        cb = conn.execute(
            "insert into context_block (workspace_id, run_id, context_id, node_id, rank, utility, visible_text, "
            "visible_sha256, token_count) values (%s, %s, 'C1', %s, 1, 3.2, 'Văn bản', 'h', 3) returning id",
            (ws_a, run, node),
        ).fetchone()[0]
        claim = conn.execute(
            "insert into claim (workspace_id, run_id, ordinal, text, status, verification_method) "
            "values (%s, %s, 0, 'Claim', 'supported', 'nli') returning id",
            (ws_a, run),
        ).fetchone()[0]
        expect_error(
            conn,
            "insert into claim_evidence (workspace_id, claim_id, node_id, role, evidence_snapshot, evidence_sha256, "
            "quote_start, quote_end, support, contradiction, verifier_revision) "
            "values (%s, %s, %s, 'primary', 'x', 'h', 0, 1, 0.9, 0.0, 'v')",
            (ws_a, claim, node),
            "evidence chính thiếu context_block_id bị từ chối",
            "23514",
        )
        expect_error(
            conn,
            "insert into claim (workspace_id, run_id, ordinal, text, status, verification_method) "
            "values (%s, %s, 1, 'Claim', 'rejected', 'nli')",
            (ws_a, run),
            "claim REJECTED bắt buộc có lý do",
            "23514",
        )
        conn.execute(
            "insert into claim_evidence (workspace_id, claim_id, node_id, context_block_id, role, evidence_snapshot, "
            "evidence_sha256, quote_start, quote_end, support, contradiction, verifier_revision) "
            "values (%s, %s, %s, %s, 'primary', 'Văn bản', 'h', 0, 7, 0.9, 0.0, 'v')",
            (ws_a, claim, node, cb),
        )
        seqs = [
            conn.execute("select append_run_event(%s, %s, %s)", (run, kind, json.dumps({}))).fetchone()[0]
            for kind in ("stage", "claim_verified", "done")
        ]
        check(seqs == [1, 2, 3], "append_run_event cấp seq liên tục 1, 2, 3")
        expect_error(
            conn,
            "delete from node where id = %s",
            (node,),
            "không xóa được node đang được run trích dẫn",
            "23503",
        )

        print("Phân quyền (RLS):")
        with conn.transaction():
            as_user(conn, u1)
            check(conn.execute("select count(*) from workspace").fetchone()[0] == 1, "user 1 chỉ thấy workspace của mình")
            check(conn.execute("select count(*) from claim_evidence").fetchone()[0] == 1, "user 1 đọc được evidence của mình")
            expect_error(conn, "select * from run_event", (), "raw run_event chỉ đọc qua FastAPI", "42501")
            expect_error(conn, "insert into workspace (owner_id, name) values (%s, 'X')", (u1,), "user không ghi trực tiếp workspace", "42501")
            expect_error(conn, "update run set cancel_requested = true where id = %s", (run,), "user không sửa run trực tiếp", "42501")
            expect_error(conn, "select * from outbox_event", (), "user không đọc outbox nội bộ", "42501")
            expect_error(conn, "select append_run_event(%s, 'stage', '{}')", (run,), "user không gọi hàm ghi event", "42501")
            conn.execute("reset role")
        with conn.transaction():
            as_user(conn, u2)
            check(conn.execute("select count(*) from source").fetchone()[0] == 0, "user 2 không thấy nguồn của workspace A")
            check(conn.execute("select count(*) from claim").fetchone()[0] == 0, "user 2 không thấy claim của workspace A")
            conn.execute("reset role")
        with conn.transaction():
            as_user(conn, None)
            expect_error(conn, "select * from workspace", (), "anon không đọc được bảng nào", "42501")
            conn.execute("reset role")

        conn.execute("update source set status = 'deleting', deleted_at = now() where id = %s", (src,))
        with conn.transaction():
            as_user(conn, u1)
            check(conn.execute("select count(*) from claim_evidence").fetchone()[0] == 0, "evidence của nguồn đã tombstone bị ẩn")
            check(conn.execute("select count(*) from node").fetchone()[0] == 0, "node của nguồn đã tombstone bị ẩn")
            check(conn.execute("select count(*) from parse_revision").fetchone()[0] == 0, "canonical text của nguồn tombstone bị ẩn")
            check(conn.execute("select count(*) from claim").fetchone()[0] == 1, "claim vẫn còn tham chiếu trong lịch sử")
            conn.execute("reset role")

        check(
            conn.execute("select public from storage.buckets where id = 'documents'").fetchone() == (False,),
            "bucket 'documents' tồn tại và riêng tư",
        )
    finally:
        conn.rollback()
        conn.close()
    failed = [label for ok, label in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} kiểm tra đạt — đã rollback, không để lại dữ liệu.")
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
