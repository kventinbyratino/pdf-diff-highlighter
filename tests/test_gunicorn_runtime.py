from __future__ import annotations

import http.client
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

import fitz


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return int(sock.getsockname()[1])


def _wait_for_health(url: str, timeout: float = 15) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1) as response:
                if response.status == 200:
                    import json

                    return json.load(response)
        except (OSError, urllib.error.URLError):
            time.sleep(0.1)
    raise AssertionError(f'Gunicorn did not become healthy at {url}')


def _worker_pids(master_pid: int) -> set[int]:
    children = Path(f'/proc/{master_pid}/task/{master_pid}/children')
    if not children.exists():
        return set()
    return {int(value) for value in children.read_text(encoding='utf-8').split()}


def _make_pdf(path: Path, text: str) -> None:
    document = fitz.open()
    page = document.new_page(width=420, height=300)
    page.insert_text((72, 72), text, fontsize=18)
    page.draw_rect(fitz.Rect(72, 120, 220, 200), color=(0, 0, 0), width=2)
    document.save(path)
    document.close()


def _multipart_compare(port: int, left: Path, right: Path) -> tuple[int, bytes]:
    boundary = '----pdf-diff-phase3-boundary'
    body = bytearray()
    for name, value in (('precision', '50'),):
        body.extend(f'--{boundary}\r\n'.encode())
        body.extend(f'Content-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
    for name, path in (('pdf1', left), ('pdf2', right)):
        body.extend(f'--{boundary}\r\n'.encode())
        body.extend(
            f'Content-Disposition: form-data; name="{name}"; filename="{path.name}"\r\n'.encode()
        )
        body.extend(b'Content-Type: application/pdf\r\n\r\n')
        body.extend(path.read_bytes())
        body.extend(b'\r\n')
    body.extend(f'--{boundary}--\r\n'.encode())

    connection = http.client.HTTPConnection('127.0.0.1', port, timeout=180)
    connection.request(
        'POST',
        '/compare',
        body=bytes(body),
        headers={'Content-Type': f'multipart/form-data; boundary={boundary}'},
    )
    response = connection.getresponse()
    payload = response.read()
    status = response.status
    connection.close()
    return status, payload


def test_gunicorn_serves_reload_and_survives_requests(tmp_path):
    port = _free_port()
    metrics_path = tmp_path / 'usage_metrics.json'
    gunicorn = Path(sys.executable).with_name('gunicorn')
    environment = os.environ.copy()
    environment.update(
        {
            'PORT': str(port),
            'APP_ENVIRONMENT': 'phase3-test',
            'RELEASE_COMMIT': 'phase3-local',
            'BUILD_TIME': '2026-07-16T09:00:00Z',
            'USAGE_METRICS_PATH': str(metrics_path),
        }
    )
    process = subprocess.Popen(
        [str(gunicorn), '--config', 'gunicorn.conf.py', 'app:app'],
        cwd=Path(__file__).resolve().parents[1],
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    try:
        health_url = f'http://127.0.0.1:{port}/health'
        health = _wait_for_health(health_url)
        assert health == {
            'status': 'ok',
            'environment': 'phase3-test',
            'commit': 'phase3-local',
            'build_time': '2026-07-16T09:00:00Z',
        }

        initial_workers = _worker_pids(process.pid)
        assert len(initial_workers) == 1
        process.send_signal(signal.SIGHUP)

        deadline = time.monotonic() + 15
        reloaded_workers: set[int] = set()
        while time.monotonic() < deadline:
            reloaded_workers = _worker_pids(process.pid)
            if len(reloaded_workers) == 1 and reloaded_workers != initial_workers:
                break
            time.sleep(0.1)
        assert len(reloaded_workers) == 1
        assert reloaded_workers != initial_workers
        assert _wait_for_health(health_url)['status'] == 'ok'

        try:
            urllib.request.urlopen(f'http://127.0.0.1:{port}/area-preview', timeout=5)
        except urllib.error.HTTPError as error:
            assert error.code == 405
        else:
            raise AssertionError('GET /area-preview must return 405')
        assert _wait_for_health(health_url)['status'] == 'ok'

        left = tmp_path / 'left.pdf'
        right = tmp_path / 'right.pdf'
        _make_pdf(left, 'Drawing A')
        _make_pdf(right, 'Drawing B')
        started = time.monotonic()
        status, payload = _multipart_compare(port, left, right)
        elapsed = time.monotonic() - started
        assert status == 200
        assert b'data-compare-slider' in payload
        assert elapsed < 180
        assert process.poll() is None
    finally:
        process.terminate()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        if process.returncode not in (0, -signal.SIGTERM):
            output = process.stdout.read() if process.stdout else ''
            raise AssertionError(f'Gunicorn exited with {process.returncode}:\n{output}')
