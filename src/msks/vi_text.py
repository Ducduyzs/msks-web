"""Chuẩn hóa số tiếng Việt và chốt kiểm tra claim cho profile bài giảng (LECTURE_ARCHITECTURE.md mục 7).

Đo 09/10 (analysis/nli_vi_lecture.json): mDeBERTa cho điểm gần như chỉ 0 hoặc 1, nên chỉnh ngưỡng không sửa được
lỗi; lỗi chính đến từ số viết bằng chữ ↔ chữ số ("bảy mươi" vs "70" bị coi là mâu thuẫn; "hai trăm hai mươi" vs
"2200" bị coi là hỗ trợ) và từ việc suy "không kém" thành "tốt hơn". Module này:

* `words_to_digits`: đổi **cụm** số viết bằng chữ sang chữ số trước khi chạy NLI (không đổi text đã lưu).
  Một từ đơn vị đứng riêng ("một phương pháp", "năm học", "hai bên") không bị đổi để tránh đổi nhầm nghĩa.
* `claim_guard`: chốt sau NLI — claim có con số không xuất hiện trong evidence, hoặc khẳng định "hơn/vượt" khi
  evidence chỉ nói "không kém/tương đương", bị loại (abstain), không publish.
"""
from __future__ import annotations

import re
import unicodedata

UNITS = {"không": 0, "một": 1, "mốt": 1, "hai": 2, "ba": 3, "bốn": 4, "tư": 4, "năm": 5, "lăm": 5,
         "sáu": 6, "bảy": 7, "bẩy": 7, "tám": 8, "chín": 9}
SCALES = {"nghìn": 1_000, "ngàn": 1_000, "triệu": 1_000_000, "tỷ": 1_000_000_000, "tỉ": 1_000_000_000}
MULTIPLIERS = {"mươi", "mười", "trăm", *SCALES}
FILLERS = {"linh", "lẻ"}
NUMBER_WORDS = set(UNITS) | MULTIPLIERS | FILLERS | {"phẩy", "rưỡi"}
# "rưỡi" chỉ là số khi ngay sau trăm/nghìn/triệu/tỷ: "hai trăm rưỡi" = 250, "một nghìn rưỡi" = 1500.
# "hai giờ rưỡi", "một tiếng rưỡi" (rưỡi sau danh từ đơn vị) giữ nguyên — đổi sẽ sai nghĩa.
HALF_AFTER = {"trăm", *SCALES}
# Từ đơn vị chỉ được hiểu là số khi đi trong cụm có hệ số (mươi/trăm/nghìn...) — "mốt", "lăm", "tư" sau "mươi".
_TOKEN = re.compile(r"\w+|\s+|[^\w\s]", re.UNICODE)


def _phrase_value(words: list[str]) -> str | None:
    if "phẩy" in words:
        index = words.index("phẩy")
        whole = _phrase_value(words[:index]) if words[:index] else "0"
        decimals = [str(UNITS[w]) for w in words[index + 1:] if w in UNITS]
        if whole is None or not decimals or len(decimals) != len(words[index + 1:]):
            return None
        return f"{whole}.{''.join(decimals)}"
    total = current = pending = 0
    seen_unit = False
    last_scale = None
    for word in words:
        if word in UNITS:
            pending = UNITS[word]
            seen_unit = True
        elif word == "mươi":
            current += (pending or 1) * 10
            pending = 0
        elif word == "mười":
            current += 10
        elif word == "trăm":
            current += (pending if seen_unit else 1) * 100
            pending = 0
            last_scale = 100
        elif word in SCALES:
            total += (current + pending or 1) * SCALES[word]
            current = pending = 0
            last_scale = SCALES[word]
        elif word == "rưỡi":
            if last_scale is None or pending:
                return None
            if last_scale == 100:
                current += 50
            else:
                total += last_scale // 2
        elif word in FILLERS:
            continue
        else:
            return None
    return str(total + current + pending)


def words_to_digits(text: str) -> str:
    """Đổi các cụm số viết bằng chữ (có hệ số hoặc ≥ 2 từ số) sang chữ số; "phần trăm" → "%"."""
    text = unicodedata.normalize("NFC", text)
    text = re.sub(r"\bphần\s+trăm\b", "%", text, flags=re.IGNORECASE)
    tokens = _TOKEN.findall(text)
    out: list[str] = []
    i = 0
    while i < len(tokens):
        if tokens[i].lower() in NUMBER_WORDS:
            j, words, end = i, [], i
            while j < len(tokens):
                if tokens[j].lower() in NUMBER_WORDS:
                    word = tokens[j].lower()
                    # Hai từ đơn vị liền nhau không phải cú pháp số ("năm hai nghìn…" = "năm" (year) + số),
                    # trừ phần thập phân sau "phẩy": dừng cụm trước từ thứ hai.
                    if words and words[-1] in UNITS and word in UNITS and "phẩy" not in words:
                        break
                    if word == "rưỡi" and (not words or words[-1] not in HALF_AFTER):
                        break
                    words.append(word)
                    end = j
                    j += 1
                elif tokens[j].isspace() and j + 1 < len(tokens) and tokens[j + 1].lower() in NUMBER_WORDS:
                    j += 1
                else:
                    break
            is_number = len(words) >= 2 or (words and words[0] in MULTIPLIERS)
            value = _phrase_value(words) if is_number else None
            if value is not None and words[-1] != "phẩy":
                out.append(value)
                i = end + 1
                continue
        out.append(tokens[i])
        i += 1
    return re.sub(r"(\d)\s+%", r"\1%", "".join(out))


_NUMBER = re.compile(r"\d+(?:[.,]\d+)*")


def numbers_in(text: str) -> set[str]:
    """Các con số đã chuẩn hóa: "3.097" (dấu nghìn) → "3097"; "0,011" (thập phân) → "0.011"."""
    found = set()
    for raw in _NUMBER.findall(words_to_digits(text)):
        if re.fullmatch(r"\d{1,3}(\.\d{3})+", raw):
            value = raw.replace(".", "")
        else:
            value = raw.replace(",", ".")
        value = value.rstrip("0").rstrip(".") if "." in value else value.lstrip("0") or "0"
        found.add(value)
    return found


_SUPERIORITY = re.compile(r"\b(tốt hơn|cao hơn|nhanh hơn|vượt|vượt trội|hơn hẳn|better|outperform)", re.IGNORECASE)
_NON_INFERIORITY = re.compile(r"\b(không kém|không thua|tương đương|ngang bằng|non-inferior|not worse)", re.IGNORECASE)


def claim_guard(claim: str, evidence: str) -> str | None:
    """Lý do loại claim (None nếu qua). Bảo thủ: thà abstain còn hơn publish số/so sánh không có trong nguồn."""
    missing = numbers_in(claim) - numbers_in(evidence)
    if missing:
        return "number_not_in_evidence"
    if (_SUPERIORITY.search(claim) and _NON_INFERIORITY.search(evidence)
            and not _SUPERIORITY.search(evidence) and not _NON_INFERIORITY.search(claim)):
        return "non_inferiority_overclaimed"
    return None
