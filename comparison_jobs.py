from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from pdf_validation import UserFacingError
from result_artifacts import JOB_ID_PATTERN

JOB_SCHEMA_VERSION = 1
TERMINAL_STATUSES = {'completed', 'failed', 'cancelled'}
ACTIVE_STATUSES = {'running', 'cancelling'}


class JobQueueFullError(UserFacingError):
    status_code = 429


class JobNotFoundError(UserFacingError):
    status_code = 404


class JobAccessError(UserFacingError):
    status_code = 403


class JobConflictError(UserFacingError):
    status_code = 409


def utc_timestamp() -> float:
    return time.time()


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(payload, ensure_ascii=False, separators=(',', ':')) + '\n', encoding='utf-8')
    os.replace(temporary, path)


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding='utf-8'))


def hash_job_token(token: str) -> str:
    return hashlib.sha256(token.encode('utf-8')).hexdigest()


def update_job_state(job_directory: Path, **changes: Any) -> dict[str, Any]:
    state_path = job_directory / 'state.json'
    state = read_json(state_path)
    state.update(changes)
    state['updated_at'] = utc_timestamp()
    atomic_write_json(state_path, state)
    try:
        os.utime(job_directory, None)
    except FileNotFoundError:
        pass
    return state


class ComparisonJobManager:
    def __init__(
        self,
        root: Path,
        artifact_root: Path,
        *,
        ttl_seconds: int = 1800,
        max_waiting: int = 3,
        worker_command: list[str] | None = None,
        cleanup_interval_seconds: int = 60,
    ) -> None:
        self.root = root
        self.artifact_root = artifact_root
        self.ttl_seconds = max(60, int(ttl_seconds))
        self.max_waiting = max(0, int(max_waiting))
        self.worker_command = worker_command or [sys.executable, str(Path(__file__).with_name('comparison_job_worker.py'))]
        self.cleanup_interval_seconds = max(1, int(cleanup_interval_seconds))
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.artifact_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._condition = threading.Condition(threading.RLock())
        self._pending: list[str] = []
        self._active_job_id: str | None = None
        self._active_process: subprocess.Popen[bytes] | None = None
        self._interactive_active = False
        self._stopping = False
        self._last_cleanup = 0.0
        self._recover_interrupted_jobs()
        self._dispatcher = threading.Thread(target=self._dispatch_loop, name='pdf-comparison-job-dispatcher', daemon=True)
        self._dispatcher.start()

    def _job_directory(self, job_id: str) -> Path:
        if not JOB_ID_PATTERN.fullmatch(job_id):
            raise JobNotFoundError('Задача не найдена')
        return self.root / job_id

    def _state(self, job_id: str) -> dict[str, Any]:
        try:
            return read_json(self._job_directory(job_id) / 'state.json')
        except (FileNotFoundError, OSError, ValueError, json.JSONDecodeError) as exc:
            raise JobNotFoundError('Задача не найдена') from exc

    def _write_state(self, job_id: str, state: dict[str, Any]) -> None:
        state['updated_at'] = utc_timestamp()
        atomic_write_json(self._job_directory(job_id) / 'state.json', state)
        try:
            os.utime(self._job_directory(job_id), None)
        except FileNotFoundError:
            pass

    def _recover_interrupted_jobs(self) -> None:
        now = utc_timestamp()
        for candidate in self.root.iterdir():
            if not candidate.is_dir() or not JOB_ID_PATTERN.fullmatch(candidate.name):
                continue
            try:
                state = read_json(candidate / 'state.json')
            except (FileNotFoundError, OSError, ValueError, json.JSONDecodeError):
                shutil.rmtree(candidate, ignore_errors=True)
                continue
            if float(state.get('expires_at', 0)) <= now:
                self._remove_all(candidate.name)
                continue
            if state.get('status') not in TERMINAL_STATUSES:
                state.update({
                    'status': 'failed',
                    'stage': 'error',
                    'progress': int(state.get('progress', 0)),
                    'message': 'Сравнение прервано перезапуском сервиса',
                    'error': 'Сервис был перезапущен. Запустите сравнение заново.',
                })
                self._write_state(candidate.name, state)
                self._remove_payload(candidate.name, remove_artifacts=True)

    def _remove_payload(self, job_id: str, *, remove_artifacts: bool) -> None:
        directory = self._job_directory(job_id)
        for filename in ('left.pdf', 'right.pdf', 'request.json', 'result.json', 'worker.log'):
            (directory / filename).unlink(missing_ok=True)
        if remove_artifacts:
            shutil.rmtree(self.artifact_root / job_id, ignore_errors=True)

    def _remove_all(self, job_id: str) -> None:
        shutil.rmtree(self.root / job_id, ignore_errors=True)
        shutil.rmtree(self.artifact_root / job_id, ignore_errors=True)

    def _cleanup_expired_locked(self, *, force: bool = False) -> None:
        now = utc_timestamp()
        if not force and now - self._last_cleanup < self.cleanup_interval_seconds:
            return
        self._last_cleanup = now
        protected = {self._active_job_id, *self._pending}
        for candidate in self.root.iterdir():
            if not candidate.is_dir() or candidate.name in protected:
                continue
            try:
                state = read_json(candidate / 'state.json')
                expired = float(state.get('expires_at', 0)) <= now
            except (FileNotFoundError, OSError, ValueError, json.JSONDecodeError):
                expired = True
            if expired:
                self._remove_all(candidate.name)

    def create_job(
        self,
        left_path: Path,
        right_path: Path,
        *,
        precision: int,
        align_pages: bool,
        visitor_id: str,
    ) -> tuple[dict[str, Any], str]:
        with self._condition:
            self._cleanup_expired_locked()
            capacity = self.max_waiting + (0 if self._interactive_active else 1)
            nonterminal_jobs = len(self._pending) + (1 if self._active_job_id else 0)
            if nonterminal_jobs >= capacity:
                raise JobQueueFullError('Очередь сравнений заполнена. Повторите попытку позже.')

            while True:
                job_id = secrets.token_hex(16)
                directory = self.root / job_id
                try:
                    directory.mkdir(mode=0o700)
                    break
                except FileExistsError:
                    continue
            token = secrets.token_urlsafe(32)
            now = utc_timestamp()
            state: dict[str, Any] = {
                'schema_version': JOB_SCHEMA_VERSION,
                'job_id': job_id,
                'token_hash': hash_job_token(token),
                'status': 'queued',
                'stage': 'queued',
                'progress': 0,
                'message': 'Задача добавлена в очередь',
                'error': None,
                'created_at': now,
                'updated_at': now,
                'expires_at': now + self.ttl_seconds,
                'page_current': 0,
                'page_total': 0,
                'metrics_recorded': False,
            }
            try:
                shutil.copyfile(left_path, directory / 'left.pdf')
                shutil.copyfile(right_path, directory / 'right.pdf')
                artifact_directory = self.artifact_root / job_id
                artifact_directory.mkdir(mode=0o700)
                atomic_write_json(directory / 'request.json', {
                    'precision': int(precision),
                    'align_pages': bool(align_pages),
                    'visitor_id': visitor_id,
                    'artifact_root': str(self.artifact_root),
                    'ttl_seconds': self.ttl_seconds,
                })
                self._write_state(job_id, state)
            except Exception:
                self._remove_all(job_id)
                raise
            self._pending.append(job_id)
            self._condition.notify_all()
            return self.public_state(job_id, token=token), token

    def _authorize(self, job_id: str, token: str) -> dict[str, Any]:
        state = self._state(job_id)
        expected = str(state.get('token_hash', ''))
        if not token or not hmac.compare_digest(expected, hash_job_token(token)):
            raise JobAccessError('Нет доступа к задаче')
        return state

    def public_state(self, job_id: str, *, token: str) -> dict[str, Any]:
        with self._condition:
            state = self._authorize(job_id, token)
            queue_position = 0
            if state.get('status') == 'queued':
                try:
                    queue_position = self._pending.index(job_id) + 1
                except ValueError:
                    queue_position = 1
            return {
                key: value
                for key, value in state.items()
                if key not in {'token_hash', 'metrics_recorded'}
            } | {'queue_position': queue_position}

    def result(self, job_id: str, *, token: str) -> tuple[dict[str, Any], dict[str, Any]]:
        with self._condition:
            state = self._authorize(job_id, token)
            if state.get('status') != 'completed':
                raise JobConflictError('Результат ещё не готов')
            try:
                payload = read_json(self._job_directory(job_id) / 'result.json')
            except (FileNotFoundError, OSError, ValueError, json.JSONDecodeError) as exc:
                raise JobConflictError('Результат задачи недоступен') from exc
            return state, payload

    def claim_metrics(self, job_id: str, *, token: str) -> tuple[bool, str]:
        with self._condition:
            state = self._authorize(job_id, token)
            request_payload = read_json(self._job_directory(job_id) / 'request.json')
            if state.get('metrics_recorded'):
                return False, str(request_payload.get('visitor_id', ''))
            state['metrics_recorded'] = True
            self._write_state(job_id, state)
            return True, str(request_payload.get('visitor_id', ''))

    def cancel(self, job_id: str, *, token: str) -> dict[str, Any]:
        process: subprocess.Popen[bytes] | None = None
        with self._condition:
            state = self._authorize(job_id, token)
            status = str(state.get('status'))
            if status == 'cancelled':
                return self.public_state(job_id, token=token)
            if status in {'completed', 'failed'}:
                raise JobConflictError('Завершённую задачу нельзя отменить')
            if job_id in self._pending:
                self._pending.remove(job_id)
                state.update({
                    'status': 'cancelled',
                    'stage': 'cancelled',
                    'message': 'Сравнение отменено',
                    'error': None,
                })
                self._write_state(job_id, state)
                self._remove_payload(job_id, remove_artifacts=True)
                self._condition.notify_all()
                return self.public_state(job_id, token=token)
            if self._active_job_id == job_id:
                state.update({'status': 'cancelling', 'stage': 'cancelling', 'message': 'Останавливаю вычисление'})
                self._write_state(job_id, state)
                process = self._active_process
            else:
                raise JobConflictError('Задача уже не выполняется')

        if process is not None and process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=5)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                if process.poll() is None:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    process.wait(timeout=5)

        with self._condition:
            self._condition.wait_for(lambda: self._active_job_id != job_id, timeout=5)
            state = self._state(job_id)
            if state.get('status') in TERMINAL_STATUSES:
                return self.public_state(job_id, token=token)
            state.update({
                'status': 'cancelled',
                'stage': 'cancelled',
                'message': 'Сравнение отменено',
                'error': None,
            })
            self._write_state(job_id, state)
            self._remove_payload(job_id, remove_artifacts=True)
            self._condition.notify_all()
            return self.public_state(job_id, token=token)

    def try_begin_interactive(self) -> bool:
        with self._condition:
            if self._interactive_active or self._active_job_id is not None or self._pending:
                return False
            self._interactive_active = True
            return True

    def end_interactive(self) -> None:
        with self._condition:
            self._interactive_active = False
            self._condition.notify_all()

    def _dispatch_loop(self) -> None:
        while True:
            with self._condition:
                self._cleanup_expired_locked()
                while not self._stopping and (not self._pending or self._interactive_active or self._active_job_id is not None):
                    self._condition.wait(timeout=1)
                    self._cleanup_expired_locked()
                if self._stopping:
                    return
                job_id = self._pending.pop(0)
                state = self._state(job_id)
                state.update({
                    'status': 'running',
                    'stage': 'preparing',
                    'progress': 1,
                    'message': 'Подготавливаю задачу',
                })
                self._write_state(job_id, state)
                log_handle = (self._job_directory(job_id) / 'worker.log').open('ab')
                try:
                    env = os.environ.copy()
                    repo_root = str(Path(__file__).resolve().parent)
                    existing_pythonpath = env.get('PYTHONPATH')
                    env['PYTHONPATH'] = repo_root if not existing_pythonpath else f'{repo_root}{os.pathsep}{existing_pythonpath}'
                    process = subprocess.Popen(
                        [*self.worker_command, '--job-dir', str(self._job_directory(job_id))],
                        cwd=str(Path(__file__).parent),
                        env=env,
                        stdout=log_handle,
                        stderr=subprocess.STDOUT,
                        start_new_session=True,
                    )
                except Exception:
                    log_handle.close()
                    state.update({
                        'status': 'failed',
                        'stage': 'error',
                        'message': 'Не удалось запустить сравнение',
                        'error': 'Не удалось запустить вычисление. Повторите попытку.',
                    })
                    self._write_state(job_id, state)
                    self._remove_payload(job_id, remove_artifacts=True)
                    self._condition.notify_all()
                    continue
                self._active_job_id = job_id
                self._active_process = process

            return_code = process.wait()
            log_handle.close()
            with self._condition:
                try:
                    state = self._state(job_id)
                except JobNotFoundError:
                    state = {}
                status = state.get('status')
                if status in {'cancelling', 'cancelled'}:
                    state.update({'status': 'cancelled', 'stage': 'cancelled', 'message': 'Сравнение отменено', 'error': None})
                    self._write_state(job_id, state)
                    self._remove_payload(job_id, remove_artifacts=True)
                elif return_code == 0 and status == 'completed':
                    for filename in ('left.pdf', 'right.pdf', 'worker.log'):
                        (self._job_directory(job_id) / filename).unlink(missing_ok=True)
                else:
                    state.update({
                        'status': 'failed',
                        'stage': 'error',
                        'message': 'Сравнение завершилось с ошибкой',
                        'error': state.get('error') or 'Не удалось сравнить PDF. Повторите попытку.',
                    })
                    self._write_state(job_id, state)
                    self._remove_payload(job_id, remove_artifacts=True)
                self._active_job_id = None
                self._active_process = None
                self._condition.notify_all()

    def shutdown(self) -> None:
        process: subprocess.Popen[bytes] | None
        with self._condition:
            self._stopping = True
            process = self._active_process
            self._condition.notify_all()
        if process is not None and process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        self._dispatcher.join(timeout=5)
