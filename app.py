from __future__ import annotations

import os
import tempfile
from io import BytesIO
from pathlib import Path

from flask import Flask, render_template, request, send_file

from pdf_compare import capture_screen_png, compare_images, compare_pdfs

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024


def _parse_precision() -> int:
    raw = request.form.get('precision', '50')
    try:
        precision = int(raw)
    except (TypeError, ValueError):
        precision = 50
    return max(1, min(100, precision))


def _render_home(**kwargs):
    defaults = {
        'pdf_result': None,
        'image_result': None,
        'error': None,
        'precision': 50,
    }
    defaults.update(kwargs)
    return render_template('index.html', **defaults)


@app.get('/')
def index():
    return _render_home()


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


@app.post('/compare-images')
def compare_image_uploads():
    left = request.files.get('shot1')
    right = request.files.get('shot2')
    precision = _parse_precision()

    if not left or not right:
        return _render_home(error='Нужно выбрать или снять два изображения', precision=precision)

    with tempfile.TemporaryDirectory() as tmpdir:
        left_path = Path(tmpdir) / 'left.png'
        right_path = Path(tmpdir) / 'right.png'
        left.save(left_path)
        right.save(right_path)
        image_result = compare_images(str(left_path), str(right_path), precision=precision)

    return _render_home(image_result=image_result, precision=precision)


@app.get('/capture-screen')
def capture_screen():
    monitor = request.args.get('monitor', '1')
    try:
        png = capture_screen_png(int(monitor))
    except Exception as exc:  # noqa: BLE001 - return the capture error to the UI
        return (str(exc), 503)

    return send_file(
        BytesIO(png),
        mimetype='image/png',
        as_attachment=False,
        download_name='screen.png',
    )


if __name__ == '__main__':
    port = int(os.environ.get('PORT', '8000'))
    app.run(host='0.0.0.0', port=port, debug=True)
