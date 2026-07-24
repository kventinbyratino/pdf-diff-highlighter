from __future__ import annotations

import json
import threading
import uuid
from pathlib import Path
from typing import Any

from flask import Flask, Request

DEFAULT_METRICS = {
    'unique_users': 0,
    'comparisons': 0,
    'uploads': 0,
    'visitors': [],
}

_lock = threading.Lock()


def metrics_path(app: Flask) -> Path:
    configured = app.config.get('USAGE_METRICS_PATH')
    if configured:
        return Path(configured)
    return Path(app.instance_path) / 'usage_metrics.json'


def _normalize(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raw = {}
    visitors = raw.get('visitors', [])
    if not isinstance(visitors, list):
        visitors = []
    normalized_visitors = sorted({str(visitor) for visitor in visitors if str(visitor)})
    comparisons = raw.get('comparisons', 0)
    try:
        comparisons = max(0, int(comparisons))
    except (TypeError, ValueError):
        comparisons = 0
    uploads = raw.get('uploads', 0)
    try:
        uploads = max(0, int(uploads))
    except (TypeError, ValueError):
        uploads = 0
    return {
        'unique_users': len(normalized_visitors),
        'comparisons': comparisons,
        'uploads': uploads,
        'visitors': normalized_visitors,
    }


def _read(path: Path) -> dict[str, Any]:
    if not path.exists():
        return DEFAULT_METRICS.copy()
    try:
        return _normalize(json.loads(path.read_text(encoding='utf-8')))
    except (OSError, json.JSONDecodeError):
        return DEFAULT_METRICS.copy()


def _write(path: Path, metrics: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_normalize(metrics), ensure_ascii=False, indent=2), encoding='utf-8')


def get_metrics(app: Flask) -> dict[str, int]:
    with _lock:
        metrics = _read(metrics_path(app))
    return {
        'unique_users': int(metrics['unique_users']),
        'comparisons': int(metrics['comparisons']),
        'uploads': int(metrics['uploads']),
    }


def resolve_visitor_id(request: Request) -> tuple[str, bool]:
    visitor_id = request.cookies.get('pdf_diff_visitor')
    if visitor_id:
        return visitor_id, False
    return uuid.uuid4().hex, True


def record_visit(app: Flask, visitor_id: str) -> dict[str, int]:
    with _lock:
        path = metrics_path(app)
        metrics = _read(path)
        if visitor_id not in metrics['visitors']:
            metrics['visitors'].append(visitor_id)
            _write(path, metrics)
            metrics = _normalize(metrics)
    return {
        'unique_users': int(metrics['unique_users']),
        'comparisons': int(metrics['comparisons']),
        'uploads': int(metrics['uploads']),
    }


def record_comparison(app: Flask, visitor_id: str) -> dict[str, int]:
    with _lock:
        path = metrics_path(app)
        metrics = _read(path)
        if visitor_id not in metrics['visitors']:
            metrics['visitors'].append(visitor_id)
        metrics['comparisons'] += 1
        _write(path, metrics)
        metrics = _normalize(metrics)
    return {
        'unique_users': int(metrics['unique_users']),
        'comparisons': int(metrics['comparisons']),
        'uploads': int(metrics['uploads']),
    }


def record_uploads(app: Flask, count: int) -> dict[str, int]:
    if count <= 0:
        return get_metrics(app)
    with _lock:
        path = metrics_path(app)
        metrics = _read(path)
        metrics['uploads'] += count
        _write(path, metrics)
        metrics = _normalize(metrics)
    return {
        'unique_users': int(metrics['unique_users']),
        'comparisons': int(metrics['comparisons']),
        'uploads': int(metrics['uploads']),
    }
