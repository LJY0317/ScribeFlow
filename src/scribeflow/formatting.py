"""Readable paragraphs without rewriting or inventing any spoken content."""

from __future__ import annotations

import re

# Model-supplied punctuation only. Do not infer speakers or semantic topics.
_SENTENCE_END = re.compile(r"[.!?。！？]+[\"'’”\)\]]*$")


def format_paragraphs(text: str, min_chars: int = 130, max_chars: int = 380) -> str:
    """Reflow whitespace only, preserving the full non-whitespace sequence.

    Prefer sentence boundaries for paragraph breaks, and fall back to token
    boundaries for long runs without punctuation. No AI rewriting or headings.
    """
    words = text.split()
    if not words:
        return ""
    paragraphs: list[str] = []
    paragraph: list[str] = []
    length = 0
    sentences = 0
    for word in words:
        paragraph.append(word)
        length += len(word) + 1
        ended = bool(_SENTENCE_END.search(word))
        if ended:
            sentences += 1
        if (
            (ended and length >= min_chars and sentences >= 2)
            or length >= max_chars
        ):
            paragraphs.append(" ".join(paragraph))
            paragraph = []
            length = 0
            sentences = 0
    if paragraph:
        paragraphs.append(" ".join(paragraph))
    formatted = "\n\n".join(paragraphs)
    assert "".join(formatted.split()) == "".join(text.split())
    return formatted + "\n"
