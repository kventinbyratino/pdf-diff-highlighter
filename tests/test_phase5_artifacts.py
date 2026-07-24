from __future__ import annotations

import os
import re
import threading
import time
from pathlib import Path

import fitz
import pytest
from PIL import Image

from app import app
from result_artifacts import ArtifactTooLargeError, create_job, prune_to_budget, start_cleanup_worker

RESULT_URL = re.compile(r'(results/[0-9a-f]{32}/page-\d{3}-(?:source|diff)\.png)')


def make_pdf(path: Path, pages: int = 1, changed: bool = False) -> None:
    document = fitz.open()
    for index in range(pages):
        page = document.new_page(width=420, height=300)
        page.insert_text((40, 40), f'Page {index + 1}', fontsize=14)
        page.draw_rect(fitz.Rect(40, 60, 320, 240), color=(0, 0, 0), width=1)
        if changed:
            page.draw_line((80, 90), (280, 210), color=(0, 0, 0), width=1.5)
    document.save(path)
    document.close()


@pytest.fixture(autouse=True)
def isolated_runtime(tmp_path):
    keys = ('USAGE_METRICS_PATH', 'RESULT_ARTIFACT_ROOT', 'RESULT_ARTIFACT_TTL_SECONDS')
    previous = {key: app.config.get(key) for key in keys}
    app.config['USAGE_METRICS_PATH'] = str(tmp_path / 'metrics.json')
    app.config['RESULT_ARTIFACT_ROOT'] = str(tmp_path / 'results')
    app.config['RESULT_ARTIFACT_TTL_SECONDS'] = 1800
    yield
    for key, value in previous.items():
        if value is None:
            app.config.pop(key, None)
        else:
            app.config[key] = value


def post_compare(client, left: Path, right: Path):
    with left.open('rb') as left_file, right.open('rb') as right_file:
        return client.post(
            '/compare',
            data={
                'pdf1': (left_file, 'left.pdf'),
                'pdf2': (right_file, 'right.pdf'),
                'precision': '50',
            },
            headers={'Origin': 'http://localhost'},
            content_type='multipart/form-data',
        )


def test_compare_returns_small_manifest_and_lazy_artifact_urls(tmp_path):
    left = tmp_path / 'left.pdf'
    right = tmp_path / 'right.pdf'
    make_pdf(left, pages=3)
    make_pdf(right, pages=3, changed=True)

    client = app.test_client()
    response = post_compare(client, left, right)
    html = response.get_data(as_text=True)
    urls = RESULT_URL.findall(html)

    assert response.status_code == 200
    assert len(response.data) < 256 * 1024
    assert 'data:image/png;base64,' not in html
    assert 'data-lazy-src=' in html
    assert len(set(urls)) == 6

    artifact_root = Path(app.config['RESULT_ARTIFACT_ROOT'])
    artifacts = sorted(artifact_root.glob('*/*.png'))
    assert len(artifacts) == 6
    assert not list(artifact_root.rglob('*.pdf'))
    assert max(path.stat().st_size for path in artifacts) < 2 * 1024 * 1024

    artifact_response = client.get('/' + urls[0])
    assert artifact_response.status_code == 200
    assert artifact_response.mimetype == 'image/png'
    assert artifact_response.headers['Cache-Control'].startswith('private')
    assert artifact_response.headers['X-Content-Type-Options'] == 'nosniff'


def test_area_preview_uses_urls_instead_of_inline_base64(tmp_path):
    left = tmp_path / 'left.pdf'
    right = tmp_path / 'right.pdf'
    make_pdf(left)
    make_pdf(right, changed=True)

    client = app.test_client()
    with left.open('rb') as left_file, right.open('rb') as right_file:
        response = client.post(
            '/area-preview',
            data={
                'pdf1': (left_file, 'left.pdf'),
                'pdf2': (right_file, 'right.pdf'),
                'page': '0',
            },
            headers={'Origin': 'http://localhost'},
            content_type='multipart/form-data',
        )

    payload = response.get_json()
    assert response.status_code == 200
    for side in ('left', 'right'):
        assert 'image' not in payload[side]
        assert payload[side]['image_url'].startswith('results/')
        assert client.get('/' + payload[side]['image_url']).status_code == 200


def test_expired_and_invalid_artifacts_are_not_served(tmp_path):
    left = tmp_path / 'left.pdf'
    right = tmp_path / 'right.pdf'
    make_pdf(left)
    make_pdf(right, changed=True)
    client = app.test_client()
    response = post_compare(client, left, right)
    url = RESULT_URL.search(response.get_data(as_text=True)).group(1)

    job_dir = next(Path(app.config['RESULT_ARTIFACT_ROOT']).iterdir())
    old = time.time() - 60
    os.utime(job_dir, (old, old))
    app.config['RESULT_ARTIFACT_TTL_SECONDS'] = 1

    assert client.get('/' + url).status_code == 404
    assert not job_dir.exists()
    assert client.get('/results/not-a-job/../../etc/passwd').status_code == 404


def test_artifact_size_and_disk_budget_are_bounded(tmp_path):
    root = tmp_path / 'bounded-results'
    job = create_job(root)
    noisy = Image.frombytes('RGB', (1024, 1024), os.urandom(1024 * 1024 * 3))
    try:
        with pytest.raises(ArtifactTooLargeError, match='2 МБ'):
            job.write_png(1, 'source', noisy)
    finally:
        noisy.close()
    assert not list(job.directory.glob('*.png'))

    older = root / ('a' * 32)
    newer = root / ('b' * 32)
    older.mkdir()
    newer.mkdir()
    (older / 'page-001-source.png').write_bytes(b'a' * 32)
    (newer / 'page-001-source.png').write_bytes(b'b' * 32)
    old = time.time() - 60
    os.utime(older, (old, old))

    assert prune_to_budget(root, budget_bytes=40) >= 1
    assert not older.exists()
    assert newer.exists()


def test_cleanup_worker_removes_expired_jobs_without_new_traffic(tmp_path):
    root = tmp_path / 'periodic-results'
    expired = root / ('c' * 32)
    expired.mkdir(parents=True)
    (expired / 'page-001-source.png').write_bytes(b'png')
    old = time.time() - 60
    os.utime(expired, (old, old))
    stop_event = threading.Event()

    worker = start_cleanup_worker(root, ttl_seconds=1, stop_event=stop_event)
    assert worker is not None
    deadline = time.time() + 1
    while expired.exists() and time.time() < deadline:
        time.sleep(0.01)
    stop_event.set()
    worker.join(timeout=1)

    assert not expired.exists()
    assert not worker.is_alive()


def test_frontend_binds_lazy_results_and_url_area_previews():
    source = Path('static/app.js').read_text(encoding='utf-8')
    template = Path('templates/index.html').read_text(encoding='utf-8')

    assert 'IntersectionObserver' in source
    assert 'bindLazyResultImages' in source
    assert 'new AbortController()' in source
    assert "signal: controller.signal" in source
    assert 'Сравниваю…' in source
    assert 'image_url' in source
    assert 'data-lazy-src' in template
    assert 'data-compare-submit' in template
    assert 'data:image/png;base64,{{ p.diff_image_b64 }}' not in template
