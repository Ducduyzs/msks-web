"""Đo chất lượng bản chép lời giảng (LECTURE_ARCHITECTURE.md mục 11 — lớp ASR/caption).

Chuẩn hóa trước khi so: Unicode NFKC (dấu tiếng Việt dạng tổ hợp và dựng sẵn phải bằng nhau; "²" = "2"), chữ thường,
bỏ dấu câu/ký hiệu, gộp khoảng trắng. Không đổi số ↔ chữ: "60" và "sáu mươi" bị tính là khác nhau, vì
claim số liệu phụ thuộc đúng cách ghi số — báo riêng tỉ lệ giữ đúng con số.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass


def normalize_vi(text: str) -> str:
    # NFKC: dấu tiếng Việt tổ hợp = dựng sẵn (như NFC) và "km²" = "km2" (chỉ số trên tương thích).
    text = unicodedata.normalize("NFKC", text).lower()
    # Dấu nghìn: "3.097" (kiểu Việt) và "3,097" đều là 3097 — nếu không, một phía bị tách thành "3 097" (đo 09/10).
    text = re.sub(r"(?<=\d)[.,](?=\d{3}(?!\d))", "", text)
    chars = []
    for ch in text:
        category = unicodedata.category(ch)
        if ch == "%" or not (category.startswith("P") or category.startswith("S")):
            chars.append(ch)
        else:
            chars.append(" ")
    # Dấu phẩy/chấm thập phân ("3,5") thành khoảng trắng ở trên; giữ % như một token.
    text = "".join(chars).replace("%", " % ")
    return re.sub(r"\s+", " ", text).strip()


def edit_distance(ref: list, hyp: list) -> int:
    previous = list(range(len(hyp) + 1))
    for i, r in enumerate(ref, 1):
        current = [i] + [0] * len(hyp)
        for j, h in enumerate(hyp, 1):
            current[j] = min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (r != h))
        previous = current
    return previous[-1]


@dataclass
class Score:
    word_errors: int
    words: int
    char_errors: int
    chars: int
    numbers_ref: int
    numbers_hit: int

    def __add__(self, other: "Score") -> "Score":
        return Score(*(a + b for a, b in zip(self.__dict__.values(), other.__dict__.values())))

    @property
    def wer(self) -> float:
        return self.word_errors / max(1, self.words)

    @property
    def cer(self) -> float:
        return self.char_errors / max(1, self.chars)

    @property
    def number_recall(self) -> float | None:
        return self.numbers_hit / self.numbers_ref if self.numbers_ref else None


def score(reference: str, hypothesis: str) -> Score:
    ref, hyp = normalize_vi(reference), normalize_vi(hypothesis)
    ref_words, hyp_words = ref.split(), hyp.split()
    # Chỉ token toàn chữ số: "km²"/"km2" là đơn vị, không phải con số cần giữ (đo 09/10: bị đếm nhầm là số).
    ref_numbers = [w for w in ref_words if w.isdecimal()]
    available = list(hyp_words)
    hit = 0
    for number in ref_numbers:
        if number in available:
            available.remove(number)
            hit += 1
    return Score(
        word_errors=edit_distance(ref_words, hyp_words), words=len(ref_words),
        char_errors=edit_distance(list(ref.replace(" ", "")), list(hyp.replace(" ", ""))), chars=len(ref.replace(" ", "")),
        numbers_ref=len(ref_numbers), numbers_hit=hit,
    )
