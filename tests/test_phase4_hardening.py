from __future__ import annotations

import io
import json
from pathlib import Path

import fitz
import pytest

import app as app_module
from app import app
from pdf_validation import (
    MAX_FILE_BYTES,
    MAX_PAGES,
    PdfLimitError,
    PdfValidationError,
    validate_pdf_pair,
)


def make_pdf(path: Path, pages: int = 1) -> None:
    document = fitz.open()
    for index in range(pages):
        page = document.new_page(width=420, height=300)
        page.insert_text((72, 72), f'Page {index + 1}', fontsize=18)
    document.save(path)
    document.close()


@pytest.fixture(autouse=True)
def isolated_metrics(tmp_path):
    old_path = app.config.get('USAGE_METRICS_PATH')
    old_artifact_root = app.config.get('RESULT_ARTIFACT_ROOT')
    app.config['USAGE_METRICS_PATH'] = str(tmp_path / 'metrics.json')
    app.config['RESULT_ARTIFACT_ROOT'] = str(tmp_path / 'results')
    yield
    if old_path is None:
        app.config.pop('USAGE_METRICS_PATH', None)
    else:
        app.config['USAGE_METRICS_PATH'] = old_path
    if old_artifact_root is None:
        app.config.pop('RESULT_ARTIFACT_ROOT', None)
    else:
        app.config['RESULT_ARTIFACT_ROOT'] = old_artifact_root


def test_pdf_validation_accepts_real_pair(tmp_path):
    left = tmp_path / 'left.pdf'
    right = tmp_path / 'right.pdf'
    make_pdf(left, 2)
    make_pdf(right, 1)

    result = validate_pdf_pair(left, right)

    assert result.left.pages == 2
    assert result.right.pages == 1
    assert result.total_bytes == left.stat().st_size + right.stat().st_size


@pytest.mark.parametrize(
    ('payload', 'message'),
    [
        (b'', 'пуст'),
        (b'plain text', 'PDF'),
        (b'%PDF-not-a-valid-document', 'поврежд'),
    ],
)
def test_pdf_validation_rejects_invalid_content(tmp_path, payload, message):
    left = tmp_path / 'left.pdf'
    right = tmp_path / 'right.pdf'
    left.write_bytes(payload)
    make_pdf(right)

    with pytest.raises(PdfValidationError, match=message):
        validate_pdf_pair(left, right)


def test_pdf_validation_rejects_page_and_file_limits(tmp_path):
    left = tmp_path / 'left.pdf'
    right = tmp_path / 'right.pdf'
    make_pdf(left, MAX_PAGES + 1)
    make_pdf(right)

    with pytest.raises(PdfLimitError, match='страниц'):
        validate_pdf_pair(left, right)

    left.write_bytes(b'%PDF-')
    with left.open('r+b') as file:
        file.seek(MAX_FILE_BYTES)
        file.write(b'x')

    with pytest.raises(PdfLimitError, match='25 МБ'):
        validate_pdf_pair(left, right)


def test_pdf_validation_rejects_document_without_pages(monkeypatch, tmp_path):
    class EmptyDocument:
        needs_pass = False
        page_count = 0

        def close(self):
            return None

    left = tmp_path / 'left.pdf'
    right = tmp_path / 'right.pdf'
    left.write_bytes(b'%PDF-placeholder')
    right.write_bytes(b'%PDF-placeholder')
    monkeypatch.setattr('pdf_validation.fitz.open', lambda _path: EmptyDocument())

    with pytest.raises(PdfValidationError, match='не содержит страниц'):
        validate_pdf_pair(left, right)


def test_regular_and_area_routes_reject_fake_pdf_without_internal_details():
    client = app.test_client()
    payload = {
        'pdf1': (io.BytesIO(b'plain text'), 'left.pdf'),
        'pdf2': (io.BytesIO(b'%PDF-broken'), 'right.pdf'),
    }
    response = client.post('/compare', data=payload, content_type='multipart/form-data')

    assert response.status_code == 400
    assert 'Файл «Чертеж 1» не является PDF' in response.get_data(as_text=True)
    assert response.headers['X-Request-ID']

    area = client.post(
        '/area-preview',
        data={
            'pdf1': (io.BytesIO(b'%PDF-broken'), 'left.pdf'),
            'pdf2': (io.BytesIO(b'%PDF-broken'), 'right.pdf'),
        },
        content_type='multipart/form-data',
    )
    assert area.status_code == 400
    assert area.is_json
    assert area.get_json()['status'] == 'error'
    assert 'cannot open' not in area.get_data(as_text=True).lower()
    assert area.headers['X-Request-ID'] == area.get_json()['request_id']


def test_invalid_precision_and_rectangles_are_explicit_errors(tmp_path):
    left = tmp_path / 'left.pdf'
    right = tmp_path / 'right.pdf'
    make_pdf(left)
    make_pdf(right)
    client = app.test_client()

    with left.open('rb') as left_file, right.open('rb') as right_file:
        precision = client.post(
            '/compare',
            data={
                'pdf1': (left_file, 'left.pdf'),
                'pdf2': (right_file, 'right.pdf'),
                'precision': 'not-a-number',
            },
            content_type='multipart/form-data',
        )
    assert precision.status_code == 400
    assert 'Точность должна быть числом' in precision.get_data(as_text=True)

    with left.open('rb') as left_file, right.open('rb') as right_file:
        rectangle = client.post(
            '/detect-area',
            data={
                'pdf1': (left_file, 'left.pdf'),
                'pdf2': (right_file, 'right.pdf'),
                'sourceRect[x]': '0',
                'sourceRect[y]': '0',
                'sourceRect[width]': '2',
                'sourceRect[height]': '2',
            },
            content_type='multipart/form-data',
        )
    assert rectangle.status_code == 400
    assert 'минимум 40' in rectangle.get_json()['message']

    with left.open('rb') as left_file, right.open('rb') as right_file:
        non_numeric = client.post(
            '/detect-area',
            data={
                'pdf1': (left_file, 'left.pdf'),
                'pdf2': (right_file, 'right.pdf'),
                'sourceRect[x]': 'not-a-number',
                'sourceRect[y]': '0',
                'sourceRect[width]': '100',
                'sourceRect[height]': '100',
            },
            content_type='multipart/form-data',
        )
    assert non_numeric.status_code == 400
    assert 'Некорректные координаты' in non_numeric.get_json()['message']


def test_unknown_exception_is_logged_but_not_exposed(monkeypatch, caplog, tmp_path):
    left = tmp_path / 'left.pdf'
    right = tmp_path / 'right.pdf'
    make_pdf(left)
    make_pdf(right)

    def fail(*_args, **_kwargs):
        raise RuntimeError('/srv/private/secret.pdf exploded')

    monkeypatch.setattr(app_module, 'compare_pdfs', fail)
    client = app.test_client()
    with left.open('rb') as left_file, right.open('rb') as right_file:
        response = client.post(
            '/compare',
            data={'pdf1': (left_file, 'left.pdf'), 'pdf2': (right_file, 'right.pdf')},
            content_type='multipart/form-data',
        )

    body = response.get_data(as_text=True)
    assert response.status_code == 500
    assert 'Внутренняя ошибка' not in body
    assert 'Не удалось обработать запрос' in body
    assert '/srv/private' not in body
    assert response.headers['X-Request-ID']
    assert '/srv/private/secret.pdf exploded' not in caplog.text
    assert response.headers['X-Request-ID'] in caplog.text
    failure_logs = [json.loads(record.message) for record in caplog.records if record.message.startswith('{')]
    assert any(
        item.get('event') == 'request_failed'
        and item.get('request_id') == response.headers['X-Request-ID']
        and item.get('commit')
        and item.get('exception_type') == 'RuntimeError'
        for item in failure_logs
    )


def test_foreign_browser_origin_is_rejected_but_cli_request_is_allowed():
    client = app.test_client()

    foreign = client.post('/compare', headers={'Origin': 'https://evil.example'})
    same_origin = client.post('/compare', headers={'Origin': 'http://localhost'})
    cli = client.post('/compare')

    assert foreign.status_code == 403
    assert 'другого сайта' in foreign.get_data(as_text=True)
    assert same_origin.status_code == 400
    assert cli.status_code == 400


def test_busy_gate_returns_429_without_starting_comparison():
    assert app_module._heavy_operation_gate.acquire(blocking=False)
    try:
        response = app.test_client().post('/compare')
    finally:
        app_module._heavy_operation_gate.release()

    assert response.status_code == 429
    assert 'уже выполняется' in response.get_data(as_text=True)
    assert response.headers['Retry-After'] == '10'


def test_error_handlers_and_security_headers_are_consistent():
    client = app.test_client()

    not_found = client.get('/missing')
    wrong_method = client.get('/area-preview')

    assert not_found.status_code == 404
    assert wrong_method.status_code == 405
    for response in (not_found, wrong_method):
        assert response.headers['X-Request-ID']


def test_nginx_templates_apply_rate_size_and_security_limits():
    rate_config = Path('deploy/nginx/rate-limit-http.conf').read_text(encoding='utf-8')
    assert 'rate=10r/m' in rate_config
    assert '$request_method' in rate_config
    assert 'zone=pdf_diff_heavy_dev:10m' in rate_config
    assert 'zone=pdf_diff_heavy_prod:10m' in rate_config

    for environment in ('dev', 'prod'):
        config = Path(f'deploy/nginx/{environment}.locations.conf').read_text(encoding='utf-8')
        assert f'limit_req zone=pdf_diff_heavy_{environment} burst=9 nodelay;' in config
        assert 'limit_req_status 429;' in config
        assert 'client_max_body_size 51m;' in config
        assert 'proxy_request_buffering on;' in config
        assert 'proxy_set_header X-Forwarded-Host $http_host;' in config
        assert 'X-Content-Type-Options "nosniff" always;' in config
        assert 'X-Frame-Options "DENY" always;' in config
        assert 'Referrer-Policy "same-origin" always;' in config


def test_limits_are_documented_constants():
    assert MAX_FILE_BYTES == 25 * 1024 * 1024
    assert MAX_PAGES == 20
    assert app.config['MAX_CONTENT_LENGTH'] == 51 * 1024 * 1024
