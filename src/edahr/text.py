from __future__ import annotations

import math
import re
from collections import Counter


WORD = re.compile(r"\b\w+\b", re.UNICODE)
SENTENCE = re.compile(r"(?<=[.!?])\s+(?=[A-ZÀ-Ỹ0-9(\[])")
STOP = {"a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "in", "is", "it", "of", "on", "or", "that", "the", "this", "to", "was", "with", "các", "có", "của", "được", "là", "một", "những", "theo", "trong", "và"}

_NUMBER_WORDS = {
    "zero": "0", "one": "1", "two": "2", "three": "3", "four": "4", "five": "5",
    "six": "6", "seven": "7", "eight": "8", "nine": "9", "ten": "10",
    "eleven": "11", "twelve": "12", "twenty": "20", "thirty": "30", "forty": "40",
    "fifty": "50", "sixty": "60", "seventy": "70", "eighty": "80", "ninety": "90",
    "hundred": "100", "thousand": "1000",
}


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def tokens(text: str) -> list[str]:
    return [t.casefold() for t in WORD.findall(text) if len(t) > 1 and t.casefold() not in STOP]


def canonical_tokens(text: str) -> list[str]:
    """Content tokens with number words mapped to digits ("six" -> "6")."""
    return [_NUMBER_WORDS.get(token, token) for token in tokens(text)]


def claim_coverage(claim: str, evidence: str) -> float:
    """Multiset share of canonical claim tokens that appear in ``evidence``."""
    claim_counts = Counter(canonical_tokens(claim))
    if not claim_counts:
        return 0.0
    body = Counter(canonical_tokens(evidence))
    hits = sum(min(count, body[token]) for token, count in claim_counts.items())
    return hits / sum(claim_counts.values())


def token_set(text: str) -> set[str]:
    return set(tokens(text))


def sentences(text: str) -> list[str]:
    value = normalize(text)
    return [p.strip() for p in SENTENCE.split(value) if p.strip()] if value else []


def token_estimate(text: str) -> int:
    return max(1, math.ceil(len(WORD.findall(text)) * 1.25))


def overlap(query: str, text: str) -> float:
    q = token_set(query)
    return len(q & token_set(text)) / len(q) if q else 0.0


def density(query: str, text: str) -> float:
    q, body = token_set(query), tokens(text)
    if not q or not body:
        return 0.0
    matches = sum(1 for token in body if token in q)
    return min(1.0, matches / max(1.0, math.sqrt(len(body))))


def jaccard(first: set[str], second: set[str]) -> float:
    union = first | second
    return len(first & second) / len(union) if union else 0.0


def containment(inner: set[str], outer: set[str]) -> float:
    return len(inner & outer) / len(inner) if inner else 0.0


def sentence_spans(text: str) -> tuple[str, list[tuple[int, int]]]:
    """Return normalized text plus (start, end) char spans of each sentence."""
    value = normalize(text)
    spans: list[tuple[int, int]] = []
    cursor = 0
    for part in SENTENCE.split(value):
        if not part.strip():
            cursor += len(part)
            continue
        start = value.find(part, cursor)
        if start < 0:
            start = cursor
        end = start + len(part)
        spans.append((start, end))
        cursor = end
    return value, spans


def pack_spans(
    text: str, target: int, overlap_sentences: int
) -> list[tuple[str, int, int]]:
    """Pack text into ~`target`-token chunks while preserving char offsets.

    Returns (chunk_text, char_start, char_end) with offsets into the
    whitespace-normalized text.
    """
    value, unit_spans = sentence_spans(text)
    if not unit_spans:
        single = (value, 0, len(value))
        return [single] if value else []
    units = [(value[s:e], s, e) for s, e in unit_spans]
    chunks: list[tuple[str, int, int]] = []
    current: list[tuple[str, int, int]] = []
    for unit in units:
        projected = token_estimate(" ".join([item[0] for item in (*current, unit)]))
        if current and projected > target:
            chunks.append((value[current[0][1]:current[-1][2]], current[0][1], current[-1][2]))
            current = current[-overlap_sentences:] if overlap_sentences else []
        current.append(unit)
    if current:
        chunk = (value[current[0][1]:current[-1][2]], current[0][1], current[-1][2])
        if not chunks or chunks[-1] != chunk:
            chunks.append(chunk)
    return chunks


def truncate_to_fit(text: str, max_tokens: int) -> str:
    """Sentence-boundary trim followed by a hard word cut that *guarantees*
    the result fits `max_tokens` estimated tokens."""
    result = truncate_to_tokens(text, max_tokens)
    while result and token_estimate(result) > max_tokens:
        words = WORD.findall(result)
        if len(words) <= 1:
            return ""
        result = " ".join(words[: len(words) - 1])
    return result


def truncate_to_tokens(text: str, max_tokens: int) -> str:
    """Trim text to at most `max_tokens` estimated tokens on a sentence boundary."""
    limit = max(4, int(max_tokens / 1.25))
    if token_estimate(text) <= limit * 1.25:
        return text
    kept: list[str] = []
    count = 0
    for part in sentences(text):
        size = len(WORD.findall(part))
        if count + size > limit:
            if not kept:
                kept.append(" ".join(WORD.findall(part)[:limit]))
                count = limit
            break
        kept.append(part)
        count += size
    result = " ".join(kept).strip()
    return result or " ".join(WORD.findall(text)[:limit])
