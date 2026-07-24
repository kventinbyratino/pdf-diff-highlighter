#!/usr/bin/env python3
"""Backup and restore the production usage metrics JSON.

Artifacts and job scratch data are intentionally not backed up in the Phase 11 MVP.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_METRICS_PATH = Path("/var/lib/pdf-diff-highlighter/prod/usage_metrics.json")
DEFAULT_BACKUP_DIR = Path("/var/lib/pdf-diff-highlighter/prod/backups/metrics")


def validate_json(path: Path) -> None:
    with path.open("r", encoding="utf-8") as handle:
        json.load(handle)


def backup(metrics_path: Path, backup_dir: Path, keep: int) -> Path:
    if not metrics_path.exists():
        raise FileNotFoundError(f"metrics file not found: {metrics_path}")
    validate_json(metrics_path)
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = backup_dir / f"usage_metrics.{stamp}.json"
    shutil.copy2(metrics_path, target)
    rotate(backup_dir, keep)
    return target


def rotate(backup_dir: Path, keep: int) -> None:
    backups = sorted(backup_dir.glob("usage_metrics.*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in backups[keep:]:
        old.unlink()


def restore(metrics_path: Path, backup_file: Path) -> Path:
    if not backup_file.exists():
        raise FileNotFoundError(f"backup file not found: {backup_file}")
    validate_json(backup_file)
    metrics_path.parent.mkdir(parents=True, exist_ok=True)
    if metrics_path.exists():
        safety = metrics_path.with_suffix(metrics_path.suffix + ".pre-restore")
        shutil.copy2(metrics_path, safety)
    shutil.copy2(backup_file, metrics_path)
    return metrics_path


def latest(backup_dir: Path) -> Path | None:
    backups = sorted(backup_dir.glob("usage_metrics.*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    return backups[0] if backups else None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["backup", "restore", "latest"])
    parser.add_argument("--metrics-path", type=Path, default=DEFAULT_METRICS_PATH)
    parser.add_argument("--backup-dir", type=Path, default=DEFAULT_BACKUP_DIR)
    parser.add_argument("--backup-file", type=Path)
    parser.add_argument("--keep", type=int, default=14)
    args = parser.parse_args()

    try:
        if args.action == "backup":
            target = backup(args.metrics_path, args.backup_dir, args.keep)
            print(target)
        elif args.action == "latest":
            found = latest(args.backup_dir)
            if not found:
                print("no backups found")
                return 1
            print(found)
        elif args.action == "restore":
            backup_file = args.backup_file or latest(args.backup_dir)
            if backup_file is None:
                print("no backup file provided and no backups found", file=sys.stderr)
                return 1
            target = restore(args.metrics_path, backup_file)
            print(target)
        return 0
    except Exception as exc:  # explicit CLI boundary
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
