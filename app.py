from __future__ import annotations

from functools import wraps
import json
import logging
import os
import tempfile
import time
from pathlib import Path
import threading
from types import SimpleNamespace
from typing import Any, Callable
from urllib.parse import urlsplit
import uuid

from flask import Flask, g, jsonify, make_response, render_template, request, send_file, url_for
from werkzeug.exceptions import HTTPException
from werkzeug.middleware.proxy_fix import ProxyFix

from comparison_jobs import ComparisonJobManager
from pdf_compare import compare_pdf_area, compare_pdfs, find_matching_area, render_pdf_page_preview
from pdf_validation import PdfPairMetadata, PdfValidationError, UserFacingError, validate_pdf_pair
from result_artifacts import ArtifactJob, ArtifactWriter, create_job, resolve_artifact, start_cleanup_worker
from usage_metrics import get_metrics, record_comparison, record_uploads, record_visit, resolve_visitor_id

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
app.config['MAX_CONTENT_LENGTH'] = 51 * 1024 * 1024
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'

DEFAULT_PRECISION = 10
MIN_AREA_SIZE = 40
_job_managers_lock = threading.Lock()
_job_managers: dict[str, ComparisonJobManager] = {}
_heavy_operation_gate = threading.BoundedSemaphore(1)


class JsonLogFormatter(logging.Formatter):
    """Keep application logs machine-readable without exposing request bodies/cookies."""

    def format(self, record: logging.LogRecord) -> str:
        if isinstance(record.msg, str) and record.msg.startswith('{'):
            return record.getMessage()
        return super().format(record)


def configure_logging(flask_app: Flask) -> None:
    for handler in flask_app.logger.handlers:
        handler.setFormatter(JsonLogFormatter())
    flask_app.logger.setLevel(os.environ.get('LOG_LEVEL', 'INFO').upper())


def _version_payload() -> dict[str, str]:
    return {
        'environment': app.config.get('APP_ENVIRONMENT', 'local'),
        'commit': app.config.get('RELEASE_COMMIT', 'unknown'),
        'build_time': app.config.get('BUILD_TIME', 'unknown'),
    }


def _log_event(event: str, level: int = logging.INFO, **fields: object) -> None:
    payload: dict[str, object] = {
        'event': event,
        'request_id': getattr(g, 'request_id', None),
        'environment': app.config.get('APP_ENVIRONMENT', 'local'),
        'commit': app.config.get('RELEASE_COMMIT', 'unknown'),
    }
    payload.update({key: value for key, value in fields.items() if value is not None})
    app.logger.log(level, json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':')))


class RequestValidationError(UserFacingError):
    status_code = 400


class ServiceBusyError(UserFacingError):
    status_code = 429


def configure_runtime(flask_app: Flask) -> None:
    metrics_path = os.environ.get('USAGE_METRICS_PATH')
    if metrics_path:
        flask_app.config['USAGE_METRICS_PATH'] = metrics_path
    environment = os.environ.get('APP_ENVIRONMENT', 'local')
    flask_app.config['APP_ENVIRONMENT'] = environment
    flask_app.config['RELEASE_COMMIT'] = os.environ.get('RELEASE_COMMIT', 'unknown')
    flask_app.config['BUILD_TIME'] = os.environ.get('BUILD_TIME', 'unknown')
    default_artifact_root = Path(tempfile.gettempdir()) / f'pdf-diff-highlighter-results-{environment}'
    flask_app.config['RESULT_ARTIFACT_ROOT'] = os.environ.get('RESULT_ARTIFACT_ROOT', str(default_artifact_root))
    try:
        ttl_seconds = int(os.environ.get('RESULT_ARTIFACT_TTL_SECONDS', '1800'))
    except ValueError:
        ttl_seconds = 1800
    flask_app.config['RESULT_ARTIFACT_TTL_SECONDS'] = max(60, ttl_seconds)
    default_job_root = Path(tempfile.gettempdir()) / f'pdf-diff-highlighter-jobs-{environment}'
    flask_app.config['COMPARISON_JOB_ROOT'] = os.environ.get('COMPARISON_JOB_ROOT', str(default_job_root))
    try:
        max_waiting = int(os.environ.get('COMPARISON_JOB_MAX_WAITING', '3'))
    except ValueError:
        max_waiting = 3
    flask_app.config['COMPARISON_JOB_MAX_WAITING'] = max(0, min(10, max_waiting))


configure_runtime(app)
configure_logging(app)
start_cleanup_worker(
    Path(app.config['RESULT_ARTIFACT_ROOT']),
    int(app.config['RESULT_ARTIFACT_TTL_SECONDS']),
)


def _artifact_settings() -> tuple[Path, int]:
    return (
        Path(app.config['RESULT_ARTIFACT_ROOT']),
        int(app.config.get('RESULT_ARTIFACT_TTL_SECONDS', 1800)),
    )


def _job_manager() -> ComparisonJobManager:
    job_root = Path(app.config['COMPARISON_JOB_ROOT'])
    artifact_root, ttl_seconds = _artifact_settings()
    key = f'{job_root.absolute()}::{artifact_root.absolute()}'
    worker_command = app.config.get('COMPARISON_JOB_WORKER_COMMAND')
    if isinstance(worker_command, str):
        worker_command = [worker_command]
    with _job_managers_lock:
        manager = _job_managers.get(key)
        if manager is None:
            manager = ComparisonJobManager(
                job_root,
                artifact_root,
                ttl_seconds=ttl_seconds,
                max_waiting=int(app.config.get('COMPARISON_JOB_MAX_WAITING', 3)),
                worker_command=worker_command,
            )
            _job_managers[key] = manager
        return manager


def _new_artifact_writer() -> tuple[ArtifactJob, ArtifactWriter]:
    root, ttl_seconds = _artifact_settings()
    job = create_job(root, ttl_seconds)

    def write_artifact(page_number: int, kind: str, image) -> str:
        filename, _size = job.write_png(page_number, kind, image)
        return url_for('result_artifact', job_id=job.job_id, filename=filename).lstrip('/')

    return job, write_artifact


def _normalized_origin(value: str) -> tuple[str, str, int] | None:
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in {'http', 'https'} or not parsed.hostname:
            return None
        port = parsed.port or (443 if parsed.scheme == 'https' else 80)
        return parsed.scheme, parsed.hostname.lower(), port
    except ValueError:
        return None


def _consume_request_body() -> None:
    if request.content_length:
        request.get_data(cache=False, parse_form_data=True)


@app.before_request
def prepare_request() -> None:
    g.request_id = uuid.uuid4().hex
    g.request_started_at = time.monotonic()
    if request.method not in {'POST', 'PUT', 'PATCH', 'DELETE'}:
        return

    source = request.headers.get('Origin') or request.headers.get('Referer')
    if source is None:
        return
    if _normalized_origin(source) != _normalized_origin(request.host_url):
        _consume_request_body()
        raise UserFacingError('Запрос с другого сайта отклонён', status_code=403)


@app.after_request
def secure_response(response):
    request_id = getattr(g, 'request_id', uuid.uuid4().hex)
    response.headers['X-Request-ID'] = request_id
    if response.status_code == 429:
        response.headers['Retry-After'] = '10'
    if request.path.startswith('/api/jobs'):
        response.headers['Cache-Control'] = 'no-store'
    duration_ms = round((time.monotonic() - getattr(g, 'request_started_at', time.monotonic())) * 1000, 2)
    log_level = logging.ERROR if response.status_code >= 500 else logging.WARNING if response.status_code >= 400 else logging.INFO
    _log_event(
        'request_completed',
        log_level,
        method=request.method,
        path=request.path,
        endpoint=request.endpoint,
        status=response.status_code,
        duration_ms=duration_ms,
        content_length=request.content_length,
        response_bytes=response.calculate_content_length(),
    )
    return response


def _is_json_error() -> bool:
    return request.path in {'/area-preview', '/detect-area'} or request.path.startswith('/api/jobs')


def _ui_mode() -> str:
    raw = (request.values.get('ui') or '').strip().lower()
    return 'km' if raw == 'km' else 'classic'


def _render_home(**context):
    visitor_id, needs_cookie = resolve_visitor_id(request)
    usage_metrics = context.pop('usage_metrics', None) or record_visit(app, visitor_id)
    ui_mode = _ui_mode()
    defaults = {
        'pdf_result': None,
        'error': None,
        'precision': DEFAULT_PRECISION,
        'align_pages': False,
        'usage_metrics': usage_metrics,
        'ui_mode': ui_mode,
    }
    defaults.update(context)
    template_name = 'index_km.html' if ui_mode == 'km' else 'index.html'
    response = make_response(render_template(template_name, **defaults))
    if needs_cookie:
        response.set_cookie('pdf_diff_visitor', visitor_id, max_age=60 * 60 * 24 * 365 * 2, samesite='Lax')
    return response


def _error_response(message: str, status_code: int):
    request_id = getattr(g, 'request_id', uuid.uuid4().hex)
    if _is_json_error():
        return jsonify({
            'status': 'error',
            'message': message,
            'request_id': request_id,
        }), status_code
    return _render_home(error=message, usage_metrics=get_metrics(app)), status_code


def _http_error_message(status_code: int) -> str:
    return {
        400: 'Проверьте параметры запроса',
        403: 'Запрос с другого сайта отклонён',
        404: 'Страница не найдена',
        405: 'Операция недоступна по этому адресу',
        413: 'Файлы превышают допустимый размер',
        429: 'Другое сравнение уже выполняется. Повторите через несколько секунд',
    }.get(status_code, 'Не удалось обработать запрос')


@app.errorhandler(UserFacingError)
def handle_user_error(exc: UserFacingError):
    return _error_response(str(exc), exc.status_code)


@app.errorhandler(HTTPException)
def handle_http_error(exc: HTTPException):
    return _error_response(_http_error_message(exc.code or 500), exc.code or 500)


@app.errorhandler(Exception)
def handle_unexpected_error(exc: Exception):
    request_id = getattr(g, 'request_id', uuid.uuid4().hex)
    _log_event(
        'request_failed',
        logging.ERROR,
        method=request.method,
        path=request.path,
        endpoint=request.endpoint,
        exception_type=type(exc).__name__,
    )
    return _error_response(f'Не удалось обработать запрос. Код ошибки: {request_id}', 500)


def heavy_operation(view: Callable[..., Any]):
    @wraps(view)
    def wrapped(*args, **kwargs):
        manager = _job_manager()
        if not _heavy_operation_gate.acquire(blocking=False):
            _consume_request_body()
            raise ServiceBusyError('Другое сравнение уже выполняется. Повторите через несколько секунд')
        if not manager.try_begin_interactive():
            _heavy_operation_gate.release()
            _consume_request_body()
            raise ServiceBusyError('Другое сравнение уже выполняется. Повторите через несколько секунд')
        try:
            return view(*args, **kwargs)
        finally:
            manager.end_interactive()
            _heavy_operation_gate.release()

    return wrapped


def _parse_precision() -> int:
    raw = request.form.get('precision', str(DEFAULT_PRECISION))
    try:
        precision = int(raw)
    except (TypeError, ValueError) as exc:
        raise RequestValidationError('Точность должна быть числом от 1 до 100') from exc
    if not 1 <= precision <= 100:
        raise RequestValidationError('Точность должна быть от 1 до 100')
    return precision


def _save_pair(tmpdir: str) -> tuple[Path, Path, PdfPairMetadata]:
    left = request.files.get('pdf1')
    right = request.files.get('pdf2')
    if not left or not right:
        raise RequestValidationError('Нужно выбрать два PDF файла')
    left_path = Path(tmpdir) / 'left.pdf'
    right_path = Path(tmpdir) / 'right.pdf'
    left.save(left_path)
    right.save(right_path)
    metadata = validate_pdf_pair(left_path, right_path)
    return left_path, right_path, metadata


def _parse_rect(prefix: str) -> dict[str, int]:
    try:
        rect = {
            'x': int(round(float(request.form[f'{prefix}[x]']))),
            'y': int(round(float(request.form[f'{prefix}[y]']))),
            'width': int(round(float(request.form[f'{prefix}[width]']))),
            'height': int(round(float(request.form[f'{prefix}[height]']))),
        }
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise RequestValidationError('Некорректные координаты области') from exc
    if rect['x'] < 0 or rect['y'] < 0:
        raise RequestValidationError('Координаты области не могут быть отрицательными')
    if rect['width'] < MIN_AREA_SIZE or rect['height'] < MIN_AREA_SIZE:
        raise RequestValidationError(f'Размер области должен быть минимум {MIN_AREA_SIZE} × {MIN_AREA_SIZE} пикселей')
    return rect


def _parse_page() -> int:
    raw = request.form.get('page', '0')
    try:
        page = int(raw)
    except (TypeError, ValueError) as exc:
        raise RequestValidationError('Некорректный номер листа') from exc
    if page < 0:
        raise RequestValidationError('Некорректный номер листа')
    return page


def _validate_page(page: int, metadata: PdfPairMetadata) -> None:
    if page >= metadata.left.pages or page >= metadata.right.pages:
        raise RequestValidationError('Выбранный лист отсутствует в одном из PDF')


@app.get('/results/<job_id>/<filename>')
def result_artifact(job_id: str, filename: str):
    root, ttl_seconds = _artifact_settings()
    artifact = resolve_artifact(root, ttl_seconds, job_id, filename)
    if artifact is None:
        return '', 404
    response = make_response(send_file(artifact, mimetype='image/png', conditional=True))
    response.headers['Cache-Control'] = f'private, max-age={ttl_seconds}'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    return response


@app.get('/')
def index():
    return _render_home()


@app.get('/health')
def health():
    return jsonify({
        'status': 'ok',
        **_version_payload(),
    })


@app.get('/version')
def version():
    return jsonify(_version_payload())


def _job_token() -> str:
    return request.headers.get('X-Job-Token', '')


def _deserialize_result(payload: dict[str, Any]) -> dict[str, Any]:
    result = dict(payload)
    pages = []
    for raw_page in result.get('pages', []):
        page = dict(raw_page)
        page['text_rows'] = [SimpleNamespace(**row) for row in page.get('text_rows', [])]
        pages.append(SimpleNamespace(**page))
    result['pages'] = pages
    return result


@app.post('/api/jobs')
def create_comparison_job():
    precision = _parse_precision()
    align_pages = request.form.get('align_pages') == 'on'
    visitor_id, needs_cookie = resolve_visitor_id(request)
    with tempfile.TemporaryDirectory() as tmpdir:
        left_path, right_path, _metadata = _save_pair(tmpdir)
        state, token = _job_manager().create_job(
            left_path,
            right_path,
            precision=precision,
            align_pages=align_pages,
            visitor_id=visitor_id,
        )
    record_uploads(app, 2)
    response = make_response(jsonify({'status': 'accepted', 'job': state, 'job_token': token}), 202)
    if needs_cookie:
        response.set_cookie('pdf_diff_visitor', visitor_id, max_age=60 * 60 * 24 * 365 * 2, samesite='Lax')
    return response


@app.get('/api/jobs/<job_id>')
def comparison_job_status(job_id: str):
    return jsonify({'status': 'ok', 'job': _job_manager().public_state(job_id, token=_job_token())})


@app.delete('/api/jobs/<job_id>')
def cancel_comparison_job(job_id: str):
    return jsonify({'status': 'ok', 'job': _job_manager().cancel(job_id, token=_job_token())})


@app.get('/api/jobs/<job_id>/result')
def comparison_job_result(job_id: str):
    manager = _job_manager()
    _state, payload = manager.result(job_id, token=_job_token())
    should_record, visitor_id = manager.claim_metrics(job_id, token=_job_token())
    usage_metrics = record_comparison(app, visitor_id) if should_record else get_metrics(app)
    pdf_result = _deserialize_result(payload)
    return _render_home(
        pdf_result=pdf_result,
        precision=int(pdf_result.get('precision', DEFAULT_PRECISION)),
        align_pages=bool(pdf_result.get('align_pages', False)),
        usage_metrics=usage_metrics,
    )


@app.post('/area-preview')
@heavy_operation
def area_preview():
    job, artifact_writer = _new_artifact_writer()
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            left_path, right_path, metadata = _save_pair(tmpdir)
            page = _parse_page()
            _validate_page(page, metadata)
            record_uploads(app, 2)
            return jsonify({
                'status': 'ok',
                'page_index': page,
                'page_count': min(metadata.left.pages, metadata.right.pages),
                'left': render_pdf_page_preview(
                    str(left_path),
                    page,
                    artifact_writer=artifact_writer,
                    artifact_kind='preview-left',
                ),
                'right': render_pdf_page_preview(
                    str(right_path),
                    page,
                    artifact_writer=artifact_writer,
                    artifact_kind='preview-right',
                ),
            })
    except Exception:
        job.remove()
        raise


@app.post('/detect-area')
@heavy_operation
def detect_area():
    source_rect = _parse_rect('sourceRect')
    with tempfile.TemporaryDirectory() as tmpdir:
        left_path, right_path, metadata = _save_pair(tmpdir)
        page = _parse_page()
        _validate_page(page, metadata)
        record_uploads(app, 2)
        try:
            return jsonify(find_matching_area(str(left_path), str(right_path), source_rect, page_index=page))
        except ValueError as exc:
            raise PdfValidationError('Не удалось найти соответствующую область') from exc


@app.post('/compare-area')
@heavy_operation
def compare_area():
    precision = _parse_precision()
    source_rect = _parse_rect('sourceRect')
    target_rect = _parse_rect('targetRect')
    job, artifact_writer = _new_artifact_writer()
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            left_path, right_path, metadata = _save_pair(tmpdir)
            page = _parse_page()
            _validate_page(page, metadata)
            record_uploads(app, 2)
            try:
                pdf_result = compare_pdf_area(
                    str(left_path),
                    str(right_path),
                    source_rect=source_rect,
                    target_rect=target_rect,
                    page_index=page,
                    precision=precision,
                    artifact_writer=artifact_writer,
                )
            except ValueError as exc:
                raise PdfValidationError('Не удалось сравнить выбранную область') from exc
    except Exception:
        job.remove()
        raise
    usage_metrics = record_comparison(app, resolve_visitor_id(request)[0])
    return _render_home(pdf_result=pdf_result, precision=precision, usage_metrics=usage_metrics)


@app.post('/compare')
@heavy_operation
def compare_pdf():
    precision = _parse_precision()
    align_pages = request.form.get('align_pages') == 'on'
    job, artifact_writer = _new_artifact_writer()
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            left_path, right_path, _metadata = _save_pair(tmpdir)
            record_uploads(app, 2)
            pdf_result = compare_pdfs(
                str(left_path),
                str(right_path),
                precision=precision,
                align_pages=align_pages,
                artifact_writer=artifact_writer,
            )
    except Exception:
        job.remove()
        raise

    usage_metrics = record_comparison(app, resolve_visitor_id(request)[0])
    return _render_home(
        pdf_result=pdf_result,
        precision=precision,
        align_pages=align_pages,
        usage_metrics=usage_metrics,
    )


if __name__ == '__main__':
    port = int(os.environ.get('PORT', '8000'))
    app.run(host='127.0.0.1', port=port, debug=False)
