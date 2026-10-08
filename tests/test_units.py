"""Unit test cho logic thuần của backend (không cần DB, GPU hay mạng)."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from edahr.schemas import DocumentSection, ScientificDocument

from msks.api import decode_cursor, encode_cursor
from msks.db import libpq_url
from msks.errors import AppError, PermanentError
from msks.ingest import _page_partition, build_tree, fingerprint_hex, jaccard_estimate, minhash, parse, sniff
from msks.ml import sparse_literal, vector_literal
from msks.qa import answer_status
from msks.settings import get_settings
from edahr.schemas import Claim, Generation


def test_libpq_url_strips_prisma_params():
    url = "postgresql://u:p@host:6543/postgres?pgbouncer=true&sslmode=require"
    assert libpq_url(url) == "postgresql://u:p@host:6543/postgres?sslmode=require"


def test_sniff_checks_real_content():
    assert sniff("a.pdf", b"%PDF-1.7 ...") == ("pdf", "application/pdf")
    assert sniff("a.md", "# Tiêu đề".encode()) == ("markdown", "text/markdown")
    with pytest.raises(PermanentError):
        sniff("fake.pdf", b"not a pdf")
    with pytest.raises(PermanentError):
        sniff("a.txt", b"\xff\xfe\x00bad")
    with pytest.raises(PermanentError):
        sniff("a.exe", b"MZ")


def test_leaf_offsets_are_code_points_into_canonical_text():
    text = ("# Mở đầu\n\n😀 Emoji ở đầu.   Khoảng   trắng lặp.\n\n"
            + " ".join(f"Câu thứ {i} về Agreement Ranking." for i in range(80))
            + "\n\n# Kết quả\n\nKết quả cuối cùng 𝔸 ngoài BMP.")
    document, parser, version = parse("markdown", text.encode(), "t", "doc", "S1", 300)
    tree = build_tree(document, get_settings(), False, parser, version)
    assert len(tree.leaves) > 2
    for leaf in tree.leaves:
        assert tree.canonical_text[leaf.char_start:leaf.char_end] == leaf.text
        assert leaf.page_start is None
    assert tree.pages is None and tree.page_count is None
    levels = {n.level for n in tree.nodes}
    assert levels == {"document", "section", "parent", "child"}


def test_page_partition_covers_text_without_gaps():
    sections = (
        DocumentSection("A", "x" * 100, page_start=1, page_end=2, metadata={"page_spans": [[0, 60, 1], [60, 100, 2]]}),
        DocumentSection("B", "y" * 50, page_start=3, page_end=3),
    )
    document = ScientificDocument("d", "S1", sections)
    blocks = _page_partition(document, [0, 102], 152)
    assert [b["page"] for b in blocks] == [1, 2, 3]
    assert blocks[0]["char_start"] == 0 and blocks[-1]["char_end"] == 152
    assert all(a["char_end"] == b["char_start"] for a, b in zip(blocks, blocks[1:]))


def test_minhash_detects_near_duplicates_only():
    base = " ".join(f"word{i}" for i in range(400))
    near = base + " extra tail words here"
    other = " ".join(f"other{i}" for i in range(400))
    assert jaccard_estimate(fingerprint_hex(minhash(base)), fingerprint_hex(minhash(near))) >= 0.8
    assert jaccard_estimate(fingerprint_hex(minhash(base)), fingerprint_hex(minhash(other))) < 0.2


def test_pgvector_literals():
    import numpy as np

    assert vector_literal(np.array([0.5, -1.0], dtype=np.float32)) == "[0.5,-1]"
    # sparsevec dùng chỉ số bắt đầu từ 1; bỏ trọng số 0
    assert sparse_literal({0: 0.25, 9: 0.0, 4: 1.5}) == "{1:0.25,5:1.5}/250002"


def test_cursor_round_trip_and_rejects_garbage():
    created = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)
    row_id = "6f1c2c1e-6c7a-4d5a-9a43-2c3b1f0e9b11"
    assert decode_cursor(encode_cursor(created, row_id)) == (created.isoformat(), row_id)
    assert decode_cursor(None) is None
    with pytest.raises(AppError):
        decode_cursor("not-a-cursor")


def test_answer_status_never_passes_failures_as_answered():
    claim = Claim("c", ("C1",), 0.9)
    assert answer_status(Generation(True, (claim,)), 1, None, False) == "answered"
    assert answer_status(Generation(True, (claim,)), 0, None, False) == "insufficient_evidence"
    assert answer_status(Generation(True, (claim,)), 1, None, True) == "verification_unavailable"
    invalid = Generation(False, validation_errors=("provider_invalid_json",))
    assert answer_status(invalid, 0, None, False) == "model_output_invalid"
    assert answer_status(Generation(False), 0, {"code": "comparative_missing_side"}, False) == "insufficient_evidence"


def test_uncalibrated_thresholds_are_flagged():
    support, contradiction, calibrated = get_settings().thresholds
    assert (support, contradiction) == (0.25, 0.5)
    assert calibrated is False
