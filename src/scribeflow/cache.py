"""Bounded, on-demand cleanup for ScribeFlow-owned retry artifacts."""

from __future__ import annotations

import os
import re
import shutil
import time
from contextlib import contextmanager
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
    total = 0
    for item in path.rglob("*"):
        try:
            if item.is_file() and not item.is_symlink():
                total += item.stat().st_size
        except FileNotFoundError:
            pass  # An active job finished while enumerating its cache.
    return total


def _owned_directories(root: Path, area: str) -> list[Path]:
    parent = root / area
    return [
        path for path in parent.iterdir()
        if not path.is_symlink() and path.is_dir() and re.fullmatch(r"[0-9a-f]{64}", path.name)
    ] if parent.is_dir() else []


@contextmanager
def download_workspace(root: Path, key: str):
    """Serialize one Drive source; downloaded audio is never a retry artifact."""
    path = root / "downloads" / key
    with job_lock(root, key):
        if path.exists():
            shutil.rmtree(path)
        path.mkdir(parents=True, mode=0o700)
        try:
            yield path
        finally:
            shutil.rmtree(path)


def cache_stats(root: Path) -> dict:
    directories = _owned_directories(root, "jobs") + _owned_directories(root, "downloads")
    return {"jobs": len(directories), "bytes": sum(_size(p) for p in directories)}


def clean_cache(root: Path) -> dict:
    """Remove unused downloads and expired/over-budget retry caches on demand."""
    jobs = root / "jobs"
    downloads = root / "downloads"
    now = time.time()
    removed = 0
    for path in _owned_directories(root, "downloads"):
        lock = job_lock(root, path.name)
        try:
            with lock.acquire(timeout=0):
                if path.is_dir():
                    shutil.rmtree(path)
                    removed += 1
        except Timeout:
            pass
    candidates = []
    for path in _owned_directories(root, "jobs"):
        try:
            candidates.append((path.stat().st_mtime, path, _size(path)))
        except FileNotFoundError:
            pass
    total = sum(size for _, _, size in candidates)
    total += sum(_size(path) for path in _owned_directories(root, "downloads"))
    for last_used, path, size in sorted(candidates):
        age = now - last_used
        if not (path / ".published").is_file() and age < MAX_AGE_SECONDS and (
            total <= MAX_TOTAL_BYTES or age < MIN_IDLE_SECONDS_FOR_SIZE_CLEANUP
        ):
            continue
        lock = job_lock(root, path.name)
        try:
            with lock.acquire(timeout=0):
                if path.is_dir():
                    # A retry may have finished after the initial enumeration.
                    if path.stat().st_mtime != last_used:
                        continue
                    shutil.rmtree(path)
                    total -= size
                    removed += 1
        except Timeout:
            pass
    # A lock file is safe to retire only after obtaining the same lock.
    locks = root / "locks"
    if locks.is_dir():
        for path in locks.iterdir():
            try:
                if (not path.is_file() or not re.fullmatch(r"[0-9a-f]{64}\.lock", path.name)
                        or now - path.stat().st_mtime <= MAX_AGE_SECONDS
                        or (jobs / path.stem).exists() or (downloads / path.stem).exists()):
                    continue
            except FileNotFoundError:
                continue
            try:
                with FileLock(str(path)).acquire(timeout=0):
                    if os.name != "nt":
                        path.unlink(missing_ok=True)
            except Timeout:
                pass
    return {"removed_jobs": removed, **cache_stats(root)}
