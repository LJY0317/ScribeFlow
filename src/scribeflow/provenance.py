"""Portable, human-readable source fingerprint at the end of a transcript."""

from __future__ import annotations

import re

_SUFFIX = re.compile(r"<!-- ScribeFlow SHA256: ([0-9a-f]{64}) -->")


def source_fingerprint(transcript: str) -> str | None:
    """Return the source audio SHA-256 only when the *last* line is valid."""
    lines = transcript.rstrip().splitlines()
    if not lines:
        return None
    match = _SUFFIX.fullmatch(lines[-1])
    return match.group(1) if match else None


def stamp_transcript(transcript: str, sha256: str) -> str:
    """Append the source checksum without changing any spoken text."""
    if not re.fullmatch(r"[0-9a-f]{64}", sha256):
        raise ValueError("원본 오디오 SHA-256 값이 올바르지 않습니다.")
    existing = source_fingerprint(transcript)
    if existing:
        if existing != sha256:
            raise ValueError("다른 원본의 SHA-256이 이미 기록되어 있습니다.")
        return transcript
    separator = "\n" if transcript.endswith("\n") else "\n\n"
    return transcript + separator + f"<!-- ScribeFlow SHA256: {sha256} -->\n"
