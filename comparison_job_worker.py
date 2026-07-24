from __future__ import annotations

import argparse
import ctypes
import json
import os
import signal
import sys
import time
import traceback
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

from comparison_jobs import atomic_write_json, read_json, update_job_state
from pdf_compare import compare_pdfs
from pdf_validation import UserFacingError, validate_pdf_pair
from result_artifacts import ArtifactJob


def bind_lifetime_to_parent() -> None:
    if not sys.platform.startswith('linux'):
        return
    parent_pid = os.getppid()
    libc = ctypes.CDLL(None)
    if libc.prctl(1, signal.SIGTERM) != 0:
        raise OSError('prctl(PR_SET_PDEATHSIG) failed')
    if os.getppid() != parent_pid:
        os.kill(os.getpid(), signal.SIGTERM)


def serialize(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return {key: serialize(item) for key, item in asdict(value).items()}
    if isinstance(value, dict):
        return {str(key): serialize(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [serialize(item) for item in value]
    return value


def run(job_directory: Path) -> int:
    bind_lifetime_to_parent()
    request_payload = read_json(job_directory / 'request.json')
    left_path = job_directory / 'left.pdf'
    right_path = job_directory / 'right.pdf'
    artifact_root = Path(str(request_payload['artifact_root']))
    artifact_job = ArtifactJob(root=artifact_root, job_id=job_directory.name)

    def set_progress(stage: str, progress: int, message: str, *, current: int = 0, total: int = 0) -> None:
        update_job_state(
            job_directory,
            status='running',
            stage=stage,
            progress=max(0, min(99, int(progress))),
            message=message,
            error=None,
            page_current=current,
            page_total=total,
        )

    def write_artifact(page_number: int, kind: str, image) -> str:
        filename, _size = artifact_job.write_png(page_number, kind, image)
        return f'results/{job_directory.name}/{filename}'

    try:
        set_progress('validating', 3, 'Проверяю PDF')
        metadata = validate_pdf_pair(left_path, right_path)
        total_pages = max(metadata.left.pages, metadata.right.pages)
        set_progress('preparing', 7, 'Подготавливаю страницы', total=total_pages)

        def on_progress(stage: str, current: int, total: int) -> None:
            total = max(1, total)
            page = max(1, min(current, total))
            if stage == 'rendering':
                progress = 8 + round(78 * ((page - 1) / total))
                message = f'Рендерю лист {page} из {total}'
            else:
                progress = 8 + round(78 * ((page - 0.35) / total))
                message = f'Сравниваю лист {page} из {total}'
            set_progress(stage, progress, message, current=page, total=total)

        result = compare_pdfs(
            str(left_path),
            str(right_path),
            precision=int(request_payload['precision']),
            align_pages=bool(request_payload['align_pages']),
            artifact_writer=write_artifact,
            progress_callback=on_progress,
        )
        set_progress('finalizing', 92, 'Формирую результат', current=total_pages, total=total_pages)
        atomic_write_json(job_directory / 'result.json', serialize(result))
        update_job_state(
            job_directory,
            status='completed',
            stage='completed',
            progress=100,
            message='Сравнение готово',
            error=None,
            page_current=total_pages,
            page_total=total_pages,
            expires_at=time.time() + int(request_payload.get('ttl_seconds', 1800)),
        )
        return 0
    except UserFacingError as exc:
        update_job_state(
            job_directory,
            status='failed',
            stage='error',
            message='Сравнение завершилось с ошибкой',
            error=str(exc),
        )
        return 1
    except Exception:
        traceback.print_exc()
        update_job_state(
            job_directory,
            status='failed',
            stage='error',
            message='Сравнение завершилось с ошибкой',
            error='Не удалось сравнить PDF. Повторите попытку.',
        )
        return 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--job-dir', type=Path, required=True)
    args = parser.parse_args()
    return run(args.job_dir)


if __name__ == '__main__':
    raise SystemExit(main())
