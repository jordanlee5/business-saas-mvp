"""One scheduled backup of an isolated staging project; no deletion or notifications."""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import shutil
import tempfile
import time
import uuid

from tools.staging_pg_backup import ROOT, StagingProject, verify_backup
from tools.staging_monitor import service_check


class BusyError(Exception):
    pass


def stamp():
    return datetime.now(timezone.utc).isoformat()


def private_directory(path):
    path = Path(os.path.abspath(path))
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError("Symlink not allowed")
    resolved = path.resolve()
    if resolved.is_relative_to(ROOT) or ROOT.is_relative_to(resolved):
        raise ValueError("Use a separate private backup directory")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path, 0o700)
    return path


def write_json(path, value):
    # Replace atomically: a reader never sees half-written status or a pointer.
    fd, temporary = tempfile.mkstemp(prefix=".status-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf8") as stream:
            json.dump(value, stream, ensure_ascii=True, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextmanager
def exclusive_lock(path):
    # Never unlink the lock file: doing so allows two different locked inodes.
    if path.is_symlink():
        raise ValueError("Symlink lock not allowed")
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    with os.fdopen(fd, "r+b") as stream:
        if os.name == "nt":
            import msvcrt
            if os.fstat(stream.fileno()).st_size == 0:
                stream.write(b"0")
                stream.flush()
            stream.seek(0)
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise BusyError from exc
        else:
            import fcntl
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise BusyError from exc
        # Closing releases the OS lock, including when this process crashes.
        yield


def wait_healthy(project, seconds):
    deadline = time.monotonic() + seconds
    while True:
        if all(service_check(project, s) == "ok" for s in ("app", "database")):
            return
        if time.monotonic() >= deadline:
            raise RuntimeError("Services not healthy after backup")
        time.sleep(min(2, max(0, deadline - time.monotonic())))


def run_job(project_name, root, *, min_free_gib=5, health_wait=120):
    if (not re.fullmatch(r"saas-pg-test-[a-z0-9][a-z0-9-]{0,48}", project_name)
            or os.environ.get("DATABASE_URL")
            or not math.isfinite(min_free_gib) or min_free_gib <= 0
            or not math.isfinite(health_wait) or not 0 <= health_wait <= 600):
        raise ValueError("Invalid staging job configuration")
    root = private_directory(root)
    with exclusive_lock(root / ".backup.lock"):
        owner = root / "project.json"
        if owner.exists():
            if owner.is_symlink() or json.loads(owner.read_text()) != {"project": project_name}:
                raise ValueError("Backup root belongs to a different project")
        else:
            write_json(owner, {"project": project_name})
        backups = private_directory(root / "backups")
        runs = private_directory(root / "runs")
        run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex
        report = dict(format=1, project=project_name, run_id=run_id,
                      started_at=stamp(), status="running", code="started")
        write_json(runs / (run_id + ".json"), report)
        write_json(root / "last-run.json", report)
        stage = "preflight_failed"
        result = 1
        try:
            if shutil.disk_usage(root).free < min_free_gib * 1024**3:
                stage = "disk_low"
                raise RuntimeError("Insufficient free space")
            recipient = os.environ.get("STAGING_BACKUP_PUBLIC_KEY")
            if recipient:
                from tools.staging_backup_crypto import public_key, encrypt
                public_key(Path(recipient))  # Fail before any application stop.
            project = StagingProject(project_name)
            # A scheduled job must not unexpectedly start a deliberately stopped app.
            if any(service_check(project_name, s) != "ok" for s in ("app", "database")):
                raise RuntimeError("Services must be healthy before backup")
            stage = "backup_failed"
            destination = backups / run_id
            plain = root / (".plaintext-" + run_id) if recipient else destination
            project.backup(plain)
            stage = "backup_invalid"
            manifest = verify_backup(plain)
            if manifest.get("source_project") != project_name:
                raise ValueError("Wrong backup project")
            stage = "service_recovery_failed"
            wait_healthy(project_name, health_wait)
            if recipient:
                stage = "backup_encryption_failed"
                encrypt(plain, destination, Path(recipient))
                # Only this run's new scratch point, after verified encryption.
                shutil.rmtree(plain)
            report.update(status="ok", code="backup_verified", completed_at=stamp(),
                          backup="backups/" + run_id)
            # Failure at any earlier step preserves the previous recovery point.
            stage = "success_record_failed"
            write_json(root / "last-success.json", report)
            result = 0
        except Exception:
            # Docker errors may include passwords, paths or environment values.
            # Store only a fixed stage code, never raw exception text.
            report.update(status="alert", code=stage, completed_at=stamp())
        write_json(runs / (run_id + ".json"), report)
        write_json(root / "last-run.json", report)
        return result, report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--min-free-gib", default=5, type=float)
    parser.add_argument("--health-wait", default=120, type=float)
    args = parser.parse_args(argv)
    try:
        code, report = run_job(args.project, args.root, min_free_gib=args.min_free_gib,
                               health_wait=args.health_wait)
    except BusyError:
        code, report = 75, dict(status="skipped", code="backup_already_running")
    except Exception:
        code, report = 2, dict(status="alert", code="job_configuration_or_status_write_failed")
    print(json.dumps(report, ensure_ascii=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
