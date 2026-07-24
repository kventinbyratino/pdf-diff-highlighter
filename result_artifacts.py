from __future__ import annotations

import os
import re
import secrets
import shutil
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from PIL import Image

from pdf_validation import UserFacingError

DEFAULT_TTL_SECONDS = 30 * 60
DEFAULT_DISK_BUDGET_BYTES = 1024 * 1024 * 1024
MAX_ARTIFACT_BYTES = 2 * 1024 * 1024
JOB_ID_PATTERN = re.compile(r'^[0-9a-f]{32}$')
FILENAME_PATTERN = re.compile(r'^page-[0-9]{3}-(?:source|diff|preview-left|preview-right)\.png$')
ArtifactWriter = Callable[[int, str, Image.Image], str]
_cleanup_worker_lock = threading.Lock()
_cleanup_worker_roots: set[str] = set()


class ArtifactTooLargeError(UserFacingError):
    status_code = 413


def cleanup_expired(root: Path, ttl_seconds: int, *, now: float | None = None) -> int:
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    cutoff = (time.time() if now is None else now) - max(1, ttl_seconds)
    removed = 0
    for candidate in root.iterdir():
        try:
            if candidate.is_dir() and candidate.stat().st_mtime < cutoff:
                shutil.rmtree(candidate)
                removed += 1
        except FileNotFoundError:
            continue
    return removed


def prune_to_budget(root: Path, budget_bytes: int = DEFAULT_DISK_BUDGET_BYTES) -> int:
    jobs: list[tuple[float, int, Path]] = []
    total_bytes = 0
    for candidate in root.iterdir():
        try:
            if not candidate.is_dir():
                continue
            size = sum(path.stat().st_size for path in candidate.iterdir() if path.is_file())
            jobs.append((candidate.stat().st_mtime, size, candidate))
            total_bytes += size
        except FileNotFoundError:
            continue
    removed = 0
    for _mtime, size, candidate in sorted(jobs):
        if total_bytes <= budget_bytes:
            break
        shutil.rmtree(candidate, ignore_errors=True)
        total_bytes -= size
        removed += 1
    return removed


def cleanup_artifacts(root: Path, ttl_seconds: int) -> tuple[int, int]:
    expired = cleanup_expired(root, ttl_seconds)
    over_budget = prune_to_budget(root)
    return expired, over_budget


def _cleanup_worker(
    root: Path,
    ttl_seconds: int,
    interval_seconds: int,
    stop_event: threading.Event,
) -> None:
    while not stop_event.is_set():
        try:
            cleanup_artifacts(root, ttl_seconds)
        except OSError:
            pass
        stop_event.wait(interval_seconds)


def start_cleanup_worker(
    root: Path,
    ttl_seconds: int,
    interval_seconds: int = 60,
    *,
    stop_event: threading.Event | None = None,
) -> threading.Thread | None:
    key = str(root.absolute())
    with _cleanup_worker_lock:
        if key in _cleanup_worker_roots:
            return None
        _cleanup_worker_roots.add(key)
    worker_stop = stop_event or threading.Event()
    worker = threading.Thread(
        target=_cleanup_worker,
        args=(root, ttl_seconds, max(1, interval_seconds), worker_stop),
        name='pdf-result-artifact-cleanup',
        daemon=True,
    )
    worker.start()
    return worker


@dataclass
class ArtifactJob:
    root: Path
    job_id: str

    @property
    def directory(self) -> Path:
        return self.root / self.job_id

    def write_png(self, page_number: int, kind: str, image: Image.Image) -> tuple[str, int]:
        if page_number < 1:
            raise ValueError('page_number must be positive')
        filename = f'page-{page_number:03d}-{kind}.png'
        if not FILENAME_PATTERN.fullmatch(filename):
            raise ValueError('unsupported artifact kind')
        destination = self.directory / filename
        temporary = destination.with_suffix('.png.tmp')
        image.save(temporary, format='PNG')
        os.replace(temporary, destination)
        size = destination.stat().st_size
        if size > MAX_ARTIFACT_BYTES:
            destination.unlink(missing_ok=True)
            raise ArtifactTooLargeError(
                'Результат страницы превышает лимит 2 МБ. Выберите меньшую область или более простой чертёж.'
            )
        return filename, size

    def remove(self) -> None:
        shutil.rmtree(self.directory, ignore_errors=True)


def create_job(root: Path, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> ArtifactJob:
    cleanup_artifacts(root, ttl_seconds)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    while True:
        job = ArtifactJob(root=root, job_id=secrets.token_hex(16))
        try:
            job.directory.mkdir(mode=0o700)
            return job
        except FileExistsError:
            continue


def resolve_artifact(root: Path, ttl_seconds: int, job_id: str, filename: str) -> Path | None:
    if not JOB_ID_PATTERN.fullmatch(job_id) or not FILENAME_PATTERN.fullmatch(filename):
        return None
    job_directory = root / job_id
    try:
        if job_directory.stat().st_mtime < time.time() - max(1, ttl_seconds):
            shutil.rmtree(job_directory, ignore_errors=True)
            return None
    except FileNotFoundError:
        return None
    candidate = job_directory / filename
    try:
        root_resolved = root.resolve()
        candidate_resolved = candidate.resolve(strict=True)
        candidate_resolved.relative_to(root_resolved)
    except (FileNotFoundError, OSError, ValueError):
        return None
    if not candidate_resolved.is_file():
        return None
    return candidate_resolved
