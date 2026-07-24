from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
import uuid
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import fitz


def make_pdf(path: Path, text: str) -> None:
    document = fitz.open()
    page = document.new_page(width=420, height=300)
    page.insert_text((72, 72), text, fontsize=18)
    page.draw_rect(fitz.Rect(72, 120, 220, 200), color=(0, 0, 0), width=2)
    document.save(path)
    document.close()


def request_json(url: str) -> dict:
    with urlopen(url, timeout=10) as response:
        if response.status != 200:
            raise RuntimeError(f'{url} returned HTTP {response.status}')
        return json.loads(response.read().decode('utf-8'))


def multipart_post(url: str, fields: dict[str, str], files: dict[str, Path]) -> tuple[int, str]:
    boundary = '----pdf-diff-smoke-' + uuid.uuid4().hex
    chunks: list[bytes] = []
    for name, value in fields.items():
        chunks.append(f'--{boundary}\r\n'.encode())
        chunks.append(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
        chunks.append(value.encode())
        chunks.append(b'\r\n')
    for name, path in files.items():
        chunks.append(f'--{boundary}\r\n'.encode())
        chunks.append(
            f'Content-Disposition: form-data; name="{name}"; filename="{path.name}"\r\n'
            'Content-Type: application/pdf\r\n\r\n'.encode()
        )
        chunks.append(path.read_bytes())
        chunks.append(b'\r\n')
    chunks.append(f'--{boundary}--\r\n'.encode())
    body = b''.join(chunks)
    request = Request(
        url,
        data=body,
        method='POST',
        headers={'Content-Type': f'multipart/form-data; boundary={boundary}'},
    )
    try:
        with urlopen(request, timeout=60) as response:
            return response.status, response.read().decode('utf-8', errors='replace')
    except HTTPError as exc:
        return exc.code, exc.read().decode('utf-8', errors='replace')


def wait_for_health(base_url: str, attempts: int = 30) -> dict:
    last_error: Exception | None = None
    for _ in range(attempts):
        try:
            return request_json(f'{base_url}/health')
        except (OSError, URLError, RuntimeError, json.JSONDecodeError) as exc:
            last_error = exc
            time.sleep(1)
    raise RuntimeError(f'health check failed: {last_error}')


def main() -> int:
    parser = argparse.ArgumentParser(description='HTTP smoke test for PDF Diff Highlighter')
    parser.add_argument('--base-url', default='http://127.0.0.1:8790')
    parser.add_argument('--expected-commit')
    args = parser.parse_args()

    base_url = args.base_url.rstrip('/')
    health = wait_for_health(base_url)
    version = request_json(f'{base_url}/version')
    if health.get('status') != 'ok':
        raise RuntimeError(f'unexpected health payload: {health}')
    if health.get('commit') != version.get('commit'):
        raise RuntimeError(f'health/version commit mismatch: {health} vs {version}')
    if args.expected_commit and version.get('commit') != args.expected_commit:
        raise RuntimeError(f"expected commit {args.expected_commit}, got {version.get('commit')}")

    with tempfile.TemporaryDirectory() as tmp:
        left = Path(tmp) / 'left.pdf'
        right = Path(tmp) / 'right.pdf'
        make_pdf(left, 'Smoke A')
        make_pdf(right, 'Smoke B')
        status, body = multipart_post(
            f'{base_url}/compare',
            {'precision': '10'},
            {'pdf1': left, 'pdf2': right},
        )
    if status != 200:
        raise RuntimeError(f'/compare returned HTTP {status}: {body[:500]}')
    if 'Сравнить' not in body and 'PDF DIFF' not in body:
        raise RuntimeError('compare response does not look like the application HTML')

    print(json.dumps({'health': health, 'version': version, 'compare_status': status}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
