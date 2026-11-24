"""ID-based linked-account transcription; the local pipeline owns audio processing."""

from __future__ import annotations

import hashlib
from pathlib import Path
from urllib.error import HTTPError

from .cloud_run import normalize_output_format
from .drive_lookup import find_transcripts_by_name, read_transcript, update_transcript
from .engine import SUPPORTED_AUDIO_SUFFIXES, hash_file
from .linked_drive import (
    DriveCredential, can_add_to_folder, create_transcript, download_file,
    file_metadata, sibling_transcripts,
)
from .pipeline import transcribe
from .provenance import source_fingerprint, transcript_model


class DriveTranscriptionError(ValueError):
    """A known job outcome whose code can safely be returned to the MCP client."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _output(record: dict, status: str, model: str | None = None) -> dict:
    file_id = record["id"]
    return {"ok": True, "status": status, "output_format": Path(record["name"]).suffix.lower()[1:],
            "transcript_file_id": file_id, "url": f"https://drive.google.com/file/d/{file_id}/view",
            "model": model}


def _read_candidate(credential: DriveCredential, record: dict) -> tuple[bytes, str | None] | None:
    try:
        data = read_transcript(record["id"], credential.access_token, require_editable=False, metadata=record)
        return data, source_fingerprint(data.decode("utf-8-sig"))
    except (UnicodeError, ValueError, PermissionError):
        return None
    except HTTPError as exc:
        if exc.code == 404:
            return None
        raise  # An unavailable server is not evidence that a candidate does not match.


def _matching(credential: DriveCredential, records: list[dict], source_sha: str) -> list[tuple[dict, bytes]]:
    matches = []
    for record in records:
        candidate = _read_candidate(credential, record)
        if candidate and candidate[1] == source_sha:
            matches.append((record, candidate[0]))
    return matches


def _one(matches: list[tuple[dict, bytes]]) -> tuple[dict, bytes] | None:
    if len(matches) > 1:
        raise DriveTranscriptionError("ambiguous_existing", "Multiple existing transcripts match the recording; select a transcript file ID.")
    return matches[0] if matches else None


def _editable(record: dict) -> bool:
    capabilities = record.get("capabilities", {})
    return bool(capabilities.get("canEdit")) and capabilities.get("canModifyContent", True)


def _same_audio(before: dict, after: dict, source_sha: str) -> bool:
    if after.get("trashed") or before["id"] != after["id"]:
        return False
    if after.get("sha256Checksum"):
        return after["sha256Checksum"] == source_sha
    return bool(before.get("version")) and before["version"] == after.get("version")


def transcribe_drive(credential: DriveCredential, file_id: str, private: Path, *,
                     output_format: str = "txt", replace_existing: bool = False,
                     transcript_file_id: str = "", **hints) -> dict:
    """Use the authenticated Drive ID from download through publication."""
    output_format = normalize_output_format(output_format)
    source = file_metadata(credential, file_id)
    name = source["name"]
    suffix = Path(name).suffix.lower()
    if source.get("trashed") or suffix not in SUPPORTED_AUDIO_SUFFIXES:
        raise DriveTranscriptionError("unsupported_audio", "Select a supported, non-trashed audio file.")
    if not source.get("capabilities", {}).get("canDownload"):
        raise PermissionError("The connected account cannot download the recording")
    stem = name[:len(name) - len(suffix)]
    desired = stem + "." + output_format
    names = (desired,) if replace_existing else (stem + ".txt", stem + ".md")
    # Local staging has a safe filename; the provenance keeps the actual Drive name.
    audio = private / ("audio" + suffix)
    source_sha = source.get("sha256Checksum")

    def download() -> str:
        download_file(credential, file_id, audio)
        digest = hash_file(audio)
        if source_sha and digest != source_sha:
            raise DriveTranscriptionError("source_changed", "The recording changed during download.")
        return digest

    if not source_sha:
        source_sha = download()
    selected = None
    old_data = None
    if transcript_file_id:
        if not replace_existing:
            raise ValueError("A transcript ID requires replacement mode")
        record = file_metadata(credential, transcript_file_id)
        if Path(record["name"]).suffix.lower() != "." + output_format:
            raise DriveTranscriptionError("format_mismatch", "Select the format of the existing transcript.")
        candidate = _read_candidate(credential, record)
        if not candidate or candidate[1] != source_sha:
            raise DriveTranscriptionError("source_mismatch", "The transcript does not identify this audio SHA-256.")
        selected, old_data = record, candidate[0]
    else:
        siblings = sibling_transcripts(credential, source, names)
        matches = _matching(credential, siblings, source_sha)
        if replace_existing:
            if not siblings:
                records = [file_metadata(credential, candidate_id) for candidate_id in
                           find_transcripts_by_name(desired, credential.access_token)]
                matches = _matching(credential, records, source_sha)
            match = _one(matches)
            if match is None:
                raise DriveTranscriptionError("replacement_missing" if not siblings else "source_mismatch",
                                              "No matching existing transcript was found.")
            selected, old_data = match
        else:
            preferred = [match for match in matches if match[0]["name"] == desired]
            match = _one(preferred or matches)
            if match:
                record, data = match
                return _output(record, "skipped_existing", (transcript_model(data.decode("utf-8-sig")) or {}).get("model_id"))
            if any(record["name"] == desired for record in siblings):
                raise DriveTranscriptionError("destination_conflict", "The requested filename already exists but does not match the recording.")

    parents = source.get("parents") or []
    if selected:
        if not _editable(selected):
            raise PermissionError("The existing transcript cannot be edited")
    elif len(parents) != 1 or not can_add_to_folder(credential, parents[0]):
        raise PermissionError("The connected account cannot create a transcript beside the recording")

    if not audio.exists():
        download()
    result = transcribe(str(audio), output_format=output_format, source_filename=name,
                        retain_chunks=False, **hints)
    if not result.get("ok"):
        return result
    data = Path(result["transcript"]).read_bytes()
    current = file_metadata(credential, file_id)
    if not _same_audio(source, current, source_sha) or source_fingerprint(data.decode("utf-8")) != source_sha:
        raise DriveTranscriptionError("source_changed", "The recording changed during transcription; no transcript was published.")
    mime_type = "text/markdown" if output_format == "md" else "text/plain"
    if selected:
        fresh = file_metadata(credential, selected["id"])
        if (not _editable(fresh) or fresh.get("trashed") or
                Path(fresh["name"]).suffix.lower() != "." + output_format):
            raise DriveTranscriptionError("destination_changed", "The existing transcript is no longer editable in the requested format.")
        # Compare the actual content; moving the file alone need not invalidate the request.
        if fresh.get("sha256Checksum"):
            unchanged = fresh["sha256Checksum"] == hashlib.sha256(old_data).hexdigest()
        else:
            unchanged = read_transcript(fresh["id"], credential.access_token, metadata=fresh) == old_data
        if not unchanged:
            raise DriveTranscriptionError("destination_changed", "The existing transcript was edited during transcription.")
        saved = update_transcript(selected["id"], credential.access_token, data, content_type=mime_type)
        if saved.get("id") != selected["id"]:
            raise RuntimeError("Drive returned an unexpected transcript ID")
        saved_name = fresh["name"]
    else:
        if current.get("parents") != source.get("parents") or current["name"] != name:
            raise DriveTranscriptionError("source_changed", "The recording moved or was renamed during transcription.")
        if sibling_transcripts(credential, current, (desired,)):
            raise DriveTranscriptionError("destination_conflict", "The requested output appeared during transcription.")
        saved = create_transcript(credential, parents[0], desired, data, mime_type)
        saved_name = desired
    if not saved.get("id") or (saved.get("sha256Checksum") and saved["sha256Checksum"] != hashlib.sha256(data).hexdigest()):
        raise RuntimeError("Drive returned an unexpected upload result")
    return _output({"id": saved["id"], "name": saved_name}, "replaced" if selected else "created", result.get("model"))
