from __future__ import annotations

import os
import tempfile
from pathlib import Path

from flask import Flask, render_template, request

from pdf_compare import compare_pdfs

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024


@app.get('/')
def index():
    return render_template('index.html', result=None)


@app.post('/compare')
def compare():
    left = request.files.get('pdf1')
    right = request.files.get('pdf2')
    if not left or not right:
        return render_template('index.html', result=None, error='Нужно выбрать два PDF файла')

    with tempfile.TemporaryDirectory() as tmpdir:
        left_path = Path(tmpdir) / 'left.pdf'
        right_path = Path(tmpdir) / 'right.pdf'
        left.save(left_path)
        right.save(right_path)
        result = compare_pdfs(str(left_path), str(right_path))

    return render_template('index.html', result=result, error=None)


if __name__ == '__main__':
    port = int(os.environ.get('PORT', '8000'))
    app.run(host='0.0.0.0', port=port, debug=True)
