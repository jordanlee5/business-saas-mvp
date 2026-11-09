"""Read-only local staging checks. Emits fixed codes, never Docker logs or secrets."""
import argparse
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re
import shutil
import subprocess

from tools.staging_pg_backup import verify_backup


def docker(*args):
    result = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=10)
    if result.returncode:
        raise RuntimeError("Docker unavailable")
    return result.stdout


def service_check(project, service):
    # Inspect only these fields: a full inspect includes environment credentials.
    ids = docker("ps", "-aq", "--filter", "label=com.docker.compose.project=" + project,
                 "--filter", "label=com.docker.compose.service=" + service,
                 "--filter", "label=com.docker.compose.oneoff=False").split()
    if len(ids) != 1:
        return "container_missing_or_duplicate"
    template = '{{json .Config.Labels}}\n{{json .State.Status}}\n{{json .State.Health.Status}}'
    # Missing Health is an inspect error and must never count as healthy.
    labels, status, health = [json.loads(line) for line in docker("inspect", "--format", template, ids[0]).splitlines()]
    if (not isinstance(labels, dict) or labels.get("com.docker.compose.project") != project or
            labels.get("com.docker.compose.service") != service):
        return "container_identity_mismatch"
    if status != "running":
        return "container_not_running"
    return "ok" if health == "healthy" else "container_not_healthy"


def backup_check(path, project, now, max_hours):
    manifest = verify_backup(path)
    if manifest.get("source_project") != project:
        return "backup_wrong_project"
    stamp = manifest.get("completed_at")
    if not isinstance(stamp, str):
        return "backup_time_missing"
    completed = datetime.fromisoformat(stamp)
    if completed.tzinfo is None:
        return "backup_time_invalid"
    age = (now - completed).total_seconds()
    if age < -300:
        return "backup_time_invalid"
    return "backup_stale" if age > max_hours * 3600 else "ok"


def inspect_staging(project, disk_paths, backup, *, min_free_gib=5, min_free_percent=10,
                    max_backup_hours=36, now=None):
    if not re.fullmatch(r"saas-pg-test-[a-z0-9][a-z0-9-]{0,48}", project):
        raise ValueError("Invalid dedicated project")
    if (not disk_paths or not all(math.isfinite(v) and v > 0 for v in
            (min_free_gib, min_free_percent, max_backup_hours)) or min_free_percent > 100):
        raise ValueError("Invalid thresholds")
    now = now or datetime.now(timezone.utc)
    checks = []

    def record(name, operation, failure):
        try:
            code = operation()
        except (OSError, ValueError, TypeError, KeyError, RuntimeError, subprocess.SubprocessError):
            code = failure
        checks.append({"check": name, "status": "ok" if code == "ok" else "alert", "code": code})

    for service in ("app", "database", "ocr"):
        record(service, lambda s=service: service_check(project, s), "container_check_failed")
    for index, path in enumerate(disk_paths, 1):
        def disk_check(p=path):
            usage = shutil.disk_usage(p)
            if usage.total <= 0:
                return "disk_check_failed"
            return "disk_low" if (usage.free < min_free_gib * 1024**3 or
                                   usage.free / usage.total * 100 < min_free_percent) else "ok"
        record("disk_" + str(index), disk_check, "disk_check_failed")
    record("backup", lambda: backup_check(backup, project, now, max_backup_hours), "backup_invalid_or_missing")
    return {"format": 1, "checked_at": now.isoformat(),
            "status": "alert" if any(c["status"] != "ok" for c in checks) else "ok", "checks": checks}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--disk-path", type=Path, action="append", required=True)
    parser.add_argument("--backup", type=Path, required=True)
    parser.add_argument("--min-free-gib", type=float, default=5)
    parser.add_argument("--min-free-percent", type=float, default=10)
    parser.add_argument("--max-backup-hours", type=float, default=36)
    args = parser.parse_args(argv)
    try:
        report = inspect_staging(args.project, args.disk_path, args.backup,
            min_free_gib=args.min_free_gib, min_free_percent=args.min_free_percent,
            max_backup_hours=args.max_backup_hours)
    except ValueError:
        print(json.dumps({"status": "alert", "code": "invalid_monitor_configuration"}))
        return 2
    print(json.dumps(report, ensure_ascii=True))
    return 0 if report["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
