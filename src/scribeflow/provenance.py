"""Provenance stored in the footer of the final TXT."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from . import __version__

_FOOTER = re.compile(
    r"^(?:<!-- ScribeFlow Metadata: [^\r\n]* -->\r?\n)?"
    r"<!-- ScribeFlow SHA256: ([0-9a-f]{64}) -->\s*\Z",
    re.MULTILINE,
)


@dataclass(frozen=True)
class TranscriptMetadata:
    source_filename: str
    source_size_bytes: int
    duration_seconds: float
    provider: str
    model_id: str
    completed_at: datetime
    snapshot: str | None = None
    detected_languages: tuple[str, ...] = ()


def source_fingerprint(transcript: str) -> str | None:
    """Return the source audio SHA-256 only when the *last* line is valid."""
    match = _FOOTER.search(transcript)
    return match.group(1) if match else None


def strip_transcript_footer(transcript: str) -> str:
    """Remove the current or legacy footer for speech-only comparisons."""
    return _FOOTER.sub("", transcript)


def stamp_transcript(
    transcript: str, sha256: str, metadata: TranscriptMetadata | None = None,
) -> str:
    """Append provenance while retaining the legacy checksum on the last line."""
    if not re.fullmatch(r"[0-9a-f]{64}", sha256):
        raise ValueError("원본 오디오 SHA-256 값이 올바르지 않습니다.")
    existing = source_fingerprint(transcript)
    if existing:
        if existing != sha256:
            raise ValueError("다른 원본의 SHA-256이 이미 기록되어 있습니다.")
        return transcript
    separator = "\n" if transcript.endswith("\n") else "\n\n"
    footer = f"<!-- ScribeFlow SHA256: {sha256} -->\n"
    if metadata is not None:
        filename = json.dumps(metadata.source_filename, ensure_ascii=False)
        filename = filename.replace(">", r"\u003e")
        duration = timedelta(milliseconds=round(metadata.duration_seconds * 1000))
        completed_at = metadata.completed_at.isoformat(timespec="seconds")
        detected_languages = ", ".join(metadata.detected_languages) or "확인 불가"
        details = (
            f"<!-- ScribeFlow Metadata: Source: {filename} | Duration: {duration}"
            f" | Size: {metadata.source_size_bytes:,} bytes"
            f" | Detected languages: {detected_languages}"
            f" | Provider: {metadata.provider} | Model ID: {metadata.model_id}"
            f" | Snapshot: {metadata.snapshot or '미공개'} | Transcribed at: {completed_at}"
            f" | ScribeFlow version: {__version__} -->\n"
        )
        footer = details + footer
    return transcript + separator + footer
