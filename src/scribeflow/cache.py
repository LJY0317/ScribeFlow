"""Bounded, on-demand cleanup for ScribeFlow-owned retry artifacts."""

from __future__ import annotations

import os
import re
import shutil
import time
from pathlib import Path

from filelock import FileLock, Timeout

MAX_AGE_SECONDS = 24 * 60 * 60  # Keep failed retries only one day by default.
MAX_TOTAL_BYTES = 256 * 1024 ** 2  # Soft budget for local incomplete jobs.
MIN_IDLE_SECONDS_FOR_SIZE_CLEANUP = 60 * 60


def retain_failed_cache() -> bool:
    """Default to a short retry cache; allow strict zero-retention mode.

    The Cloud Run worker already uses an ephemeral private temporary directory,
    so this setting primarily controls local CLI failure artifacts.
    """
    policy = os.environ.get("SCRIBEFLOW_FAILED_CACHE_MODE", "retry").strip().lower()
    if policy not in {"retry", "discard"}:
        raise ValueError("SCRIBEFLOW_FAILED_CACHE_MODE must be 'retry' or 'discard'")
    return policy == "retry"


def job_lock(root: Path, signature: str) -> FileLock:
    locks = root / "locks"
    locks.mkdir(parents=True, exist_ok=True)
    return FileLock(str(locks / f"{signature}.lock"))


def _size(path: Path) -> int:
    return sum(
        item.stat().st_size
        for item in path.rglob("*")
        if item.is_file() and not item.is_symlink()
    )


def cache_stats(root: Path) -> dict:
    jobs = root / "jobs"
    directories = [
        p for p in jobs.iterdir()
        if p.is_dir() and re.fullmatch(r"[0-9a-f]{64}", p.name)
    ] if jobs.is_dir() else []
    return {"jobs": len(directories), "bytes": sum(_size(p) for p in directories)}


def clean_cache(root: Path) -> dict:
    """Remove expired jobs, then oldest unlocked jobs above the size budget.

    Runs only when requested or when starting a new transcription.  Jobs that
    are actively locked are skipped, as are recently active jobs when trimming
    for size.  No source audio or published transcript is ever removed.
    """
    jobs = root / "jobs"
    now = time.time()
    candidates = []
    total = 0
    for p in jobs.iterdir() if jobs.is_dir() else ():
        if not p.is_dir() or not re.fullmatch(r"[0-9a-f]{64}", p.name):
            continue
        size = _size(p)
        total += size
        candidates.append((p.stat().st_mtime, p, size))
    removed = 0
    for last_used, path, size in sorted(candidates):
        age = now - last_used
        if age < MAX_AGE_SECONDS and (
            total <= MAX_TOTAL_BYTES or age < MIN_IDLE_SECONDS_FOR_SIZE_CLEANUP
        ):
            continue
        lock = job_lock(root, path.name)
        try:
            lock.acquire(timeout=0)
        except Timeout:
            continue
        try:
            if path.is_dir():
                shutil.rmtree(path)
                total -= size
                removed += 1
        finally:
            lock.release()
    # Crashed remote downloads use a separate temporary area. Do not touch
    # folders that may still be in use by an active transcription.
    downloads = root / "downloads"
    if downloads.is_dir():
        for path in downloads.iterdir():
            if (path.is_dir() and path.name.startswith("download-")
                    and now - path.stat().st_mtime > MAX_AGE_SECONDS):
                shutil.rmtree(path)
    # Lock records contain no audio. Retire abandoned ones so the number of
    # distinct files processed does not grow this directory indefinitely.
    locks = root / "locks"
    if locks.is_dir():
        for path in locks.iterdir():
            if (not path.is_file() or not re.fullmatch(r"[0-9a-f]{64}\.lock", path.name)
                    or now - path.stat().st_mtime <= MAX_AGE_SECONDS
                    or (jobs / path.stem).exists()):
                continue
            lock = FileLock(str(path))
            try:
                lock.acquire(timeout=0)
            except Timeout:
                continue
            else:
                lock.release()
                path.unlink(missing_ok=True)
    return {"removed_jobs": removed, **cache_stats(root)}
