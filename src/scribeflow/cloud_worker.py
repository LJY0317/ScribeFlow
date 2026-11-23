"""One-shot Cloud Run Job entrypoint for an explicitly selected Drive audio."""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

from .cloud_run import normalize_language, normalize_transcription_context
from .linked_drive import DriveCredential, check_file
from .pipeline import transcribe


def run_job() -> dict:
    """Run one transcription; credentials are injected at deployment time."""
    file_id = os.environ.get("SCRIBEFLOW_SOURCE_FILE_ID", "").strip()
    envelope = os.environ.get("SCRIBEFLOW_LINKED_DRIVE_ENVELOPE", "")
    config_secret = os.environ.get("SCRIBEFLOW_RCLONE_CONFIG_SECRET", "").strip()
    keywords, prompt = normalize_transcription_context(
        json.loads(os.environ.get("SCRIBEFLOW_KEYWORDS_JSON") or "[]"),
        os.environ.get("SCRIBEFLOW_PROMPT", ""),
    )
    language = normalize_language(os.environ.get("SCRIBEFLOW_LANGUAGE", "auto"))
    replace_existing = os.environ.get("SCRIBEFLOW_REPLACE_EXISTING") == "true"
    transcript_file_id = os.environ.get("SCRIBEFLOW_TRANSCRIPT_FILE_ID", "").strip()
    output_format = os.environ.get("SCRIBEFLOW_OUTPUT_FORMAT", "txt")

    if not re.fullmatch(r"[A-Za-z0-9_-]{20,}", file_id):
        raise ValueError("SCRIBEFLOW_SOURCE_FILE_ID must be a Google Drive file ID")
    if not os.environ.get("OPENAI_API_KEY"):
        raise ValueError("OPENAI_API_KEY is not configured")
    if not envelope and (not config_secret or not Path(config_secret).is_file()):
        raise ValueError("ScribeFlow rclone config secret must be mounted as a file")

    # The Secret Manager mount is read-only, but rclone can refresh OAuth tokens.
    # Supply a writable private copy for this job; it is removed on completion.
    with tempfile.TemporaryDirectory(prefix="scribeflow-cloud-") as directory:
        private = Path(directory)
        config = private / "rclone.conf"
        scope = {}
        if envelope:
            credential = DriveCredential.open(envelope, file_id)
            record = check_file(credential, file_id)
            if not record["can_download"] or (not replace_existing and not record["can_save_transcript"]):
                raise PermissionError("The connected account lost access to the audio or its output folder")
            config.touch(mode=0o600)
            config.write_text(credential.rclone_config(record["drive_id"]), encoding="utf-8")
            scope = {"SCRIBEFLOW_DRIVE_ROOT_FOLDER_ID": record["parent_id"]}
        else:
            shutil.copyfile(config_secret, config)
            config.chmod(0o600)
        scope.update(RCLONE_CONFIG=str(config), SCRIBEFLOW_CACHE_DIR=str(private / "cache"))
        if envelope:
            scope["RCLONE_DRIVE_SHARED_WITH_ME"] = "false"
        previous = {name: os.environ.get(name) for name in scope}
        try:
            os.environ.update(scope)
            options = {"transcript_file_id": transcript_file_id} if transcript_file_id else {}
            if output_format != "txt":
                options["output_format"] = output_format
            return transcribe(
                file_id, keywords=keywords, prompt=prompt, language=language,
                replace_existing=replace_existing, **options,
            )
        finally:
            for name, value in previous.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value


def main() -> int:
    try:
        result = run_job()
    except Exception as exc:
        # Do not leak OAuth refresh tokens, API keys, or raw rclone errors to logs.
        print(json.dumps({"ok": False, "error_type": type(exc).__name__}), file=sys.stderr)
        return 1
    print(json.dumps({
        "ok": bool(result.get("ok")),
        "status": result.get("status"),
        "transcript_url": result.get("url"),
    }, ensure_ascii=False))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
