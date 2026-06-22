from __future__ import annotations

import os
import tempfile
from pathlib import Path

from flask import Flask, render_template, request

from pdf_compare import compare_pdfs

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


if __name__ == '__main__':
    port = int(os.environ.get('PORT', '8000'))
    app.run(host='0.0.0.0', port=port, debug=True)
