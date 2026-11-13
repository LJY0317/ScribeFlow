"""Publish one transcript beside its audio, keeping work out of user folders.

Local files and rclone remotes share the same transcription engine.  The
transcript is deliberately the *only* user-facing output.
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
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .cache import clean_cache, job_lock
from .engine import (
    BITRATE_KBPS,
    MODEL,
    auto_chunk_seconds,
    hash_file,
    transcribe_file,
)
from .formatting import format_paragraphs
from .provenance import source_fingerprint, stamp_transcript


@dataclass(frozen=True)
class Paths:
    source: str
    transcript: str
    remote: bool


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


def resolve_source(source: str) -> str:
    """Resolve Drive URL/ID to a scoped rclone path without downloading audio."""
    file_id = _drive_file_id(source)
    if file_id is None:
        return source
    entries = json.loads(
        run_rclone("lsjson", "gdrive:", "-R", "--files-only").stdout
    )
    found = [e for e in entries if e.get("ID") == file_id and not e.get("IsDir")]
    if not found:
        raise FileNotFoundError(
            "Google Drive 파일 ID를 현재 rclone 연결에서 찾지 못했습니다. "
            "다른 폴더에 있는 경우 SCRIBEFLOW_DRIVE_ROOT_FOLDER_ID를 "
            "해당 폴더 또는 상위 폴더 ID로 설정하세요."
        )
    metadata = found[0]
    if not (
        str(metadata.get("MimeType", "")).startswith("audio/")
        or Path(metadata["Path"]).suffix.lower() in {
            ".m4a", ".mp3", ".wav", ".aac", ".ogg",
            ".qta", ".webm", ".mp4", ".flac",
        }
    ):
        raise ValueError("선택한 Google Drive 파일은 음성 녹음 형식이 아닙니다.")
    return "gdrive:" + metadata["Path"]


def paths_for(source: str, output: str | None = None) -> Paths:
    source = resolve_source(source)
    if is_remote(source):
        remote, relative = _remote_parts(source)
        if output is None:
            folder, name = posixpath.split(relative)
            destination = remote + ":" + posixpath.join(folder, posixpath.splitext(name)[0] + ".txt")
        else:
            _, target_rel = _remote_parts(output)
            if posixpath.dirname(relative) != posixpath.dirname(target_rel):
                raise ValueError("전사문은 원본과 같은 Google Drive 폴더에 저장해야 합니다.")
            destination = output
        if not destination.lower().endswith(".txt"):
            raise ValueError("결과 파일은 .txt 확장자여야 합니다.")
        if source == destination:
            raise ValueError("입력 파일과 출력 파일을 동일하게 지정할 수 없습니다.")
        return Paths(source, destination, True)

    source_path = Path(source).expanduser().resolve()
    if output is not None and is_remote(output):
        raise ValueError("로컬 입력과 원격 출력을 섞지 마세요.")
    destination_path = (
        Path(output).expanduser().resolve() if output else source_path.with_suffix(".txt")
    )
    if destination_path.suffix.lower() != ".txt":
        raise ValueError("결과 파일은 .txt 확장자여야 합니다.")
    if destination_path.parent != source_path.parent:
        raise ValueError("전사문은 원본과 같은 폴더에 저장해야 합니다.")
    if source_path == destination_path:
        raise ValueError("입력 파일과 출력 파일을 동일하게 지정할 수 없습니다.")
    return Paths(str(source_path), str(destination_path), False)


def cache_root() -> Path:
    custom = os.environ.get("SCRIBEFLOW_CACHE_DIR")
    if custom:
        return Path(custom).expanduser().resolve()
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
    return subprocess.run(
        ["rclone", *args] + (
            ["--drive-root-folder-id", root_folder_id] if root_folder_id else []
        ),
        check=True, capture_output=True, text=True,
    )


def remote_exists(destination: str) -> bool:
    """Inspect only the explicitly requested parent folder."""
    parent, name = destination.rsplit("/", 1) if "/" in destination else (
        destination.split(":", 1)[0] + ":", destination.split(":", 1)[1]
    )
    listed = run_rclone("lsf", parent, "--files-only", "--max-depth", "1").stdout
    return name in listed.splitlines()


def remote_audio_sha256(source: str) -> str | None:
    """Use the checksum Google Drive supplies; do not download source audio."""
    parent, name = source.rsplit("/", 1) if "/" in source else (
        source.split(":", 1)[0] + ":", source.split(":", 1)[1]
    )
    entries = json.loads(
        run_rclone("lsjson", parent, "--files-only", "--hash").stdout
    )
    for entry in entries:
        if entry.get("Name") == name:
            value = entry.get("Hashes", {}).get("sha256")
            return value if value and re.fullmatch(r"[0-9a-f]{64}", value) else None
    raise FileNotFoundError(f"원본 음성을 찾지 못했습니다: {source}")


def existing_transcript_status(paths: Paths) -> str:
    """Check a preexisting transcript; never trigger paid transcription."""
    source_sha = (
        remote_audio_sha256(paths.source) if paths.remote
        else hash_file(Path(paths.source))
    )
    transcript = (
        run_rclone("cat", paths.transcript).stdout if paths.remote
        else Path(paths.transcript).read_text(encoding="utf-8")
    )
    saved = source_fingerprint(transcript)
    if source_sha is None or saved is None:
        return "unverified_existing"
    return "skipped_existing" if source_sha == saved else "source_mismatch"


def remote_result_url(destination: str) -> str | None:
    """Find the uploaded file ID without modifying Drive sharing settings."""
    parent, name = destination.rsplit("/", 1) if "/" in destination else (
        destination.split(":", 1)[0] + ":", destination.split(":", 1)[1]
    )
    entries = json.loads(run_rclone("lsjson", parent, "--files-only").stdout)
    for entry in entries:
        if entry.get("Name") == name and entry.get("ID"):
            return f"https://drive.google.com/file/d/{entry['ID']}/view"
    return None


# Scan only audio recordings by default; a directory can contain many videos.
_AUDIO_SUFFIXES = {".m4a", ".mp3", ".wav", ".aac", ".ogg", ".qta", ".flac", ".opus", ".wma"}


def scan_drive(folder: str, recursive: bool = False, limit: int = 50) -> dict:
    """Separate verified transcripts, unverified pairs, and missing ones."""
    folder = folder.rstrip("/") or folder
    if folder != "gdrive:":
        _remote_parts(folder)
    options = ["-R"] if recursive else ["--max-depth", "1"]
    entries = json.loads(
        run_rclone("lsjson", folder, *options, "--files-only", "--hash").stdout
    )
    names = {e["Path"].casefold() for e in entries if e.get("Path") and not e.get("IsDir")}
    sources = sorted(
        (e for e in entries if not e.get("IsDir") and
         (str(e.get("MimeType", "")).startswith("audio/") or
          posixpath.splitext(e.get("Path", ""))[1].lower() in _AUDIO_SUFFIXES)),
        key=lambda e: e.get("Path", ""),
    )
    pending = []
    unverified = []
    already = 0
    unverified_total = 0
    missing_total = 0
    for item in sources:
        relative = item["Path"]
        stem = posixpath.splitext(relative)[0]
        prefix = folder.rstrip(":")
        remote_path = prefix + ":" + relative if folder == "gdrive:" else folder + "/" + relative
        if (stem + ".txt").casefold() in names:
            transcript_path = prefix + ":" + stem + ".txt" if folder == "gdrive:" else folder + "/" + stem + ".txt"
            source_sha = item.get("Hashes", {}).get("sha256")
            saved_sha = source_fingerprint(run_rclone("cat", transcript_path).stdout) if source_sha else None
            state = (
                "skipped_existing" if saved_sha and saved_sha == source_sha
                else "source_mismatch" if saved_sha and source_sha
                else "unverified_existing"
            )
            if state == "skipped_existing":
                already += 1
            else:
                unverified_total += 1
                if len(unverified) < limit:
                    unverified.append({"source": remote_path, "status": state})
            continue
        missing_total += 1
        if len(pending) < limit:
            pending.append({
                "source": remote_path,
                "name": posixpath.basename(relative),
                "size_bytes": item.get("Size"),
                "drive_file_id": item.get("ID"),
            })
    return {
        "folder": folder,
        "total_audio": len(sources),
        "already_transcribed": already,
        "unverified_count": unverified_total,
        "unverified": unverified,
        "untranscribed_count": missing_total,
        "untranscribed": pending,
    }


def ensure_free_destination(paths: Paths) -> None:
    occupied = remote_exists(paths.transcript) if paths.remote else Path(paths.transcript).exists()
    if occupied:
        raise FileExistsError(
            f"이미 전사문 파일이 존재합니다: {paths.transcript}\n"
            "기존 파일은 덮어쓰지 않습니다. 다시 전사하려면 --output으로 "
            "같은 폴더의 다른 .txt 이름을 지정하세요."
        )


def _publish_local(contents: str, destination: Path) -> None:
    """Create atomically without ever overwriting a preexisting user file."""
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
        os.link(temporary, destination)  # Fails rather than replacing an existing file.
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _publish_remote(contents: str, destination: str, job_dir: Path) -> None:
    upload = job_dir / "final.txt"
    upload.write_text(contents, encoding="utf-8")
    # --ignore-existing prevents replacing concurrent/existing Drive documents.
    run_rclone("copyto", str(upload), destination, "--ignore-existing")
    if run_rclone("cat", destination).stdout != contents:
        raise RuntimeError("Google Drive 결과 업로드 검증에 실패했습니다. 기존 문서를 덮어쓰지 않았습니다.")


def plain_transcript(job_dir: Path) -> str:
    """Publish only recognized speech, never generated headings or metadata."""
    manifest = json.loads((job_dir / "manifest.json").read_text(encoding="utf-8"))
    chunks = manifest["chunks"]
    segments = []
    for chunk in chunks:
        text = (job_dir / "transcripts" / f"{chunk['index']:04d}.txt").read_text(
            encoding="utf-8"
        ).strip()
        if text:
            segments.append(text)
    if not segments:
        return ""
    return "\n\n".join(format_paragraphs(text).strip() for text in segments) + "\n"


def transcribe(
    source: str,
    output: str | None = None,
    *,
    language: str = "ko",
    keywords: tuple[str, ...] = (),
    prompt: str = "",
    chunk_seconds: int | None = None,
    overlap_seconds: int = 3,
) -> dict:
    """Create exactly one adjacent .txt file; preserve failed jobs for retry."""
    paths = paths_for(source, output)
    cache = cache_root()
    cache.mkdir(parents=True, exist_ok=True, mode=0o700)
    clean_cache(cache)
    # Serialize by *destination* as well as by source/settings signature.
    # Otherwise different keyword/prompt configurations might both incur
    # API costs before one of them notices the destination already exists.
    output_lock_key = hashlib.sha256(
        ("destination:\n" + paths.transcript).encode("utf-8")
    ).hexdigest()
    with job_lock(cache, output_lock_key):
        return _transcribe_locked(
            paths, cache, language=language, keywords=keywords, prompt=prompt,
            chunk_seconds=chunk_seconds, overlap_seconds=overlap_seconds,
        )


def _transcribe_locked(
    paths: Paths, cache: Path, *, language: str, keywords: tuple[str, ...],
    prompt: str, chunk_seconds: int | None, overlap_seconds: int,
) -> dict:
    """Destination lock already held; skip existing outputs before transfer."""
    if remote_exists(paths.transcript) if paths.remote else Path(paths.transcript).exists():
        status = existing_transcript_status(paths)
        return {
            "ok": status == "skipped_existing", "status": status, "source": paths.source,
            "transcript": paths.transcript, "model": MODEL,
            "url": _best_effort_remote_url(paths.transcript) if paths.remote else None,
        }
    if paths.remote:
        download_root = cache / "downloads"
        download_root.mkdir(parents=True, exist_ok=True)
        download = tempfile.TemporaryDirectory(prefix="download-", dir=download_root)
    else:
        download = None
    try:
        if paths.remote:
            local_source = Path(download.name) / posixpath.basename(paths.source)
            run_rclone("copyto", paths.source, str(local_source))
        else:
            local_source = Path(paths.source).resolve(strict=True)

        effective_seconds = chunk_seconds if chunk_seconds is not None else auto_chunk_seconds()
        settings = {
            "model": MODEL,
            "source_filename": local_source.name,
            "source_sha256": hash_file(local_source),
            "language": language,
            "keywords": list(keywords),
            "prompt": prompt,
            "chunk_seconds": effective_seconds,
            "overlap_seconds": overlap_seconds,
            "bitrate_kbps": BITRATE_KBPS,
        }
        signature = hashlib.sha256(
            json.dumps(settings, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        job_dir = cache / "jobs" / signature
        with job_lock(cache, signature):
            # Recheck after lock acquisition to prevent duplicate API billing.
            ensure_free_destination(paths)
            try:
                transcribe_file(
                    local_source, job_dir, chunk_seconds=effective_seconds,
                    overlap_seconds=overlap_seconds, language=language,
                    keywords=keywords, prompt=prompt,
                )
                contents = stamp_transcript(
                    plain_transcript(job_dir), settings["source_sha256"]
                )
                if paths.remote:
                    _publish_remote(contents, paths.transcript, job_dir)
                else:
                    _publish_local(contents, Path(paths.transcript))
            finally:
                if job_dir.exists():
                    os.utime(job_dir, None)
            # Keep failed jobs for retry, but drop all intermediates on success.
            try:
                shutil.rmtree(job_dir)
            except OSError as exc:
                print(f"주의: 완료된 캐시 정리에 실패했습니다: {exc}", file=sys.stderr)
        return {
            "ok": True, "status": "created", "source": paths.source,
            "transcript": paths.transcript, "model": MODEL,
            "url": _best_effort_remote_url(paths.transcript) if paths.remote else None,
        }
    finally:
        if download is not None:
            download.cleanup()


def _best_effort_remote_url(destination: str) -> str | None:
    """Result URL is informative and must never mark a successful write failed."""
    try:
        return remote_result_url(destination)
    except (OSError, RuntimeError, subprocess.CalledProcessError, ValueError):
        return None
