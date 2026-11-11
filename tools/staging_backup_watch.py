"""Independent staging backup watchdog; fixed-code local events and optional WeCom delivery."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import urllib.request
import uuid

from tools.staging_backup_job import BusyError, exclusive_lock, private_directory, write_json
from tools.staging_monitor import backup_check, service_check
from tools.staging_pg_backup import ROOT


def age_seconds(value, now):
    date = datetime.fromisoformat(value)
    if date.tzinfo is None or (now - date).total_seconds() < -300:
        raise ValueError("Invalid timestamp")
    return (now - date).total_seconds()


def read_record(path, project):
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError("Symlink refused")
    value = json.loads(path.read_text(encoding="utf8"))
    if not isinstance(value, dict) or value.get("project") != project:
        raise ValueError("Wrong project")
    return value


def unit_check(project):
    result = subprocess.run(["systemctl", "show", "saas-staging-backup@" + project + ".service",
        "--property=LoadState,ActiveState,Result"], capture_output=True, text=True, timeout=10)
    fields = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    if result.returncode or fields.get("LoadState") != "loaded":
        return "backup_unit_missing"
    if fields.get("ActiveState") == "failed" or fields.get("Result") not in ("success", None):
        return "backup_unit_failed"
    timer = subprocess.run(["systemctl", "show", "saas-staging-backup@" + project + ".timer",
        "--property=LoadState,ActiveState,UnitFileState"], capture_output=True, text=True, timeout=10)
    fields = dict(line.split("=", 1) for line in timer.stdout.splitlines() if "=" in line)
    if (timer.returncode or fields.get("LoadState") != "loaded" or
            fields.get("ActiveState") != "active" or fields.get("UnitFileState") != "enabled"):
        return "backup_timer_inactive"
    return "ok"


def inspect_backup(project, root, *, first_deadline=None, now=None):
    if (not re.fullmatch(r"saas-pg-test-[a-z0-9][a-z0-9-]{0,48}", project)
            or os.environ.get("DATABASE_URL")):
        raise ValueError("Invalid dedicated project")
    root = Path(root)
    now = now or datetime.now(timezone.utc)
    checks = []

    def add(name, code):
        checks.append(dict(check=name, code=code, status="ok" if code in
                           ("ok", "first_backup_pending", "backup_in_progress") else "alert"))

    running = False
    try:
        if (root / "last-run.json").exists():
            last = read_record(root / "last-run.json", project)
            age = age_seconds(last["started_at"], now)
            if last["status"] == "running":
                running = age <= 15 * 60
                add("last_job", "backup_in_progress" if running else "backup_overdue_running")
            elif last["status"] == "alert":
                add("last_job", "backup_job_failed")
            elif last["status"] != "ok":
                add("last_job", "backup_status_invalid")
            else:
                add("last_job", "ok")
        else:
            add("last_job", "first_backup_pending")
    except Exception:
        add("last_job", "backup_status_invalid")
    try:
        if (root / "last-success.json").exists():
            success = read_record(root / "last-success.json", project)
            if success.get("status") != "ok" or not re.fullmatch(
                    r"backups/[0-9]{8}T[0-9]{6}Z-[a-f0-9]{32}", success.get("backup", "")):
                raise ValueError("Invalid recovery point")
            backup = root / success["backup"]
            if any(p.is_symlink() for p in (backup, *backup.parents)):
                raise ValueError("Symlink refused")
            age_seconds(success["completed_at"], now)
            add("recovery_point", backup_check(backup, project, now, 36))
        else:
            deadline = datetime.fromisoformat(first_deadline) if first_deadline else now
            if deadline.tzinfo is None or (deadline - now).total_seconds() > 36 * 3600:
                raise ValueError("Invalid first-run deadline")
            add("recovery_point", "first_backup_pending" if now < deadline else "backup_missing")
    except Exception:
        add("recovery_point", "backup_invalid")
    try:
        add("scheduler", unit_check(project))
    except Exception:
        add("scheduler", "backup_unit_check_failed")
    for service in ("app", "database", "ocr"):
        try:
            code = service_check(project, service)
            # Only the app is intentionally stopped during a normal backup.
            add(service, "backup_in_progress" if service == "app" and running else code)
        except Exception:
            add(service, "container_check_failed")
    try:
        disk = shutil.disk_usage(root)
        add("disk", "ok" if disk.free >= 5 * 1024**3 and disk.free / disk.total >= .1 else "disk_low")
    except Exception:
        add("disk", "disk_check_failed")
    return dict(format=1, project=project, checked_at=now.isoformat(),
                status="alert" if any(c["status"] == "alert" for c in checks) else "ok", checks=checks)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def send_wecom(secret_file, message):
    """Never log the URL, response body, or exception. Do not follow redirects/proxies."""
    if secret_file is None or not Path(secret_file).exists():
        return "not_configured"
    try:
        path = Path(secret_file).absolute()
        if (path.resolve().is_relative_to(ROOT) or
                any(p.is_symlink() for p in (path, *path.parents)) or
                (os.name != "nt" and path.stat().st_mode & 0o077)):
            return "configuration_invalid"
        url = path.read_text(encoding="utf-8-sig").strip()
        if not re.fullmatch(r"https://qyapi\.weixin\.qq\.com/cgi-bin/webhook/send\?key=[A-Za-z0-9_-]{16,128}", url):
            return "configuration_invalid"
        body = json.dumps({"msgtype": "text", "text": {"content": message}}, ensure_ascii=False).encode()
        if len(message.encode()) > 2048:
            return "message_invalid"
        request = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        with opener.open(request, timeout=10) as response:
            data = json.loads(response.read(8193))
            return "delivered" if response.status == 200 and type(data.get("errcode")) is int and data["errcode"] == 0 else "provider_rejected"
    except Exception:
        return "delivery_failed"


def record_and_notify(report, state_root, secret_file=None, *, now=None, sender=send_wecom):
    now = now or datetime.now(timezone.utc)
    state_root = private_directory(state_root)
    with exclusive_lock(state_root / ".watch.lock"):
        owner = state_root / "project.json"
        if owner.exists():
            read_record(owner, report["project"])
        else:
            write_json(owner, {"project": report["project"]})
        events = private_directory(state_root / "events")
        codes = sorted(c["check"] + ":" + c["code"] for c in report["checks"] if c["status"] == "alert")
        signature = hashlib.sha256(json.dumps(codes).encode()).hexdigest() if codes else "healthy"
        state_path = state_root / "state.json"
        state = read_record(state_path, report["project"]) if state_path.exists() else {"project": report["project"]}
        changed = state.get("observed") != signature
        # Never send an initial healthy message; recovery follows an observed incident.
        due = bool(codes) or bool(state.get("incident_seen"))
        if due:
            due = state.get("delivered_signature") != signature or (bool(codes) and
                  age_seconds(state.get("delivered_at", "1970-01-01T00:00:00+00:00"), now) >= 6 * 3600)
        notification = "not_needed"
        event = dict(project=report["project"], checked_at=report["checked_at"],
                     kind="failure" if codes else "recovery", codes=codes)
        if codes:
            state["incident_seen"] = True
        if changed and (codes or state.get("incident_seen")):
            write_json(events / (uuid.uuid4().hex + ".json"), event)
        if due:
            label = "故障" if codes else "恢复"
            message = "开礼测试备份告警：" + label + "\n项目：" + report["project"] + "\n检查时间(UTC)：" + report["checked_at"]
            message += "\n" + ("；".join(codes) if codes else "备份、服务和磁盘检查已恢复正常。")
            notification = sender(secret_file, message)
            write_json(events / (uuid.uuid4().hex + ".json"), dict(event, delivery=notification))
            if notification == "delivered":
                state.update(delivered_signature=signature, delivered_at=now.isoformat())
                if not codes:
                    state["incident_seen"] = False
        state.update(observed=signature, checked_at=now.isoformat())
        write_json(state_path, state)
        result = dict(report, notification=notification,
                      notification_configured=bool(secret_file and Path(secret_file).exists()))
        write_json(state_root / "latest.json", result)
        return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--state-root", type=Path, required=True)
    parser.add_argument("--first-backup-deadline", default=os.environ.get("STAGING_BACKUP_FIRST_DEADLINE"))
    parser.add_argument("--webhook-file", type=Path)
    parser.add_argument("--require-offsite", action="store_true",
                        default=os.environ.get("STAGING_REQUIRE_OFFSITE") == "1")
    args = parser.parse_args(argv)
    try:
        report = inspect_backup(args.project, args.root, first_deadline=args.first_backup_deadline)
        if args.require_offsite:
            from tools.staging_offsite_backup import offsite_check
            result = offsite_check(args.root, args.project)
            report["checks"].append(dict(check="offsite_backup", code=result,
                                         status="ok" if result == "ok" else "alert"))
            if result != "ok":
                report["status"] = "alert"
        report = record_and_notify(report, args.state_root, args.webhook_file)
        code = 1 if report["status"] == "alert" or report["notification"] not in ("delivered", "not_needed") else 0
    except BusyError:
        code, report = 75, dict(status="skipped", code="watch_already_running")
    except Exception:
        code, report = 2, dict(status="alert", code="watch_configuration_or_state_failed")
    print(json.dumps(report, ensure_ascii=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
