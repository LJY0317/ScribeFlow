"""One-shot Cloud Run Job entrypoint for an explicitly selected Drive audio."""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from pathlib import Path
from urllib.error import HTTPError

from .cloud_run import normalize_language, normalize_output_format, normalize_transcription_context
from .drive_transcription import DriveTranscriptionError, transcribe_drive
from .linked_drive import DriveCredential
from .pipeline import NoSpeechDetectedError


def run_job() -> dict:
    """Run one transcription; credentials are injected at deployment time."""
    file_id = os.environ.get("SCRIBEFLOW_SOURCE_FILE_ID", "").strip()
    envelope = os.environ.get("SCRIBEFLOW_LINKED_DRIVE_ENVELOPE", "")
    keywords, prompt = normalize_transcription_context(
        json.loads(os.environ.get("SCRIBEFLOW_KEYWORDS_JSON") or "[]"),
        os.environ.get("SCRIBEFLOW_PROMPT", ""),
    )
    language = normalize_language(os.environ.get("SCRIBEFLOW_LANGUAGE", "auto"))
    replace_existing = os.environ.get("SCRIBEFLOW_REPLACE_EXISTING") == "true"
    transcript_file_id = os.environ.get("SCRIBEFLOW_TRANSCRIPT_FILE_ID", "").strip()
    output_format = normalize_output_format(os.environ.get("SCRIBEFLOW_OUTPUT_FORMAT", "txt"))

    if not re.fullmatch(r"[A-Za-z0-9_-]{20,}", file_id):
        raise ValueError("SCRIBEFLOW_SOURCE_FILE_ID must be a Google Drive file ID")
    if not os.environ.get("OPENAI_API_KEY"):
        raise ValueError("OPENAI_API_KEY is not configured")
    if not envelope or not os.environ.get("SCRIBEFLOW_REQUEST_OWNER"):
        raise ValueError("A connected Google account is required for a transcription job")

    credential = DriveCredential.open(envelope, file_id)
    with tempfile.TemporaryDirectory(prefix="scribeflow-cloud-") as directory:
        private = Path(directory)
        previous_cache = os.environ.get("SCRIBEFLOW_CACHE_DIR")
        os.environ["SCRIBEFLOW_CACHE_DIR"] = str(private / "cache")
        try:
            return transcribe_drive(
                credential, file_id, private,
                keywords=keywords, prompt=prompt, language=language,
                replace_existing=replace_existing, transcript_file_id=transcript_file_id,
                output_format=output_format,
            )
        finally:
            if previous_cache is None:
                os.environ.pop("SCRIBEFLOW_CACHE_DIR", None)
            else:
                os.environ["SCRIBEFLOW_CACHE_DIR"] = previous_cache


def _result_summary(result: dict) -> dict:
    summary = {"result": result.get("status") or "unknown",
               "output_format": result.get("output_format") or os.environ.get("SCRIBEFLOW_OUTPUT_FORMAT", "txt")}
    for field in ("model", "error_code"):
        if result.get(field):
            summary[field] = result[field]
    file_id = result.get("transcript_file_id")
    if result.get("ok") and file_id and re.fullmatch(r"[A-Za-z0-9_-]{10,}", file_id):
        summary.update(transcript_file_id=file_id, transcript_url=f"https://drive.google.com/file/d/{file_id}/view")
    return summary


def _publish_result(summary: dict) -> None:
    project = os.environ.get("GOOGLE_CLOUD_PROJECT")
    region = os.environ.get("SCRIBEFLOW_REGION")
    job = os.environ.get("CLOUD_RUN_JOB")
    execution = os.environ.get("CLOUD_RUN_EXECUTION")
    if not all((project, region, job, execution)):
        return  # Local/manual invocation has no Cloud Run execution resource.
    from google.auth import default
    from google.auth.transport.requests import AuthorizedSession
    from .cloud_dedup import FirestoreJobClaims

    credentials, _ = default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    resource = f"projects/{project}/locations/{region}/jobs/{job}/executions/{execution}"
    with AuthorizedSession(credentials) as session:
        FirestoreJobClaims(project, session).write_result(
            resource, os.environ.get("SCRIBEFLOW_REQUEST_OWNER", ""), summary,
        )


def main() -> int:
    try:
        result = run_job()
    except Exception as exc:
        # Return stable error codes, never raw exception messages or credentials.
        if isinstance(exc, DriveTranscriptionError):
            code = exc.code
        elif isinstance(exc, NoSpeechDetectedError):
            code = "no_speech"
        elif isinstance(exc, PermissionError):
            code = "permission_denied"
        elif isinstance(exc, HTTPError):
            code = f"drive_http_{exc.code}"
        else:
            code = type(exc).__name__
        result = {"ok": False, "status": "failed", "error_code": code}
    summary = _result_summary(result)
    try:
        _publish_result(summary)
    except Exception as exc:
        # A failed receipt write must not turn a published transcript into a failed job.
        print(json.dumps({"result_record_error": type(exc).__name__}), file=sys.stderr)
    print(json.dumps({"ok": bool(result.get("ok")), "status": summary["result"],
                      "error_code": summary.get("error_code")}, ensure_ascii=False))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
