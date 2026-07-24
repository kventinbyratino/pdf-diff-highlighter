from __future__ import annotations

import sys
import time
from pathlib import Path

import fitz
import pytest

import app as app_module
from app import app
from comparison_jobs import ComparisonJobManager, atomic_write_json, read_json


@pytest.fixture(autouse=True)
def isolated_job_runtime(tmp_path):
    keys = (
        'USAGE_METRICS_PATH',
        'RESULT_ARTIFACT_ROOT',
        'RESULT_ARTIFACT_TTL_SECONDS',
        'COMPARISON_JOB_ROOT',
        'COMPARISON_JOB_MAX_WAITING',
        'COMPARISON_JOB_WORKER_COMMAND',
    )
    previous = {key: app.config.get(key) for key in keys}
    for manager in list(app_module._job_managers.values()):
        manager.shutdown()
    app_module._job_managers.clear()

    app.config['USAGE_METRICS_PATH'] = str(tmp_path / 'metrics.json')
    app.config['RESULT_ARTIFACT_ROOT'] = str(tmp_path / 'results')
    app.config['RESULT_ARTIFACT_TTL_SECONDS'] = 1800
    app.config['COMPARISON_JOB_ROOT'] = str(tmp_path / 'jobs')
    app.config['COMPARISON_JOB_MAX_WAITING'] = 3

    worker_script = tmp_path / 'fake_worker.py'
    worker_script.write_text(
        """
from __future__ import annotations

import argparse
import time
from pathlib import Path

from comparison_jobs import atomic_write_json, read_json, update_job_state


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--job-dir', type=Path, required=True)
    args = parser.parse_args()
    job_dir = args.job_dir
    request = read_json(job_dir / 'request.json')

    steps = [
        ('validating', 10, 'Проверяю PDF'),
        ('rendering', 40, 'Рендерю лист 1 из 1'),
        ('comparing', 70, 'Сравниваю лист 1 из 1'),
        ('finalizing', 90, 'Формирую результат'),
    ]
    for stage, progress, message in steps:
        update_job_state(
            job_dir,
            status='running',
            stage=stage,
            progress=progress,
            message=message,
            error=None,
            page_current=1,
            page_total=1,
        )
        time.sleep(0.35)

    atomic_write_json(job_dir / 'result.json', {
        'left_pages': 1,
        'right_pages': 1,
        'pages': [],
        'changed_pages': 0,
        'precision': int(request['precision']),
        'diff_threshold': 1,
        'align_pages': bool(request['align_pages']),
    })
    update_job_state(
        job_dir,
        status='completed',
        stage='completed',
        progress=100,
        message='Сравнение готово',
        error=None,
        page_current=1,
        page_total=1,
        expires_at=time.time() + int(request.get('ttl_seconds', 1800)),
    )
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
""".lstrip(),
        encoding='utf-8',
    )
    app.config['COMPARISON_JOB_WORKER_COMMAND'] = [sys.executable, str(worker_script)]
    yield

    for manager in list(app_module._job_managers.values()):
        manager.shutdown()
    app_module._job_managers.clear()

    for key, value in previous.items():
        if value is None:
            app.config.pop(key, None)
        else:
            app.config[key] = value


def make_pdf(path: Path, text: str) -> None:
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), text, fontsize=18)
    doc.save(path)
    doc.close()


def wait_for_job_state(client, job_id: str, token: str, *, expected: str | None = None, timeout: float = 10.0):
    deadline = time.monotonic() + timeout
    last_payload = None
    while time.monotonic() < deadline:
        response = client.get(f'/api/jobs/{job_id}', headers={'X-Job-Token': token})
        assert response.status_code == 200
        payload = response.get_json()
        last_payload = payload['job']
        if expected is None:
            if last_payload['status'] in {'completed', 'failed', 'cancelled'}:
                return last_payload
        elif last_payload['status'] == expected:
            return last_payload
        time.sleep(0.1)
    raise AssertionError(f'job {job_id} did not reach {expected or "terminal"}: {last_payload}')


def test_job_api_progress_cancel_and_result(tmp_path):
    left = tmp_path / 'left.pdf'
    right = tmp_path / 'right.pdf'
    make_pdf(left, 'Left sheet')
    make_pdf(right, 'Right sheet')

    client = app.test_client()
    with left.open('rb') as left_file, right.open('rb') as right_file:
        response = client.post(
            '/api/jobs',
            data={
                'pdf1': (left_file, 'left.pdf'),
                'pdf2': (right_file, 'right.pdf'),
                'precision': '60',
                'align_pages': 'on',
            },
            content_type='multipart/form-data',
        )

    assert response.status_code == 202
    payload = response.get_json()
    assert payload['status'] == 'accepted'
    assert payload['job']['status'] in {'queued', 'running'}
    assert payload['job_token']

    job_id = payload['job']['job_id']
    token = payload['job_token']

    running = wait_for_job_state(client, job_id, token, expected='running')
    assert running['progress'] >= 1
    assert running['queue_position'] >= 0

    completed = wait_for_job_state(client, job_id, token, expected='completed')
    assert completed['progress'] == 100

    html_response = client.get(f'/api/jobs/{job_id}/result', headers={'X-Job-Token': token})
    html = html_response.get_data(as_text=True)
    assert html_response.status_code == 200
    assert 'Сравнение листов' in html
    assert 'data-job-progress' in html
    assert 'Сравнить область' in html


def test_job_api_cancel_stops_running_job(tmp_path):
    left = tmp_path / 'left.pdf'
    right = tmp_path / 'right.pdf'
    make_pdf(left, 'Cancel left')
    make_pdf(right, 'Cancel right')

    client = app.test_client()
    with left.open('rb') as left_file, right.open('rb') as right_file:
        response = client.post(
            '/api/jobs',
            data={'pdf1': (left_file, 'left.pdf'), 'pdf2': (right_file, 'right.pdf')},
            content_type='multipart/form-data',
        )
    payload = response.get_json()
    job_id = payload['job']['job_id']
    token = payload['job_token']

    wait_for_job_state(client, job_id, token, expected='running')
    cancel_response = client.delete(f'/api/jobs/{job_id}', headers={'X-Job-Token': token})
    assert cancel_response.status_code == 200

    cancelled = wait_for_job_state(client, job_id, token, expected='cancelled')
    assert cancelled['status'] == 'cancelled'
    assert cancelled['stage'] == 'cancelled'


def test_job_restart_recovery_marks_interrupted_jobs_failed(tmp_path):
    job_root = tmp_path / 'jobs'
    artifact_root = tmp_path / 'results'
    job_root.mkdir()
    artifact_root.mkdir()
    job_id = '0123456789abcdef0123456789abcdef'
    job_dir = job_root / job_id
    job_dir.mkdir()
    (artifact_root / job_id).mkdir()
    (job_dir / 'left.pdf').write_bytes(b'%PDF-1.4\n%EOF\n')
    (job_dir / 'right.pdf').write_bytes(b'%PDF-1.4\n%EOF\n')
    atomic_write_json(job_dir / 'request.json', {
        'precision': 50,
        'align_pages': False,
        'visitor_id': 'visitor',
        'artifact_root': str(artifact_root),
        'ttl_seconds': 1800,
    })
    atomic_write_json(job_dir / 'state.json', {
        'schema_version': 1,
        'job_id': job_id,
        'token_hash': 'x' * 64,
        'status': 'running',
        'stage': 'comparing',
        'progress': 42,
        'message': 'Сравниваю',
        'error': None,
        'created_at': time.time() - 5,
        'updated_at': time.time() - 5,
        'expires_at': time.time() + 1800,
        'page_current': 1,
        'page_total': 1,
        'metrics_recorded': False,
    })

    manager = ComparisonJobManager(job_root, artifact_root)
    try:
        state = read_json(job_dir / 'state.json')
        assert state['status'] == 'failed'
        assert state['stage'] == 'error'
        assert 'перезапуском' in state['message']
        assert not (job_dir / 'left.pdf').exists()
        assert not (artifact_root / job_id).exists()
    finally:
        manager.shutdown()


def test_phase7_template_contains_accessible_job_and_area_controls():
    html = Path('templates/index.html').read_text(encoding='utf-8')

    assert 'data-job-progress aria-live="polite" aria-atomic="true" tabindex="-1"' in html
    assert 'data-job-cancel' in html
    assert 'data-area-page-picker' in html
    assert 'data-area-page aria-label="Лист для сравнения"' in html
    assert 'role="dialog" aria-modal="true" aria-label="Сравнить область"' in html
    assert 'pages|length > 1' in html
    assert 'data-page-target="page-{{ p.page_number }}"' in html
    assert 'aria-label="Перейти к странице {{ p.page_number }}"' in html
    assert 'data-lazy-src="{{ p.left_image_url }}"' in html
    assert 'id="viewer" class="viewer hidden" aria-hidden="true"' in html


def test_phase7_frontend_keeps_shared_area_page_job_restore_and_fullscreen_behaviour():
    source = Path('static/app.js').read_text(encoding='utf-8')

    assert "const selectedPage = document.querySelector('[data-area-page]')?.value || '0';" in source
    assert "data.set('page', selectedPage);" in source
    assert "fetch(areaEndpoint('area-preview'), { method: 'POST', body: areaFormData() })" in source
    assert "fetch(areaEndpoint('detect-area'), { method: 'POST', body: data })" in source
    assert "fetch(areaEndpoint('compare-area'), { method: 'POST', body: data" in source
    assert 'Уверенность: ${percent}%' in source
    assert 'localStorage.setItem(JOB_STORAGE_KEY, JSON.stringify(activeJob))' in source
    assert 'function restoreActiveJob()' in source
    assert "fetch(jobEndpoint(`/${current.id}`), { headers: jobHeaders(), cache: 'no-store' })" in source
    assert "viewerState.scale = Math.min(4, Math.max(1, viewerState.scale + direction * 0.25));" in source
    assert 'if (viewerState.scale <= 1) return;' in source
    assert "if (event.key === 'Escape') closeViewer();" in source


def test_phase7_roadmap_report_documents_mvp_scope():
    report = Path('PHASE_7_TEST_REPORT.md').read_text(encoding='utf-8')

    assert 'Status: `DONE`' not in report  # keep Russian/project wording below as source of truth
    assert 'Статус: `DONE`' in report
    assert 'один общий номер листа' in report
    assert 'Раздельный выбор листов left/right оставлен как будущая настройка' in report
    assert '`PDF-020` закрыт' in report
