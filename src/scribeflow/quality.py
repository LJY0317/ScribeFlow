"""Read-only Korean transcript comparison with exact character error rate.

This module never calls OpenAI or changes the user's source files.
CER is Levenshtein distance divided by reference character count. Text is
NFC-normalized, case-folded, with whitespace/punctuation and the provenance
SHA-256 footer ignored; these choices measure spoken content rather than layout.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable

_SOURCE_SHA_FOOTER = re.compile(
    r"\s*<!-- ScribeFlow SHA256: [0-9a-f]{64} -->\s*$"
)


def comparable_text(text: str) -> str:
    if not isinstance(text, str):
        raise TypeError("전사문은 텍스트여야 합니다.")
    stripped = _SOURCE_SHA_FOOTER.sub("", text)
    normalized = unicodedata.normalize("NFC", stripped).casefold()
    return "".join(
        char for char in normalized
        if not char.isspace() and not unicodedata.category(char).startswith("P")
    )


def edit_distance(a: str, b: str) -> int:
    """Exact bit-parallel Myers Levenshtein distance, linear text scans.

    Python arbitrary-precision integers represent the bit vector, so even
    long Korean recordings avoid quadratic memory and per-cell Python loops.
    """
    if len(a) > len(b):
        a, b = b, a
    if not a:
        return len(b)
    width = len(a)
    mask = (1 << width) - 1
    highest = 1 << (width - 1)
    bitmasks: dict[str, int] = {}
    for offset, char in enumerate(a):
        bitmasks[char] = bitmasks.get(char, 0) | (1 << offset)

    positive = mask
    negative = 0
    distance = width
    for char in b:
        equal = bitmasks.get(char, 0)
        x = equal | negative
        zero = ((((equal & positive) + positive) ^ positive) | equal | negative) & mask
        plus = negative | ~(zero | positive)
        minus = positive & zero
        if plus & highest:
            distance += 1
        if minus & highest:
            distance -= 1
        plus = ((plus << 1) | 1) & mask
        minus = (minus << 1) & mask
        positive = (minus | ~(x | plus)) & mask
        negative = plus & x
    return distance


def compare_transcripts(
    reference: str, baseline: str, candidate: str,
    keywords: Iterable[str] = (),
) -> dict:
    """Score two ASR versions against a *human-corrected* reference.

    Keyword counts are unaligned substring matches; they are diagnostics,
    not proof that every spoken occurrence was recognized correctly.
    """
    ref = comparable_text(reference)
    if not ref:
        raise ValueError("기준 전사문에 비교 가능한 발언이 없습니다.")
    baseline_text = comparable_text(baseline)
    candidate_text = comparable_text(candidate)
    original_errors = edit_distance(ref, baseline_text)
    candidate_errors = edit_distance(ref, candidate_text)
    original_cer = original_errors / len(ref)
    candidate_cer = candidate_errors / len(ref)

    terms = []
    for keyword in keywords:
        term = comparable_text(keyword)
        if not term:
            raise ValueError("비교할 전문용어는 비어 있을 수 없습니다.")
        terms.append({
            "term": keyword,
            "reference_count": ref.count(term),
            "baseline_count": baseline_text.count(term),
            "candidate_count": candidate_text.count(term),
        })

    return {
        "reference_characters": len(ref),
        "baseline": {
            "character_errors": original_errors,
            "cer": round(original_cer, 6),
        },
        "candidate": {
            "character_errors": candidate_errors,
            "cer": round(candidate_cer, 6),
        },
        "cer_reduction": round(original_cer - candidate_cer, 6),
        "relative_cer_reduction": (
            round((original_cer - candidate_cer) / original_cer, 6)
            if original_cer else None
        ),
        "keyword_occurrences_unaligned": terms,
    }
