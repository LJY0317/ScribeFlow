"""Publish one transcript beside its audio, keeping work out of user folders.

Local files use the filesystem; Drive transcriptions delegate to the same
ID-based Drive API workflow as the Cloud Run worker.
"""

from __future__ import annotations

import hashlib
import json
import os
import posixpath
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from math import ceil
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .cache import clean_cache, job_lock, retain_failed_cache
from .cloud_run import normalize_language, normalize_output_format, normalize_transcription_context
from .drive_lookup import candidate_from_id, existing_access_token
from .engine import (
    SUPPORTED_AUDIO_SUFFIXES,
    clock,
    configured_model,
    hash_file,
    transcribe_file,
)
from .formatting import format_paragraphs
from .provenance import (
    TranscriptMetadata, can_upgrade_transcript, model_upgrade_recommended,
    source_fingerprint, stamp_transcript, transcript_model,
)

@dataclass(frozen=True)
class Paths:
    source: str
    transcript: str
    remote: bool


@dataclass(frozen=True)
class Replacement:
    source_sha256: str
    transcript_sha256: str


def is_remote(value: str) -> bool:
    """Rclone remote syntax: gdrive:folder/audio.m4a, not macOS/Windows paths."""
    if re.match(r"^[A-Za-z]:[/\\]", value):
        return False
    return bool(re.match(r"^[A-Za-z0-9][A-Za-z0-9_-]*:[^/\\]", value))


def _remote_parts(value: str) -> tuple[str, str]:
    if not is_remote(value):
        raise ValueError("Google Drive 경로는 'gdrive:폴더/녹음.m4a' 형식이어야 합니다.")
    remote, relative = value.split(":", 1)
    if remote != "gdrive":
        raise ValueError("현재 Google Drive 원격 연결 'gdrive:'만 지원합니다.")
    parts = relative.split("/")
    if any(p in {"", ".", ".."} for p in parts):
        raise ValueError("원격 경로에 빈 폴더명이나 '..'을 사용할 수 없습니다.")
    return remote, relative


def _drive_file_id(source: str) -> str | None:
    """Recognize user-supplied Google Drive file links or bare file IDs."""
    if re.fullmatch(r"[A-Za-z0-9_-]{20,}", source):
        return source
    parts = urlparse(source)
    if parts.scheme not in {"http", "https"} or parts.netloc != "drive.google.com":
        return None
    match = re.fullmatch(r"/file/d/([A-Za-z0-9_-]{20,})(?:/.*)?", parts.path)
    if match:
        return match.group(1)
    if parts.path in {"/open", "/uc"}:
        value = parse_qs(parts.query).get("id", [None])[0]
        if value and re.fullmatch(r"[A-Za-z0-9_-]{20,}", value):
            return value
    raise ValueError("지원하지 않는 Google Drive 파일 URL입니다.")


def _require_supported_audio_path(path: str) -> None:
    suffix = posixpath.splitext(path)[1].lower()
    if suffix not in SUPPORTED_AUDIO_SUFFIXES:
        raise ValueError(
            f"지원하지 않는 오디오 확장자입니다: {suffix or '(없음)'}. "
            "ScribeFlow는 손실 재인코딩을 자동 수행하지 않습니다."
        )


def _verified_fast_drive_path(file_id: str) -> str | None:
    """Resolve a scoped/display path for read-only CLI discovery.

    Verify the final rclone file ID; direct Drive transcription does not use
    this lookup for ordinary file IDs.
    """
    if not os.environ.get("RCLONE_CONFIG"):
        return None
    root = os.environ.get("SCRIBEFLOW_DRIVE_ROOT_FOLDER_ID") or None
    try:
        # Refresh the local rclone connection if necessary.
        run_rclone("about", "gdrive:")
    except (OSError, RuntimeError, subprocess.CalledProcessError):
        return None
    candidate = candidate_from_id(file_id, root)
    if candidate is None:
        return None
    _require_supported_audio_path(candidate.relative_path)
    shared_before = os.environ.get("RCLONE_DRIVE_SHARED_WITH_ME")
    enabled_shared = candidate.shared_with_me and shared_before is None
    if enabled_shared:
        os.environ["RCLONE_DRIVE_SHARED_WITH_ME"] = "true"
    path = "gdrive:" + candidate.relative_path
    matched = False
    try:
        metadata = json.loads(run_rclone("lsjson", path, "--stat").stdout)
        matched = (
            isinstance(metadata, dict)
            and metadata.get("ID") == file_id
            and not metadata.get("IsDir")
        )
        return path if matched else None
    except (OSError, RuntimeError, subprocess.CalledProcessError, ValueError, TypeError):
        return None
    finally:
        if enabled_shared and not matched:
            os.environ.pop("RCLONE_DRIVE_SHARED_WITH_ME", None)


def resolve_source(source: str) -> str:
    """Resolve Drive URL/ID to a scoped rclone path without downloading audio."""
    file_id = _drive_file_id(source)
    if file_id is None:
        return source

    # Bounded metadata queries are much cheaper than listing an entire Drive.
    fast = _verified_fast_drive_path(file_id)
    if fast is not None:
        return fast

    def find() -> dict | None:
        entries = json.loads(
            run_rclone("lsjson", "gdrive:", "-R", "--files-only").stdout
        )
        return next(
            (entry for entry in entries
             if entry.get("ID") == file_id and not entry.get("IsDir")),
            None,
        )

    metadata = None
    shared_before = os.environ.get("RCLONE_DRIVE_SHARED_WITH_ME")
    # A shared-with-me folder isn't included in the normal My Drive root.
    # A worker processes one file, so keep the matching rclone view for its
    # subsequent download, transcript lookup, and upload.
    if not os.environ.get("SCRIBEFLOW_DRIVE_ROOT_FOLDER_ID") and shared_before is None:
        os.environ["RCLONE_DRIVE_SHARED_WITH_ME"] = "true"
        try:
            metadata = find()
        finally:
            if metadata is None:
                os.environ.pop("RCLONE_DRIVE_SHARED_WITH_ME", None)
    if metadata is None:
        metadata = find()
    if metadata is None:
        raise FileNotFoundError(
            "Google Drive 파일 ID를 현재 rclone 연결에서 찾지 못했습니다. "
            "설정된 Drive 계정과 공유 권한을 확인하세요."
        )
    _require_supported_audio_path(metadata["Path"])
    return "gdrive:" + metadata["Path"]


def paths_for(source: str, output: str | None = None, *, output_format: str = "txt") -> Paths:
    output_format = normalize_output_format(output_format)
    source = resolve_source(source)
    _require_supported_audio_path(source)
    if is_remote(source):
        remote, relative = _remote_parts(source)
        if output is None:
            folder, name = posixpath.split(relative)
            destination = remote + ":" + posixpath.join(folder, posixpath.splitext(name)[0] + "." + output_format)
        else:
            _, target_rel = _remote_parts(output)
            if posixpath.dirname(relative) != posixpath.dirname(target_rel):
                raise ValueError("전사문은 원본과 같은 Google Drive 폴더에 저장해야 합니다.")
            destination = output
        if not destination.lower().endswith("." + output_format):
            raise ValueError(f"결과 파일은 .{output_format} 확장자여야 합니다.")
        if source == destination:
            raise ValueError("입력 파일과 출력 파일을 동일하게 지정할 수 없습니다.")
        return Paths(source, destination, True)

    source_path = Path(source).expanduser().resolve()
    if output is not None and is_remote(output):
        raise ValueError("로컬 입력과 원격 출력을 섞지 마세요.")
    destination_path = (
        Path(output).expanduser().resolve() if output else source_path.with_suffix("." + output_format)
    )
    if destination_path.suffix.lower() != "." + output_format:
        raise ValueError(f"결과 파일은 .{output_format} 확장자여야 합니다.")
    if destination_path.parent != source_path.parent:
        raise ValueError("전사문은 원본과 같은 폴더에 저장해야 합니다.")
    if source_path == destination_path:
        raise ValueError("입력 파일과 출력 파일을 동일하게 지정할 수 없습니다.")
    return Paths(str(source_path), str(destination_path), False)


def cache_root() -> Path:
    custom = os.environ.get("SCRIBEFLOW_CACHE_DIR")
    if custom:
        return Path(custom).expanduser().resolve()
    # Editable source checkouts keep ScribeFlow's temporary data self-contained.
    # A packaged installation remains free to use the OS-standard cache dir.
    project_root = Path(__file__).resolve().parents[2]
    if (project_root / "pyproject.toml").is_file() and (
        project_root / "src" / "scribeflow"
    ).is_dir():
        return project_root / ".scribeflow" / "cache"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "ScribeFlow"
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA", Path.home())) / "ScribeFlow" / "Cache"
    return Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "scribeflow"


def run_rclone(*args: str) -> subprocess.CompletedProcess:
    # Optional Drive root scopes all rclone operations to a selected subtree.
    root_folder_id = os.environ.get("SCRIBEFLOW_DRIVE_ROOT_FOLDER_ID")
    if not shutil.which("rclone"):
        raise RuntimeError(
            "Google Drive 파일 처리를 위해 rclone이 필요합니다. "
            "rclone config에서 Google Drive 연결을 설정하세요."
        )
    tail_options = {"encoding": "utf-8", "errors": "replace"} if "--tail" in args else {}
    return subprocess.run(
        ["rclone", *args] + (
            ["--drive-root-folder-id", root_folder_id] if root_folder_id else []
        ),
        check=True, capture_output=True, text=True, **tail_options,
    )


class _LocalDriveCredential:
    """Use rclone's existing OAuth connection, without using rclone for file I/O."""

    def __init__(self):
        self.config_path = os.environ.get("RCLONE_CONFIG")
        if not self.config_path:
            line = run_rclone("config", "paths").stdout.splitlines()[0]
            _, _, self.config_path = line.partition(": ")

    @property
    def access_token(self) -> str:
        token = existing_access_token(self.config_path)
        if not token:
            # rclone owns the refresh token and refresh lifecycle.
            run_rclone("about", "gdrive:")
            token = existing_access_token(self.config_path)
        if not token:
            raise PermissionError("Google Drive 전사에는 rclone의 활성 Google OAuth 연결이 필요합니다.")
        return token


def _transcribe_drive_cli(
    source: str, output: str | None, *, requested_id: str | None,
    cache: Path, output_format: str, replace_existing: bool,
    transcript_file_id: str | None, **hints,
) -> dict:
    # Import here: the Drive workflow delegates its audio processing back to
    # the local-only branch of transcribe().
    from .drive_transcription import DriveTranscriptionError, transcribe_drive

    path = None
    output_name = None
    file_id = requested_id
    if not file_id or output or os.environ.get("SCRIBEFLOW_DRIVE_ROOT_FOLDER_ID"):
        paths = paths_for(source, output, output_format=output_format)
        path = paths.source
        record = json.loads(run_rclone("lsjson", path, "--stat").stdout)
        resolved_id = record.get("ID")
        if record.get("IsDir") or not resolved_id or (file_id and resolved_id != file_id):
            raise ValueError("선택한 Drive 원본 파일을 고유하게 식별할 수 없습니다.")
        file_id = resolved_id
        if output:
            output_name = posixpath.basename(paths.transcript)
    # The direct Drive ID is preferred for URLs/IDs. No rclone path fallback
    # for audio downloads, transcript selection, or publication.
    if not re.fullmatch(r"[A-Za-z0-9_-]{10,}", file_id or ""):
        raise ValueError("A valid Google Drive file ID is required")
    credential = _LocalDriveCredential()
    lock_key = hashlib.sha256(("drive:" + file_id).encode()).hexdigest()
    downloads = cache / "downloads"
    downloads.mkdir(parents=True, exist_ok=True)
    with job_lock(cache, lock_key), tempfile.TemporaryDirectory(prefix="drive-", dir=downloads) as directory:
        try:
            result = transcribe_drive(
                credential, file_id, Path(directory), output_format=output_format,
                replace_existing=replace_existing, transcript_file_id=transcript_file_id or "",
                output_name=output_name, **hints,
            )
        except DriveTranscriptionError as exc:
            return {"ok": False, "status": exc.code, "source": path or source, "transcript": None}
    destination = None
    if (path and result.get("transcript_name") and result.get("transcript_parent_id")
            and result.get("transcript_parent_id") == result.get("source_parent_id")):
        remote, relative = _remote_parts(path)
        destination = remote + ":" + posixpath.join(posixpath.dirname(relative), result["transcript_name"])
    result.pop("transcript_name", None)
    result.pop("transcript_parent_id", None)
    result.pop("source_parent_id", None)
    return {**result, "source": path or source, "transcript": destination}


# Use the same supported extensions in Drive discovery and the transcription engine.


def scan_drive(
    folder: str, recursive: bool = False, limit: int = 50, *, include_models: bool = False,
) -> dict:
    """Separate verified transcripts, unverified pairs, and missing ones."""
    folder = folder.rstrip("/") or folder
    if folder != "gdrive:":
        _remote_parts(folder)
    options = ["-R"] if recursive else ["--max-depth", "1"]
    entries = json.loads(
        run_rclone("lsjson", folder, *options, "--files-only", "--hash").stdout
    )
    transcripts = {}
    for entry in entries:
        path = entry.get("Path", "")
        if not entry.get("IsDir") and path.lower().endswith((".txt", ".md")):
            transcripts.setdefault(path, []).append(entry)
    sources = sorted(
        (e for e in entries if not e.get("IsDir") and
         posixpath.splitext(e.get("Path", ""))[1].lower()
         in SUPPORTED_AUDIO_SUFFIXES),
        key=lambda e: e.get("Path", ""),
    )
    pending = []
    unverified = []
    recorded = []
    model_counts = {}
    current_model = configured_model() if include_models else None
    already = 0
    unverified_total = 0
    missing_total = 0
    for item in sources:
        relative = item["Path"]
        stem = posixpath.splitext(relative)[0]
        prefix = folder.rstrip(":")
        remote_path = prefix + ":" + relative if folder == "gdrive:" else folder + "/" + relative
        states = []
        for extension in ("txt", "md"):
            matches = transcripts.get(stem + "." + extension, [])
            if not matches:
                continue
            source_sha = item.get("Hashes", {}).get("sha256")
            transcript_path = None
            contents = ""
            if len(matches) == 1:
                base = "gdrive:" if folder == "gdrive:" else folder + "/"
                transcript_path = base + matches[0]["Path"]
                if source_sha or include_models:
                    contents = run_rclone("cat", transcript_path, "--tail", "4096").stdout
            saved_sha = source_fingerprint(contents)
            state = (
                "skipped_existing" if saved_sha and saved_sha == source_sha
                else "source_mismatch" if saved_sha and source_sha
                else "unverified_existing"
            )
            states.append(state)
            if include_models:
                details = transcript_model(contents)
                model_key = details["model_id"] if details else None
                model_counts[model_key] = model_counts.get(model_key, 0) + 1
                if len(recorded) < limit:
                    recorded.append({
                        "source": remote_path, "drive_file_id": item.get("ID"),
                        "transcript": transcript_path,
                        "transcript_drive_file_id": matches[0].get("ID") if len(matches) == 1 else None,
                        "output_format": extension, "status": state, "model": details,
                        "upgrade_candidate": (state == "skipped_existing"
                                              and model_upgrade_recommended(details, current_model)),
                    })
        if states:
            if "skipped_existing" in states:
                already += 1
            else:
                unverified_total += 1
                if len(unverified) < limit:
                    unverified.append({"source": remote_path, "status": "source_mismatch" if "source_mismatch" in states else "unverified_existing"})
            continue
        missing_total += 1
        if len(pending) < limit:
            pending.append({
                "source": remote_path,
                "name": posixpath.basename(relative),
                "size_bytes": item.get("Size"),
                "drive_file_id": item.get("ID"),
            })
    result = {
        "folder": folder,
        "total_audio": len(sources),
        "already_transcribed": already,
        "unverified_count": unverified_total,
        "unverified": unverified,
        "untranscribed_count": missing_total,
        "untranscribed": pending,
    }
    if include_models:
        result["transcribed"] = recorded
        result["model_counts"] = [
            {"model_id": model_id, "count": count} for model_id, count in model_counts.items()
        ]
    return result


def ensure_free_destination(paths: Paths) -> None:
    occupied = Path(paths.transcript).exists()
    if occupied:
        raise FileExistsError(
            f"이미 전사문 파일이 존재합니다: {paths.transcript}\n"
            "기존 파일은 덮어쓰지 않습니다. 다시 전사하려면 --output으로 "
            "같은 폴더의 다른 이름을 지정하세요."
        )


def _publish_local(contents: str, destination: Path, *, replace: bool = False) -> None:
    """Publish atomically after the caller has selected and verified a replacement."""
    if not destination.parent.is_dir():
        raise FileNotFoundError(f"저장할 폴더가 없습니다: {destination.parent}")
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=destination.parent,
            prefix=".scribeflow-", suffix=".part", delete=False,
        ) as stream:
            temporary = Path(stream.name)
            stream.write(contents)
            stream.flush()
            os.fsync(stream.fileno())
        if replace:
            temporary.chmod(destination.stat().st_mode & 0o777)
            os.replace(temporary, destination)
        else:
            os.link(temporary, destination)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


class NoSpeechDetectedError(ValueError):
    """No recognizable speech was returned; do not publish a blank TXT."""


def assemble_transcript(
    job_dir: Path, chunks: list[dict], *, output_format: str = "txt",
) -> tuple[str, tuple[str, ...]]:
    """Join cached speech and collect the languages reported for its chunks."""
    segments = []
    languages: list[str] = []
    for chunk in chunks:
        result_path = job_dir / "transcripts" / f"{chunk['index']:04d}.json"
        if result_path.exists():
            result = json.loads(result_path.read_text(encoding="utf-8"))
            text = result["text"]
            languages.extend(result["languages"])
        else:
            # Older resumable jobs cached the text alone.
            text = result_path.with_suffix(".txt").read_text(encoding="utf-8").strip()
        if not text:
            continue
        text = format_paragraphs(text).strip()
        start = clock(chunk["start"])
        end = clock(ceil(chunk["end"]))
        heading = "## " if output_format == "md" else ""
        segments.append(f"{heading}[{start}–{end}]\n{text}")
    text = "\n\n".join(segments)
    return (text + "\n" if text else ""), tuple(dict.fromkeys(languages))


def transcribe(
    source: str,
    output: str | None = None,
    *,
    language: str = "auto",
    keywords: tuple[str, ...] = (),
    prompt: str = "",
    chunk_seconds: int | None = None,
    overlap_seconds: int = 3,
    replace_existing: bool = False,
    transcript_file_id: str | None = None,
    output_format: str = "txt",
    source_filename: str | None = None,
    retain_chunks: bool = True,
) -> dict:
    """Create exactly one adjacent TXT or Markdown file; preserve failed jobs for retry."""
    keywords, prompt = normalize_transcription_context(keywords, prompt)
    language = normalize_language(language)
    requested_id = _drive_file_id(source)
    output_format = normalize_output_format(output_format)
    remote = bool(requested_id or is_remote(source))
    if transcript_file_id and (
        not replace_existing or not remote
        or not re.fullmatch(r"[A-Za-z0-9_-]{10,}", transcript_file_id)
    ):
        raise ValueError("A Drive transcript file ID requires remote replacement mode")
    model = configured_model()
    keep_failed = retain_failed_cache()
    cache = cache_root()
    cache.mkdir(parents=True, exist_ok=True, mode=0o700)
    clean_cache(cache)
    if remote:
        return _transcribe_drive_cli(
            source, output, requested_id=requested_id, cache=cache, output_format=output_format,
            replace_existing=replace_existing, transcript_file_id=transcript_file_id,
            language=language, keywords=keywords, prompt=prompt,
            chunk_seconds=chunk_seconds, overlap_seconds=overlap_seconds,
        )
    paths = paths_for(source, output, output_format=output_format)
    # Default TXT and Markdown are one logical output, not two billable jobs.
    alternate = None
    if output is None and not replace_existing:
        alternate = str(Path(paths.transcript).with_suffix(".md" if output_format == "txt" else ".txt"))
    lock_target = str(Path(paths.transcript).with_suffix(""))
    output_lock_key = hashlib.sha256(("destination:\n" + lock_target).encode()).hexdigest()
    with job_lock(cache, output_lock_key):
        return _transcribe_locked(
            paths, cache, language=language, keywords=keywords, prompt=prompt,
            chunk_seconds=chunk_seconds, overlap_seconds=overlap_seconds,
            keep_failed=keep_failed, model=model, replace_existing=replace_existing,
            source_filename=source_filename, retain_chunks=retain_chunks,
            alternate_transcript=alternate,
        )


def _transcribe_locked(
    paths: Paths, cache: Path, *, language: str, keywords: tuple[str, ...],
    prompt: str, chunk_seconds: int | None, overlap_seconds: int,
    keep_failed: bool, model: str, replace_existing: bool,
    source_filename: str | None = None, retain_chunks: bool = True,
    alternate_transcript: str | None = None,
) -> dict:
    """Select one local output while holding the destination lock."""
    local_source = Path(paths.source).resolve(strict=True)
    source_sha = hash_file(local_source)
    replacement = None
    previous_model = None
    matches = []
    conflict = None
    for destination in (paths.transcript, alternate_transcript):
        if not destination or not Path(destination).is_file():
            continue
        data = Path(destination).read_bytes()
        old = data.decode("utf-8-sig")
        previous_sha = source_fingerprint(old)
        if previous_sha == source_sha:
            matches.append((destination, old, hashlib.sha256(data).hexdigest()))
        elif destination == paths.transcript:
            conflict = "source_mismatch" if previous_sha else "unverified_existing"
    if matches:
        reusable = [] if replace_existing else [
            match for match in matches if not can_upgrade_transcript(match[1], model)
        ]
        destination, old, digest = (reusable or matches)[0]
        previous_model = (transcript_model(old) or {}).get("model_id")
        if reusable:
            return {"ok": True, "status": "skipped_existing", "source": paths.source,
                    "transcript": destination, "url": None, "model": previous_model,
                    "output_format": Path(destination).suffix.lower()[1:], "transcript_file_id": None}
        paths = Paths(paths.source, destination, False)
        replacement = Replacement(source_sha, digest)
    elif conflict or replace_existing:
        return {"ok": False, "status": conflict or "replacement_missing", "transcript": paths.transcript}
    settings = {
        "model": model,
        "source_filename": local_source.name,
        "source_sha256": source_sha,
        "language": language,
        "keywords": list(keywords),
        "prompt": prompt,
        "chunk_seconds": chunk_seconds,
        "overlap_seconds": overlap_seconds,
        "audio_strategy": "original-or-stream-copy-v1",
    }
    signature = hashlib.sha256(
        json.dumps(settings, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    job_dir = cache / "jobs" / signature
    with job_lock(cache, signature):
        # Recheck after lock acquisition to prevent duplicate API billing.
        if replacement is None:
            ensure_free_destination(paths)
        try:
            transcribe_file(
                local_source, job_dir, chunk_seconds=chunk_seconds,
                overlap_seconds=overlap_seconds, language=language,
                keywords=keywords, prompt=prompt, model=model,
                **({"retain_chunks": False} if not retain_chunks else {}),
            )
            manifest = json.loads(
                (job_dir / "manifest.json").read_text(encoding="utf-8")
            )
            recognized, detected_languages = assemble_transcript(
                job_dir, manifest["chunks"],
                output_format=posixpath.splitext(paths.transcript)[1].lstrip(".").lower(),
            )
            if not recognized.strip():
                raise NoSpeechDetectedError(
                    "음성에서 인식된 발언이 없습니다. "
                    "비어 있는 전사문은 생성하지 않았습니다."
                )
            contents = stamp_transcript(
                recognized,
                manifest["source_sha256"],
                TranscriptMetadata(
                    source_filename=source_filename or manifest["source_filename"],
                    source_size_bytes=manifest["source_size"],
                    duration_seconds=manifest["duration_seconds"],
                    provider="OpenAI",
                    model_id=manifest["model"],
                    completed_at=datetime.now(timezone.utc),
                    detected_languages=detected_languages,
                ),
            )
            if (manifest["source_sha256"] != settings["source_sha256"]
                    or hash_file(local_source) != settings["source_sha256"]):
                raise ValueError("전사 중에 원본 음성이 변경되었습니다.")
            if replacement and hash_file(Path(paths.transcript)) != replacement.transcript_sha256:
                raise ValueError("전사 중에 기존 전사문이 변경되었습니다.")
            _publish_local(contents, Path(paths.transcript), replace=replacement is not None)
        except BaseException:
            if not keep_failed and job_dir.is_dir():
                try:
                    shutil.rmtree(job_dir)
                except OSError as exc:
                    print(
                        f"주의: 실패한 작업의 임시 캐시를 정리하지 못했습니다: {exc}",
                        file=sys.stderr,
                    )
            raise
        finally:
            if job_dir.exists():
                os.utime(job_dir, None)
        # Mark the published job first so a future cleanup can immediately
        # reap its cache even if today's deletion hits a transient error.
        try:
            (job_dir / ".published").touch(exist_ok=True)
        except OSError:
            pass
        try:
            shutil.rmtree(job_dir)
        except OSError as exc:
            print(f"주의: 완료된 캐시 정리에 실패했습니다: {exc}", file=sys.stderr)
    status = ("replaced" if replace_existing else "upgraded") if replacement else "created"
    result = {
        "ok": True, "status": status, "source": paths.source,
        "output_format": Path(paths.transcript).suffix.lower()[1:],
        "transcript": paths.transcript, "model": model, "url": None,
        "transcript_file_id": None,
    }
    if status == "upgraded":
        result["previous_model"] = previous_model
    return result
