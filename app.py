from __future__ import annotations

import os
import tempfile
from pathlib import Path

from flask import Flask, jsonify, render_template, request

from pdf_compare import compare_pdf_area, compare_pdfs, find_matching_area, render_pdf_page_preview

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024

DEFAULT_PRECISION = 50


def _parse_precision() -> int:
    raw = request.form.get('precision', str(DEFAULT_PRECISION))
    try:
        precision = int(raw)
    except (TypeError, ValueError):
        precision = DEFAULT_PRECISION
    return max(1, min(100, precision))


def _render_home(**context):
    defaults = {
        'pdf_result': None,
        'error': None,
        'precision': DEFAULT_PRECISION,
    }
    defaults.update(context)
    return render_template('index.html', **defaults)


def _save_pair(tmpdir: str):
    left = request.files.get('pdf1')
    right = request.files.get('pdf2')
    if not left or not right:
        raise ValueError('Нужно выбрать два PDF файла')
    left_path = Path(tmpdir) / 'left.pdf'
    right_path = Path(tmpdir) / 'right.pdf'
    left.save(left_path)
    right.save(right_path)
    return left_path, right_path


def _parse_rect(prefix: str) -> dict[str, int]:
    try:
        return {
            'x': int(round(float(request.form[f'{prefix}[x]']))),
            'y': int(round(float(request.form[f'{prefix}[y]']))),
            'width': int(round(float(request.form[f'{prefix}[width]']))),
            'height': int(round(float(request.form[f'{prefix}[height]']))),
        }
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError('Некорректные координаты области') from exc


def _parse_page() -> int:
    try:
        return max(0, int(request.form.get('page', '0')))
    except (TypeError, ValueError):
        return 0


@app.get('/')
def index():
    return _render_home()


@app.post('/area-preview')
def area_preview():
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            left_path, right_path = _save_pair(tmpdir)
            page = _parse_page()
            return jsonify({
                'status': 'ok',
                'left': render_pdf_page_preview(str(left_path), page),
                'right': render_pdf_page_preview(str(right_path), page),
            })
    except Exception as exc:  # returned to UI
        return jsonify({'status': 'error', 'message': str(exc)}), 400


@app.post('/detect-area')
def detect_area():
    try:
        source_rect = _parse_rect('sourceRect')
        with tempfile.TemporaryDirectory() as tmpdir:
            left_path, right_path = _save_pair(tmpdir)
            result = find_matching_area(str(left_path), str(right_path), source_rect, page_index=_parse_page())
            return jsonify(result)
    except Exception as exc:  # returned to UI
        return jsonify({'status': 'error', 'message': str(exc)}), 400


@app.post('/compare-area')
def compare_area():
    precision = _parse_precision()
    try:
        source_rect = _parse_rect('sourceRect')
        target_rect = _parse_rect('targetRect')
        with tempfile.TemporaryDirectory() as tmpdir:
            left_path, right_path = _save_pair(tmpdir)
            pdf_result = compare_pdf_area(
                str(left_path),
                str(right_path),
                source_rect=source_rect,
                target_rect=target_rect,
                page_index=_parse_page(),
                precision=precision,
            )
        return _render_home(pdf_result=pdf_result, precision=precision)
    except Exception as exc:
        return _render_home(error=str(exc), precision=precision)


@app.post('/compare')
def compare_pdf():
    left = request.files.get('pdf1')
    right = request.files.get('pdf2')
    precision = _parse_precision()

    if not left or not right:
        return _render_home(error='Нужно выбрать два PDF файла', precision=precision)

    with tempfile.TemporaryDirectory() as tmpdir:
        left_path = Path(tmpdir) / 'left.pdf'
        right_path = Path(tmpdir) / 'right.pdf'
        left.save(left_path)
        right.save(right_path)
        pdf_result = compare_pdfs(str(left_path), str(right_path), precision=precision)

    return _render_home(pdf_result=pdf_result, precision=precision)


if __name__ == '__main__':
    port = int(os.environ.get('PORT', '8000'))
    app.run(host='0.0.0.0', port=port, debug=True)
