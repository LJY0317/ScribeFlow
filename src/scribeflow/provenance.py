"""Provenance and the shared model-upgrade policy for final transcripts."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from . import __version__

_FOOTER = re.compile(
    r"^(?:<!-- ScribeFlow Metadata: (?P<metadata>[^\r\n]*) -->\r?\n)?"
    r"<!-- ScribeFlow SHA256: (?P<sha256>[0-9a-f]{64}) -->\s*\Z",
    re.MULTILINE,
)
_MODEL_DETAILS = re.compile(
    r" \| Provider: (?P<provider>[^|]+) \| Model ID: (?P<model_id>[\w.:-]+)"
    r" \| Snapshot: (?P<snapshot>[^|]+) \| Transcribed at: (?P<transcribed_at>[^|]+)"
    r" \| ScribeFlow version: (?P<scribeflow_version>[^|]+)\Z"
)


# Known, provider-documented migrations for ordinary file transcription only.
# Do not rank arbitrary model names, dates, or special-purpose diarization models.
# https://developers.openai.com/api/docs/guides/speech-to-text
_MODEL_UPGRADES = {
    "gpt-transcribe": frozenset({
        "whisper-1", "gpt-4o-transcribe", "gpt-4o-mini-transcribe",
        "gpt-4o-mini-transcribe-2025-12-15", "gpt-4o-mini-transcribe-2025-03-20",
    }),
}
_BODY_DIGEST = re.compile(r" \| Transcript SHA256: ([0-9a-f]{64})(?= \|)")


def _body_sha256(body: str) -> str:
    # Native Windows newline translation must not count as a manual edit.
    return hashlib.sha256(body.replace("\r\n", "\n").encode("utf-8")).hexdigest()


def model_upgrade_recommended(details: dict | None, current_model: str) -> bool:
    """Only explicitly supported upgrade pairs qualify; unknown models are kept."""
    return bool(
        details and details.get("provider") == "OpenAI"
        and details.get("model_id") in _MODEL_UPGRADES.get(current_model, ())
    )


def can_upgrade_transcript(transcript: str, current_model: str) -> bool:
    """Called only after matching the original audio SHA-256.

    Legacy complete metadata has no body hash: an explicit request to transcribe
    the selected audio may upgrade it, but its prior manual edits are unknowable.
    For newly stamped transcripts, a changed body prevents automatic replacement.
    """
    if not model_upgrade_recommended(transcript_model(transcript), current_model):
        return False
    footer = _FOOTER.search(transcript)
    digest = _BODY_DIGEST.search(footer["metadata"] or "")
    if digest is None:
        return " | Transcript SHA256:" not in (footer["metadata"] or "")
    return digest[1] == _body_sha256(transcript[:footer.start()])


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
    return match["sha256"] if match else None


def transcript_model(transcript: str) -> dict | None:
    """Read recorded model details from current TXT; legacy SHA-only TXT has none."""
    footer = _FOOTER.search(transcript)
    model = _MODEL_DETAILS.search(footer["metadata"] or "") if footer else None
    if model is None:
        return None
    details = model.groupdict()
    if details["snapshot"] == "미공개":
        details["snapshot"] = None
    return details


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
            f" | Transcript SHA256: {_body_sha256(transcript + separator)}"
            f" | Provider: {metadata.provider} | Model ID: {metadata.model_id}"
            f" | Snapshot: {metadata.snapshot or '미공개'} | Transcribed at: {completed_at}"
            f" | ScribeFlow version: {__version__} -->\n"
        )
        footer = details + footer
    return transcript + separator + footer
