"""Unit test phần bài giảng (LECTURE_ARCHITECTURE.md): không cần DB, GPU, FFmpeg hay mạng."""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from msks.api_media import youtube_id
from msks.errors import AppError, PermanentError
from msks.ingest import build_tree
from msks.media.captions import parse_captions, validate_timing
from msks.media.frames import select_frames
from msks.media.pipeline import final_status
from msks.media.timeline import (
    BoardItem,
    SpeechItem,
    alignment_edges,
    build_document,
    coverage,
    fmt_ms,
    locate,
    node_extras,
    plan_sections,
)
from msks.profiles import choose_run_profile, get_profile
from msks.qa import extraction_needs_review, transcription_state
from msks.settings import get_settings

SRT = """1
00:00:01,000 --> 00:00:04,500
Xin chào các bạn, hôm nay ta học <i>Agreement Ranking</i>.

2
00:00:05,000 --> 00:00:09,000
RRF dùng k = 60 &amp; không cần cây tóm tắt.
""".encode()

VTT = """WEBVTT

NOTE bình luận

00:01.000 --> 00:03.250
Câu thứ nhất.

00:01:10.000 --> 00:01:12.000
Cue vượt thời lượng video.
""".encode()


def test_parse_srt_strips_tags_and_entities():
    cues = parse_captions(SRT)
    assert [(c.start_ms, c.end_ms) for c in cues] == [(1000, 4500), (5000, 9000)]
    assert cues[0].text == "Xin chào các bạn, hôm nay ta học Agreement Ranking."
    assert cues[1].text == "RRF dùng k = 60 & không cần cây tóm tắt."


def test_vtt_timing_beyond_duration_is_flagged_not_fixed():
    cues = validate_timing(parse_captions(VTT), duration_ms=30_000)
    assert cues[0].flags == []
    assert "alignment_uncertain" in cues[1].flags and cues[1].start_ms == 70_000


def test_captions_without_valid_cue_rejected():
    with pytest.raises(PermanentError):
        parse_captions(b"WEBVTT\n\nkhong co cue nao")


def _board(lines: int) -> np.ndarray:
    """Ảnh 180×320 nền trắng; mỗi "dòng chữ" là một dải 3 hàng điểm ảnh đen (≈1,7% ảnh)."""
    image = np.ones((180, 320), dtype=np.float32)
    for line in range(lines):
        image[20 + line * 10:23 + line * 10, 20:300] = 0.0
    return image


def test_frame_selection_catches_incremental_board_writing():
    # Bảng viết dần: mỗi 5 s thêm nửa dòng (< ngưỡng), nhưng so với frame ĐÃ CHỌN thì cộng dồn vượt ngưỡng.
    base = _board(2)
    frames = [base.copy() for _ in range(6)]
    for i in range(1, 6):
        frames[i][60:60 + i, 20:300] = 0.0  # mỗi mẫu thêm một hàng (0,49% < ngưỡng 0,8%)
    samples = [(Path(f"f{i}.jpg"), i * 5000) for i in range(6)]
    lookup = {p: f for (p, _), f in zip(samples, frames)}
    selected, incomplete = select_frames(samples, 30_000, 0.008, 100, sig=lambda p: lookup[p])
    assert [f.timestamp_ms for f in selected] == [0, 10_000, 20_000]
    assert [f.visible_to_ms for f in selected] == [10_000, 20_000, 30_000]
    assert incomplete is None


def test_new_text_line_on_white_slide_is_a_change_but_compression_noise_is_not():
    from msks.media.frames import change_ratio

    slide = _board(2)
    noisy = np.clip(slide + np.random.default_rng(0).normal(0, 0.03, slide.shape).astype(np.float32), 0, 1)
    assert change_ratio(noisy, slide) < 0.002            # nhiễu nén ~3% độ sáng không tính là thay đổi
    assert change_ratio(_board(3), slide) > 0.002        # thêm một dòng chữ là thay đổi


def test_frame_budget_reports_incomplete_instead_of_silently_dropping():
    samples = [(Path(f"f{i}.jpg"), i * 5000) for i in range(5)]
    lookup = {p: _board(i + 1) for i, (p, _) in enumerate(samples)}
    selected, incomplete = select_frames(samples, 25_000, 0.002, 2, sig=lambda p: lookup[p])
    assert len(selected) == 2 and incomplete == 10_000 and selected[-1].visible_to_ms == 10_000


def _items():
    speech = [
        SpeechItem("s1", "Hôm nay   ta học Agreement Ranking 😀.", 0, 4000, "asr", []),
        SpeechItem("s2", "Nó dùng RRF với k bằng 60.", 4000, 9000, "asr", ["low_confidence"]),
        SpeechItem("s3", "Sang slide hai: chi phí lập chỉ mục.", 12_000, 16_000, "asr", []),
    ]
    board = [
        BoardItem("r1", "frame-a", 0, 0, 11_000, [10, 10, 200, 40], "Agreement Ranking: RRF k = 60", 0.8),
        BoardItem("r2", "frame-b", 11_000, 11_000, 20_000, [10, 10, 200, 40], "Index: 32 s vs 3097 s", 0.75),
        BoardItem("r3", "frame-b", 11_000, 11_000, 20_000, [10, 50, 200, 80], "chữ mờ", 0.2, ["low_confidence"], False, False),
    ]
    return speech, board


def test_source_map_is_exact_in_code_points_and_single_modality():
    speech, board = _items()
    plans = plan_sections(speech, board, [0, 11_000], window_ms=180_000)
    # Theo thời gian; lời giảng bị cắt tại lúc đổi slide (11 s) thành hai section.
    assert [(p.modality, p.start_ms) for p in plans] == [("speech", 0), ("board", 0), ("board", 11_000), ("speech", 12_000)]
    document, spans, canonical = build_document(plans, "doc", "S1", "vi")
    tree = build_tree(document, get_settings(), False, "msks-lecture", "test")
    assert tree.canonical_text == canonical
    for span in spans:
        target = next(i for i in [*speech, *board] if i.id == span.target_id)
        assert canonical[span.char_start:span.char_end] == " ".join(target.text.split())
    assert all(s.target_id != "r3" for s in spans)  # vùng dưới ngưỡng tin cậy không vào canonical
    for leaf in tree.leaves:
        assert canonical[leaf.char_start:leaf.char_end] == leaf.text
    extras = node_extras(tree.nodes, spans)
    for leaf in tree.leaves:
        assert extras[leaf.legacy_node_id]["modality"] in ("speech", "board")


def test_locate_returns_all_items_overlapping_quote():
    speech, board = _items()
    plans = plan_sections(speech, board, [0, 11_000], window_ms=180_000)
    _, spans, canonical = build_document(plans, "doc", "S1", "vi")
    start = canonical.index("Hôm nay")
    end = canonical.index("60.") + 3
    locator = locate(spans, start, end)
    assert [i["id"] for i in locator["items"]] == ["s1", "s2"]
    assert (locator["start_ms"], locator["end_ms"], locator["precision"]) == (0, 9000, "segment")
    assert locate(spans, 10**6, 10**6 + 5)["items"] == []


def test_alignment_is_temporal_overlap_only():
    speech, board = _items()
    edges = {(s, r): score for s, r, score in alignment_edges(speech, board)}
    assert edges[("s1", "r1")] == 1.0
    assert ("s3", "r2") in edges and ("s3", "r1") not in edges
    assert all(r != "r3" for _, r in edges)  # vùng bị loại không tạo alignment


def test_final_status_reflects_capability_and_quality():
    full = {"audio_available": True, "visual_available": True, "captions_available": False}
    assert final_status({"flagged_ratio": 0.1}, full, 0.5) == "ready"
    assert final_status({"flagged_ratio": 0.8}, full, 0.5) == "review_required"
    assert final_status({"flagged_ratio": 0.0, "visual_incomplete_from_ms": 60_000}, full, 0.5) == "ready_limited"
    caption_only = {"audio_available": False, "visual_available": False, "captions_available": True}
    assert final_status({"flagged_ratio": 0.0}, caption_only, 0.5) == "ready_limited"


def test_coverage_merges_overlapping_speech():
    speech = [SpeechItem("a", "x", 0, 5000, "asr"), SpeechItem("b", "y", 3000, 8000, "asr", ["low_confidence"])]
    cov = coverage(speech, [], 10_000, frames_selected=0, visual_incomplete_from=None, dropped_segments=1,
                   has_audio=True, has_video=False, captions_used=False)
    assert cov["speech_ms"] == 8000 and cov["speech_ratio"] == 0.8 and cov["flagged_ratio"] == 0.5


def test_numeric_claim_on_flagged_unreviewed_transcript_needs_review():
    flagged = {"items": [{"flags": ["low_confidence"], "reviewed": False}]}
    reviewed = {"items": [{"flags": ["low_confidence"], "reviewed": True}]}
    assert extraction_needs_review("RRF dùng k = 60", [flagged]) is True
    assert extraction_needs_review("RRF dùng k = 60", [reviewed]) is False
    assert extraction_needs_review("Agreement Ranking không cần cây", [flagged]) is False
    assert transcription_state(flagged) == "automatic" and transcription_state(reviewed) == "reviewed"
    assert transcription_state(None) == "not_applicable"


@pytest.mark.parametrize("url", [
    "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
    "https://youtu.be/dQw4w9WgXcQ?t=42",
    "https://www.youtube.com/shorts/dQw4w9WgXcQ",
    "https://m.youtube.com/watch?feature=share&v=dQw4w9WgXcQ",
    "dQw4w9WgXcQ",
])
def test_youtube_id_variants(url):
    assert youtube_id(url) == "dQw4w9WgXcQ"


@pytest.mark.parametrize("url", ["https://evil.example/watch?v=dQw4w9WgXcQ", "https://youtu.be/short", "; rm -rf /"])
def test_youtube_id_rejects_other_hosts_and_garbage(url):
    with pytest.raises(AppError):
        youtube_id(url)


def test_profiles_never_mix_and_lecture_uses_multilingual_models():
    assert choose_run_profile(["pdf", "markdown"], None) == "product"
    assert choose_run_profile(["pdf", "video"], None) == "lecture_vi_v1"
    lecture = get_profile("lecture_vi_v1")
    assert "multilingual" in lecture.sbert_model and "mDeBERTa" in lecture.nli_model
    assert lecture.calibrated is False and lecture.digest() != get_profile("product").digest()
    with pytest.raises(AppError):
        choose_run_profile(["pdf"], "unknown")


def test_fmt_ms():
    assert fmt_ms(754_000) == "12:34" and fmt_ms(3_723_000) == "1:02:03"
