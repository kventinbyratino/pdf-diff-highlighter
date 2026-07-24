from pathlib import Path
import json
import logging

from app import app
from scripts import backup_metrics


def test_backup_metrics_roundtrip(tmp_path):
    metrics = tmp_path / "usage_metrics.json"
    metrics.write_text('{"comparisons": 3, "unique_clients": 2}', encoding="utf-8")
    backup_dir = tmp_path / "backups"

    backup_file = backup_metrics.backup(metrics, backup_dir, keep=14)
    assert backup_file.exists()

    metrics.write_text('{"comparisons": 0, "unique_clients": 0}', encoding="utf-8")
    backup_metrics.restore(metrics, backup_file)

    assert metrics.read_text(encoding="utf-8") == '{"comparisons": 3, "unique_clients": 2}'
    assert Path(str(metrics) + ".pre-restore").exists()


def test_backup_metrics_rotation(tmp_path):
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()

    for index in range(4):
        target = backup_dir / f"usage_metrics.20260101T00000{index}Z.json"
        target.write_text(f'{{"comparisons": {index}}}', encoding="utf-8")

    backup_metrics.rotate(backup_dir, keep=2)
    backups = sorted(backup_dir.glob("usage_metrics.*.json"))
    assert len(backups) == 2


def test_ops_watchdog_human_bytes():
    from scripts.ops_watchdog import human_bytes

    assert human_bytes(512) == "512 B"
    assert human_bytes(1024) == "1.0 KiB"


def test_phase11_structured_access_log_has_request_id_commit_and_no_cookie(caplog):
    caplog.set_level(logging.INFO, logger=app.logger.name)

    response = app.test_client().get('/health', headers={'Cookie': 'pdf_diff_visitor=secret-client-id'})

    assert response.status_code == 200
    assert response.headers['X-Request-ID']
    entries = [json.loads(record.message) for record in caplog.records if record.message.startswith('{')]
    completed = [item for item in entries if item.get('event') == 'request_completed']
    assert completed
    last = completed[-1]
    assert last['request_id'] == response.headers['X-Request-ID']
    assert last['path'] == '/health'
    assert last['status'] == 200
    assert last['commit']
    assert last['environment']
    assert 'pdf_diff_visitor' not in caplog.text
    assert 'secret-client-id' not in caplog.text
