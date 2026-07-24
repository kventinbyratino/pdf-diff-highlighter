#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

PNG_PATTERN = re.compile(rb'data:image/png;base64,([A-Za-z0-9+/=]+)')
ARTIFACT_URL_PATTERN = re.compile(rb'(?:/)?results/[0-9a-f]{32}/page-[0-9]{3}-(?:source|diff)\.png')


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--left', type=Path, required=True)
    parser.add_argument('--right', type=Path, required=True)
    parser.add_argument('--case', required=True)
    parser.add_argument('--align-pages', action='store_true')
    args = parser.parse_args()

    with tempfile.TemporaryDirectory(prefix='pdf-diff-benchmark-') as tmpdir:
        os.environ['APP_ENVIRONMENT'] = 'benchmark'
        os.environ['RELEASE_COMMIT'] = 'phase5-baseline'
        os.environ['USAGE_METRICS_PATH'] = str(Path(tmpdir) / 'metrics.json')
        artifact_root = Path(tmpdir) / 'results'
        os.environ['RESULT_ARTIFACT_ROOT'] = str(artifact_root)

        from app import app

        app.config['TESTING'] = True
        client = app.test_client()
        started = time.perf_counter()
        with args.left.open('rb') as left, args.right.open('rb') as right:
            response = client.post(
                '/compare',
                data={
                    'pdf1': (left, args.left.name),
                    'pdf2': (right, args.right.name),
                    'precision': '50',
                    **({'align_pages': 'on'} if args.align_pages else {}),
                },
                headers={'Origin': 'http://localhost'},
                content_type='multipart/form-data',
            )
        elapsed = time.perf_counter() - started
        body = response.data
        encoded_images = PNG_PATTERN.findall(body)
        unique_encoded_images = list(dict.fromkeys(encoded_images))
        inline_png_sizes = [len(base64.b64decode(value)) for value in unique_encoded_images]
        artifact_paths = sorted(artifact_root.glob('*/*.png'))
        artifact_png_sizes = [path.stat().st_size for path in artifact_paths]
        png_sizes = artifact_png_sizes or inline_png_sizes
        png_reference_count = len(ARTIFACT_URL_PATTERN.findall(body)) or len(encoded_images)
        result = {
            'case': args.case,
            'align_pages': args.align_pages,
            'status_code': response.status_code,
            'wall_seconds': round(elapsed, 6),
            'first_result_seconds': round(elapsed, 6),
            'first_result_note': 'Response is buffered; no bytes are available before the full HTML is rendered.',
            'html_bytes': len(body),
            'png_reference_count': png_reference_count,
            'png_unique_count': len(png_sizes),
            'png_total_bytes': sum(png_sizes),
            'png_max_bytes': max(png_sizes, default=0),
            'input_total_bytes': args.left.stat().st_size + args.right.stat().st_size,
        }
        print(json.dumps(result, sort_keys=True))


if __name__ == '__main__':
    main()
