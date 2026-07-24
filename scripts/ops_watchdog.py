#!/usr/bin/env python3
"""Production watchdog for PDF Diff Highlighter.

Silent on success by default. Prints a concise alert when a monitored signal is unhealthy.
Designed for cron/no-agent Telegram delivery.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, build_opener

DEFAULT_SERVICE = "pdf-diff-highlighter-prod.service"
DEFAULT_HEALTH_URL = "https://lab-tim.ru/projects/pdf-diff-highlighter/health"
DEFAULT_VERSION_URL = "https://lab-tim.ru/projects/pdf-diff-highlighter/version"
DEFAULT_METRICS_PATH = "/var/lib/pdf-diff-highlighter/prod/usage_metrics.json"
DEFAULT_ARTIFACTS_PATH = "/var/lib/pdf-diff-highlighter/prod/artifacts"
DEFAULT_DISK_PATH = "/var/lib/pdf-diff-highlighter/prod"

STATUS_RE = re.compile(r'"\S+\s+(?P<path>\S+)\s+HTTP/\S+"\s+(?P<status>\d{3})\s+')


@dataclass
class Alert:
    title: str
    detail: str


def run(cmd: list[str], timeout: int = 10) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, text=True, capture_output=True, timeout=timeout, check=False)


def systemctl_show(service: str) -> dict[str, str]:
    result = run([
        "systemctl",
        "show",
        service,
        "-p",
        "ActiveState",
        "-p",
        "SubState",
        "-p",
        "NRestarts",
        "-p",
        "MainPID",
        "-p",
        "MemoryCurrent",
        "-p",
        "ExecMainStatus",
        "-p",
        "Result",
    ])
    values: dict[str, str] = {}
    for line in result.stdout.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            values[key] = value
    if result.returncode != 0:
        values["systemctl_error"] = result.stderr.strip() or result.stdout.strip()
    return values


def fetch_json(url: str, timeout: int) -> tuple[dict[str, object] | None, str | None, int | None]:
    try:
        opener = build_opener(ProxyHandler({}))
        with opener.open(url, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
            return json.loads(raw), None, response.status
    except HTTPError as exc:
        return None, str(exc), exc.code
    except (URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
        return None, str(exc), None


def parse_recent_status_counts(service: str, since: str) -> tuple[int, int, int, list[str]]:
    result = run(["journalctl", "-q", "-u", service, "--since", since, "--no-pager"], timeout=15)
    five_xx = 0
    four_xx = 0
    restarts = 0
    oom_lines: list[str] = []
    for line in result.stdout.splitlines():
        if "Starting gunicorn" in line or "Started " in line:
            restarts += 1
        if "oom" in line.lower() or "out of memory" in line.lower() or "killed process" in line.lower():
            oom_lines.append(line[-220:])
        match = STATUS_RE.search(line)
        if not match:
            continue
        status = int(match.group("status"))
        if 500 <= status <= 599:
            five_xx += 1
        elif 400 <= status <= 499:
            four_xx += 1
    return five_xx, four_xx, restarts, oom_lines[-3:]


def path_size_bytes(path: Path) -> int:
    if not path.exists():
        return 0
    if path.is_file():
        return path.stat().st_size
    total = 0
    for root, dirs, files in os.walk(path):
        for name in files:
            try:
                total += (Path(root) / name).stat().st_size
            except OSError:
                continue
    return total


def human_bytes(value: int) -> str:
    units = ["B", "KiB", "MiB", "GiB", "TiB"]
    size = float(value)
    for unit in units:
        if size < 1024 or unit == units[-1]:
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{value} B"


def check(args: argparse.Namespace) -> tuple[list[Alert], dict[str, object]]:
    alerts: list[Alert] = []
    details: dict[str, object] = {}

    service_state = systemctl_show(args.service)
    details["service"] = service_state
    if service_state.get("ActiveState") != "active":
        alerts.append(Alert("service down", f"{args.service}: {service_state}"))

    health, health_error, health_status = fetch_json(args.health_url, args.timeout)
    version, version_error, version_status = fetch_json(args.version_url, args.timeout)
    details["health"] = health or {"error": health_error, "status": health_status}
    details["version"] = version or {"error": version_error, "status": version_status}
    if health_error or health_status != 200 or not health or health.get("status") != "ok":
        alerts.append(Alert("health failed", f"{args.health_url}: status={health_status}, error={health_error}, body={health}"))
    if version_error or version_status != 200 or not version:
        alerts.append(Alert("version failed", f"{args.version_url}: status={version_status}, error={version_error}, body={version}"))

    if args.expected_commit:
        actual = str((version or health or {}).get("commit", ""))
        if actual != args.expected_commit:
            alerts.append(Alert("commit mismatch", f"expected={args.expected_commit}, actual={actual or 'missing'}"))

    disk = shutil.disk_usage(args.disk_path)
    used_percent = disk.used / disk.total * 100
    details["disk"] = {"path": args.disk_path, "used_percent": round(used_percent, 1), "free": disk.free}
    if used_percent >= args.disk_threshold_percent:
        alerts.append(Alert("disk threshold", f"{args.disk_path}: used={used_percent:.1f}%, free={human_bytes(disk.free)}"))

    five_xx, four_xx, restarts, oom_lines = parse_recent_status_counts(args.service, args.since)
    details["recent"] = {"since": args.since, "5xx": five_xx, "4xx": four_xx, "restarts": restarts, "oom": oom_lines}
    if five_xx >= args.max_5xx:
        alerts.append(Alert("5xx spike", f"{five_xx} responses with 5xx since {args.since}"))
    if restarts >= args.max_restarts:
        alerts.append(Alert("restart spike", f"{restarts} starts since {args.since}"))
    if oom_lines:
        alerts.append(Alert("possible OOM", "\n".join(oom_lines)))

    metrics = Path(args.metrics_path)
    if not metrics.exists():
        alerts.append(Alert("metrics missing", str(metrics)))
    else:
        details["metrics_size"] = metrics.stat().st_size

    artifacts_size = path_size_bytes(Path(args.artifacts_path))
    details["artifacts_size"] = artifacts_size
    if artifacts_size >= args.artifacts_threshold_bytes:
        alerts.append(Alert("artifact storage threshold", f"{args.artifacts_path}: {human_bytes(artifacts_size)}"))

    return alerts, details


def format_alerts(alerts: list[Alert], details: dict[str, object]) -> str:
    version = details.get("version") if isinstance(details.get("version"), dict) else {}
    commit = version.get("commit", "unknown") if isinstance(version, dict) else "unknown"
    build_time = version.get("build_time", "unknown") if isinstance(version, dict) else "unknown"
    lines = [
        "🚨 PDF Diff Highlighter prod watchdog",
        f"commit: {commit}",
        f"build_time: {build_time}",
        "",
        "Problems:",
    ]
    for alert in alerts:
        lines.append(f"- {alert.title}: {alert.detail}")
    return "\n".join(lines)


def should_emit(alerts: list[Alert], state_file: str, repeat_seconds: int) -> bool:
    if not state_file:
        return True
    state_path = Path(state_file)
    fingerprint_source = "\n".join(sorted(alert.title for alert in alerts))
    fingerprint = hashlib.sha256(fingerprint_source.encode("utf-8")).hexdigest()
    now = time.time()
    previous: dict[str, object] = {}
    if state_path.exists():
        try:
            previous = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            previous = {}
    raw_last_sent = previous.get("last_sent", 0)
    try:
        last_sent = float(raw_last_sent)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        last_sent = 0.0
    last_fingerprint = str(previous.get("fingerprint", ""))
    if last_fingerprint == fingerprint and now - last_sent < repeat_seconds:
        return False
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps({"fingerprint": fingerprint, "last_sent": now}, sort_keys=True), encoding="utf-8")
    return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--service", default=DEFAULT_SERVICE)
    parser.add_argument("--health-url", default=DEFAULT_HEALTH_URL)
    parser.add_argument("--version-url", default=DEFAULT_VERSION_URL)
    parser.add_argument("--metrics-path", default=DEFAULT_METRICS_PATH)
    parser.add_argument("--artifacts-path", default=DEFAULT_ARTIFACTS_PATH)
    parser.add_argument("--disk-path", default=DEFAULT_DISK_PATH)
    parser.add_argument("--expected-commit", default="")
    parser.add_argument("--since", default="15 minutes ago")
    parser.add_argument("--timeout", type=int, default=8)
    parser.add_argument("--disk-threshold-percent", type=float, default=85.0)
    parser.add_argument("--artifacts-threshold-bytes", type=int, default=5 * 1024 * 1024 * 1024)
    parser.add_argument("--max-5xx", type=int, default=3)
    parser.add_argument("--max-restarts", type=int, default=3)
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--state-file", default="")
    parser.add_argument("--repeat-hours", type=float, default=6.0)
    args = parser.parse_args()

    alerts, details = check(args)
    if alerts:
        if should_emit(alerts, args.state_file, int(args.repeat_hours * 3600)):
            print(format_alerts(alerts, details))
            return 2
        return 0
    if args.verbose:
        print(json.dumps(details, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
