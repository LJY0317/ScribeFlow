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
from .pipeline import transcribe


def run_job() -> dict:
    """Run one transcription; credentials are injected at deployment time."""
    file_id = os.environ.get("SCRIBEFLOW_SOURCE_FILE_ID", "").strip()
    config_secret = os.environ.get("SCRIBEFLOW_RCLONE_CONFIG_SECRET", "").strip()
    keywords, prompt = normalize_transcription_context(
        json.loads(os.environ.get("SCRIBEFLOW_KEYWORDS_JSON") or "[]"),
        os.environ.get("SCRIBEFLOW_PROMPT", ""),
    )
    language = normalize_language(os.environ.get("SCRIBEFLOW_LANGUAGE", "auto"))

    if not re.fullmatch(r"[A-Za-z0-9_-]{20,}", file_id):
        raise ValueError("SCRIBEFLOW_SOURCE_FILE_ID must be a Google Drive file ID")
    if not os.environ.get("OPENAI_API_KEY"):
        raise ValueError("OPENAI_API_KEY is not configured")
    if not config_secret or not Path(config_secret).is_file():
        raise ValueError("ScribeFlow rclone config secret must be mounted as a file")

    # The Secret Manager mount is read-only, but rclone can refresh OAuth tokens.
    # Supply a writable private copy for this job; it is removed on completion.
    with tempfile.TemporaryDirectory(prefix="scribeflow-cloud-") as directory:
        private = Path(directory)
        config = private / "rclone.conf"
        shutil.copyfile(config_secret, config)
        config.chmod(0o600)
        old_config = os.environ.get("RCLONE_CONFIG")
        old_cache = os.environ.get("SCRIBEFLOW_CACHE_DIR")
        try:
            os.environ["RCLONE_CONFIG"] = str(config)
            os.environ["SCRIBEFLOW_CACHE_DIR"] = str(private / "cache")
            if language == "auto" and not keywords and not prompt:
                return transcribe(file_id)
            return transcribe(
                file_id, keywords=keywords, prompt=prompt, language=language,
            )
        finally:
            if old_config is None:
                os.environ.pop("RCLONE_CONFIG", None)
            else:
                os.environ["RCLONE_CONFIG"] = old_config
            if old_cache is None:
                os.environ.pop("SCRIBEFLOW_CACHE_DIR", None)
            else:
                os.environ["SCRIBEFLOW_CACHE_DIR"] = old_cache


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
