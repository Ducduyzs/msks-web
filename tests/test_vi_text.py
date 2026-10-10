"""Chuẩn hóa số tiếng Việt + chốt claim cho profile bài giảng (msks.vi_text)."""
from __future__ import annotations

import pytest

from msks.eval_asr import normalize_vi, score
from msks.vi_text import claim_guard, numbers_in, words_to_digits


@pytest.mark.parametrize("text,expected", [
    ("với bảy mươi bài báo mất ba mươi hai giây", "với 70 bài báo mất 32 giây"),
    ("khoảng hai trăm hai mươi token", "khoảng 220 token"),
    ("chênh lệch không phẩy không một một", "chênh lệch 0.011"),
    ("chiếm bốn mươi phần trăm", "chiếm 40%"),
    ("năm hai nghìn không trăm hai mươi lăm", "năm 2025"),
    ("một nghìn hai trăm linh năm", "1205"),
    ("hai mươi mốt sinh viên", "21 sinh viên"),
    ("mười lăm phút", "15 phút"),
    ("khoảng hai trăm rưỡi token", "khoảng 250 token"),
    ("một nghìn rưỡi sinh viên", "1500 sinh viên"),
    ("hai triệu rưỡi đồng", "2500000 đồng"),
])
def test_number_phrases_become_digits(text, expected):
    assert words_to_digits(text) == expected


@pytest.mark.parametrize("text", [
    "Hôm nay học một phương pháp mới",   # "một" là mạo từ
    "trong năm học này",                  # "năm" = year
    "hai bên đồng ý",                     # từ đơn vị đứng riêng: không đổi
    "học lúc hai giờ rưỡi",               # rưỡi sau danh từ đơn vị: giữ nguyên
    "mất một tiếng rưỡi",
])
def test_single_unit_words_are_left_alone(text):
    assert words_to_digits(text) == text


def test_numbers_in_handles_vietnamese_separators():
    assert numbers_in("RAPTOR mất 3.097 giây, chênh lệch 0,011 và 40%") == {"3097", "0.011", "40"}
    assert numbers_in("ba mươi hai giây") == {"32"}


def test_guard_rejects_number_absent_from_evidence():
    assert claim_guard("Mỗi đoạn lá có khoảng 2200 token.", "Mỗi đoạn lá có khoảng hai trăm hai mươi token") == "number_not_in_evidence"
    assert claim_guard("Lập chỉ mục 70 bài báo mất 32 giây.", "với bảy mươi bài báo chỉ mất ba mươi hai giây") is None


def test_guard_rejects_non_inferiority_turned_into_superiority():
    evidence = "Trên PeerQA, Agreement Ranking không kém RAPTOR về độ chính xác."
    assert claim_guard("Agreement Ranking tốt hơn RAPTOR trên PeerQA.", evidence) == "non_inferiority_overclaimed"
    assert claim_guard("Agreement Ranking không kém RAPTOR.", evidence) is None
    assert claim_guard("RAPTOR chậm hơn.", "RAPTOR chậm hơn Agreement Ranking.") is None


def test_asr_metrics_unicode_and_numbers():
    import unicodedata

    assert normalize_vi(unicodedata.normalize("NFD", "Người bệnh, 60%!")) == "người bệnh 60 %"
    s = score("Hằng số k = 60, chạy 32 giây.", "hằng số k bằng sáu mươi chạy 32 giây")
    assert s.words == 7 and s.word_errors == 3 and s.number_recall == 0.5


def test_asr_metrics_vietnamese_thousands_separator():
    # Pipeline chép "3.097 giây" (dấu nghìn kiểu Việt), kịch bản ghi "3097": cùng một con số.
    assert normalize_vi("mất khoảng 3.097 giây") == normalize_vi("mất khoảng 3097 giây") == "mất khoảng 3097 giây"
    assert normalize_vi("chênh lệch 3,5 điểm") == "chênh lệch 3 5 điểm"  # thập phân không bị gộp
    assert normalize_vi("rộng 377 km²") == normalize_vi("rộng 377 km2")
    assert score("rộng 377 km²", "rộng ba trăm bảy mươi bảy km vuông").numbers_ref == 1
    assert score("RAPTOR mất 3097 giây", "Raptor mất 3.097 giây").number_recall == 1.0


@pytest.mark.parametrize("start,end,text,keep,end_out,flags", [
    # Câu bịa đo được 09/10: kéo quá cuối video 53,4 s, chỉ gồm câu kết YouTube → loại.
    (49680, 79660, "Hẹn gặp lại các bạn trong những video tiếp theo.", False, 53400, []),
    (50000, 52000, "Cảm ơn các bạn đã theo dõi.", False, 52000, []),
    (10000, 15000, "Hãy subscribe cho kênh Ghiền Mì Gõ để không bỏ lỡ những video hấp dẫn", False, 15000, []),
    (54000, 56000, "Nội dung bất kỳ", False, 56000, []),                       # bắt đầu sau khi media hết
    (45000, 70000, "Với bài giảng tiếng Việt cần đánh giá lại", True, 53400, ["beyond_media_end"]),
    (40000, 48000, "Tóm lại k bằng 60. Cảm ơn các bạn đã theo dõi.", True, 48000, ["stock_phrase"]),
    (1000, 5000, "Các bạn theo dõi công thức trên bảng", True, 5000, []),     # câu giảng thật: không đụng
])
def test_asr_screen_segment(start, end, text, keep, end_out, flags):
    from msks.media.asr import screen_segment

    assert screen_segment(start, end, text, 53400) == (keep, end_out, flags)


def test_prompt_language_rules_only_for_non_english_profiles():
    from msks.qa import LANGUAGE_RULES, grounded_prompt

    assert LANGUAGE_RULES not in grounded_prompt("q", [], {}, 50)
    assert LANGUAGE_RULES in grounded_prompt("Hằng số k bằng bao nhiêu?", [], {}, 50, "vi")


def test_clean_glossary_limits_and_dedup():
    from msks.media.asr import GLOSSARY_MAX_TERMS, clean_glossary, glossary_prompt

    assert clean_glossary(["  Agreement   Ranking ", "agreement ranking", "SBERT\x00", ""]) == ["Agreement Ranking", "SBERT"]
    assert glossary_prompt(["RAPTOR", "PeerQA"], "toàn cục") == "RAPTOR, PeerQA."
    assert glossary_prompt([], "toàn cục") == "toàn cục" and glossary_prompt(None) is None
    with pytest.raises(ValueError):
        clean_glossary([f"t{i}" for i in range(GLOSSARY_MAX_TERMS + 1)])
    with pytest.raises(ValueError):
        clean_glossary(["x" * 61])
    with pytest.raises(ValueError):
        clean_glossary(["thuật ngữ dài " * 3 + str(i) for i in range(12)])  # > 400 ký tự
