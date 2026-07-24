#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import tempfile
from pathlib import Path

CASES = [
    ('a4_1_same', 'a4_1_left.pdf', 'a4_1_same.pdf'),
    ('a4_1_changed', 'a4_1_left.pdf', 'a4_1_changed.pdf'),
    ('a4_10_same', 'a4_10_left.pdf', 'a4_10_same.pdf'),
    ('a4_10_changed', 'a4_10_left.pdf', 'a4_10_changed.pdf'),
    ('a1_1_same', 'a1_1_left.pdf', 'a1_1_same.pdf'),
    ('a1_1_changed', 'a1_1_left.pdf', 'a1_1_changed.pdf'),
    ('a0_1_same', 'a0_1_left.pdf', 'a0_1_same.pdf'),
    ('a0_1_changed', 'a0_1_left.pdf', 'a0_1_changed.pdf'),
]
TIME_FIELDS = {
    'Maximum resident set size (kbytes)': 'peak_rss_kib',
    'User time (seconds)': 'user_seconds',
    'System time (seconds)': 'system_seconds',
    'Percent of CPU this job got': 'cpu_percent_text',
}


def parse_time_file(path: Path) -> dict[str, object]:
    metrics: dict[str, object] = {}
    for line in path.read_text().splitlines():
        stripped = line.strip()
        for label, key in TIME_FIELDS.items():
            prefix = f'{label}:'
            if stripped.startswith(prefix):
                value = stripped[len(prefix):].strip()
                if key == 'peak_rss_kib':
                    metrics[key] = int(value)
                elif key in {'user_seconds', 'system_seconds'}:
                    metrics[key] = float(value)
                else:
                    metrics[key] = value
    return metrics


def median_number(records: list[dict[str, object]], key: str) -> float:
    values: list[float] = []
    for record in records:
        value = record[key]
        if not isinstance(value, (int, float, str)):
            raise TypeError(f'{key} is not numeric: {value!r}')
        values.append(float(value))
    return float(statistics.median(values))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--fixtures', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--repeats', type=int, default=2)
    parser.add_argument('--timeout', type=int, default=240)
    args = parser.parse_args()

    project = Path(__file__).resolve().parents[1]
    python = project / '.venv/bin/python'
    worker = project / 'benchmarks/benchmark_worker.py'
    all_runs: dict[str, list[dict[str, object]]] = {}

    for case, left_name, right_name in CASES:
        records: list[dict[str, object]] = []
        for repeat in range(1, args.repeats + 1):
            with tempfile.NamedTemporaryFile(prefix='pdf-diff-time-', delete=False) as handle:
                time_path = Path(handle.name)
            command = [
                '/usr/bin/time', '-v', '-o', str(time_path),
                str(python), str(worker),
                '--left', str(args.fixtures / left_name),
                '--right', str(args.fixtures / right_name),
                '--case', case,
            ]
            environment = os.environ.copy()
            environment['OMP_NUM_THREADS'] = '1'
            environment['OPENBLAS_NUM_THREADS'] = '1'
            completed = subprocess.run(
                command,
                cwd=project,
                env=environment,
                text=True,
                capture_output=True,
                timeout=args.timeout,
                check=False,
            )
            try:
                if completed.returncode != 0:
                    raise RuntimeError(f'{case} run {repeat} failed: {completed.stderr[-2000:]}')
                record = json.loads(completed.stdout.strip().splitlines()[-1])
                record.update(parse_time_file(time_path))
                record['repeat'] = repeat
                records.append(record)
                print(json.dumps(record, sort_keys=True), flush=True)
            finally:
                time_path.unlink(missing_ok=True)
        all_runs[case] = records

    summary = []
    for case, records in all_runs.items():
        representative = dict(records[-1])
        representative.update({
            'case': case,
            'runs': len(records),
            'wall_seconds_median': round(median_number(records, 'wall_seconds'), 4),
            'first_result_seconds_median': round(median_number(records, 'first_result_seconds'), 4),
            'peak_rss_mib_median': round(median_number(records, 'peak_rss_kib') / 1024, 1),
            'html_mib_median': round(median_number(records, 'html_bytes') / 1024 / 1024, 3),
            'png_total_mib_median': round(median_number(records, 'png_total_bytes') / 1024 / 1024, 3),
            'cpu_seconds_median': round(median_number(records, 'user_seconds') + median_number(records, 'system_seconds'), 4),
        })
        summary.append(representative)

    output = {
        'schema_version': 1,
        'render_zoom': 3.0,
        'repeats': args.repeats,
        'cases': summary,
        'raw_runs': all_runs,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, indent=2, sort_keys=True) + '\n')
    print(f'wrote {args.output}')


if __name__ == '__main__':
    main()
