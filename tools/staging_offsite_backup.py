"""Pull verified staging recovery points to a second machine; bounded retention.

No database access, container stops, or automatic restore. Private config stays off Git.
"""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading

from tools.staging_backup_job import private_directory, write_json, exclusive_lock, stamp, BusyError
from tools.staging_pg_backup import verify_backup, digest

RUN = re.compile(r"[0-9]{8}T[0-9]{6}Z-[a-f0-9]{32}")


def read_json(path):
    if any(is_link(p) for p in (path, *path.parents)):
        raise ValueError("Links refused")
    return json.loads(path.read_text(encoding="utf8"))


def is_link(path):
    # Windows junctions are reparse points too, not necessarily is_symlink().
    return path.is_symlink() or (path.exists() and bool(getattr(path.lstat(), "st_file_attributes", 0) & 0x400))


def owned_root(root, project, create=False):
    if os.environ.get("DATABASE_URL") or not re.fullmatch(r"saas-pg-test-[a-z0-9][a-z0-9-]{0,48}", project):
        raise ValueError("Staging configuration required")
    root = Path(os.path.abspath(root))
    if any(is_link(p) for p in (root, *root.parents)):
        raise ValueError("Links refused")
    root = private_directory(root)
    owner = root / "project.json"
    if not owner.exists() and create:
        write_json(owner, {"project": project})
    if read_json(owner) != {"project": project}:
        raise ValueError("Project mismatch")
    return root


def point(root, run_id, project):
    if not RUN.fullmatch(run_id):
        raise ValueError("Invalid recovery point")
    path = root / "backups" / run_id
    if any(is_link(p) for p in (path, *path.parents)):
        raise ValueError("Links refused")
    manifest = verify_backup(path)
    if manifest.get("source_project") != project:
        raise ValueError("Source mismatch")
    return path


def latest(root, project):
    report = read_json(root / "last-success.json")
    run_id = report["run_id"]
    if (report.get("project") != project or report.get("status") != "ok"
            or report.get("backup") != "backups/" + run_id):
        raise ValueError("Invalid success pointer")
    return run_id, point(root, run_id, project)


def export_latest(root, project, stream):
    root = owned_root(root, project)
    # Same lock as nightly backup and retention: never copy a point being changed.
    with exclusive_lock(root / ".backup.lock"):
        run_id, path = latest(root, project)
        with tarfile.open(fileobj=stream, mode="w|") as archive:
            archive.add(path, arcname=run_id, recursive=True)


def receive(archive_path, root, project, max_bytes=20 * 1024**3):
    """Extract regular files only into a private staging dir; publish after full hash check."""
    root = owned_root(root, project, create=True)
    backups = private_directory(root / "backups")
    work = Path(tempfile.mkdtemp(prefix=".incoming-", dir=root))
    try:
        run_id = None
        total = 0
        seen = set()
        with tarfile.open(archive_path, "r:") as archive:
            for index, member in enumerate(archive):
                name = PurePosixPath(member.name)
                if (index >= 100000 or name.is_absolute() or ".." in name.parts
                        or "\\" in member.name or ":" in member.name or not name.parts
                        or not RUN.fullmatch(name.parts[0]) or member.name in seen
                        or not (member.isdir() or member.isfile())):
                    raise ValueError("Unsafe archive")
                seen.add(member.name)
                run_id = run_id or name.parts[0]
                if name.parts[0] != run_id:
                    raise ValueError("Mixed recovery points")
                total += member.size
                if member.size < 0 or total > max_bytes or shutil.disk_usage(root).free < member.size + 1024**3:
                    raise ValueError("Archive size or disk limit")
                target = work.joinpath(*name.parts)
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True, mode=0o700)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    with archive.extractfile(member) as source, target.open("xb") as dest:
                        shutil.copyfileobj(source, dest)
                    os.chmod(target, 0o600)
        if run_id is None:
            raise ValueError("Empty archive")
        incoming = work / run_id
        manifest = verify_backup(incoming)
        if manifest.get("source_project") != project:
            raise ValueError("Source mismatch")
        fingerprint = digest(incoming / "manifest.json")
        dest = backups / run_id
        if dest.exists():
            point(root, run_id, project)
            if digest(dest / "manifest.json") != fingerprint:
                raise ValueError("Conflicting recovery point")
        else:
            incoming.rename(dest)
        report = dict(format=1, project=project, run_id=run_id, status="ok",
                      backup="backups/" + run_id, manifest_sha256=fingerprint, completed_at=stamp())
        write_json(root / "last-success.json", report)
        return report
    finally:
        # Only our freshly-created temporary directory; no archive links extracted.
        shutil.rmtree(work)


def prune(root, project, keep, require_receipt):
    """Caller owns .backup.lock. Never touch unknown, failed, or unacknowledged points."""
    if not isinstance(keep, int) or not 2 <= keep <= 365:
        raise ValueError("Retention must keep at least two points")
    protected, _ = latest(root, project)
    candidates = []
    for path in (root / "backups").iterdir():
        if not RUN.fullmatch(path.name) or is_link(path) or not path.is_dir():
            continue
        try:
            point(root, path.name, project)
            if require_receipt:
                receipt = read_json(root / "receipts" / (path.name + ".json"))
                if (receipt.get("project") != project or receipt.get("run_id") != path.name
                        or receipt.get("manifest_sha256") != digest(path / "manifest.json")):
                    continue
            candidates.append(path)
        except (ValueError, OSError, KeyError):
            continue
    candidates.sort(key=lambda p: p.name, reverse=True)
    removed = []
    for path in candidates[keep:]:
        if path.name == protected:
            continue
        # Recheck immediately before deleting; all cooperating writers hold this lock.
        point(root, path.name, project)
        if path.resolve().parent != (root / "backups").resolve():
            raise ValueError("Deletion outside managed root")
        shutil.rmtree(path)
        removed.append(path.name)
    return removed


def acknowledge(root, project, run_id, fingerprint, keep=7):
    root = owned_root(root, project)
    with exclusive_lock(root / ".backup.lock"):
        path = point(root, run_id, project)
        if digest(path / "manifest.json") != fingerprint:
            raise ValueError("Receipt mismatch")
        receipts = private_directory(root / "receipts")
        write_json(receipts / (run_id + ".json"), dict(project=project, run_id=run_id,
                   manifest_sha256=fingerprint, received_at=stamp()))
        removed = prune(root, project, keep, True)
        report = dict(project=project, status="ok", run_id=run_id,
                      completed_at=stamp(), retained_minimum=keep, removed=removed)
        write_json(root / "last-offsite.json", report)
        return report


def offsite_check(root, project, now=None):
    """Server-side deadline: detects offline PC without relying on the PC sending alerts."""
    now = now or datetime.now(timezone.utc)
    try:
        from tools.staging_backup_watch import age_seconds
        root = Path(root)
        report = read_json(root / "last-offsite.json")
        if report.get("project") != project or report.get("status") != "ok":
            raise ValueError("Invalid receipt")
        path = point(root, report["run_id"], project)
        receipt = read_json(root / "receipts" / (report["run_id"] + ".json"))
        if receipt.get("manifest_sha256") != digest(path / "manifest.json") or receipt.get("project") != project:
            raise ValueError("Invalid receipt")
        manifest = read_json(path / "manifest.json")
        if max(age_seconds(manifest["completed_at"], now), age_seconds(report["completed_at"], now)) > 36 * 3600:
            return "offsite_stale"
        return "ok"
    except Exception:
        return "offsite_missing_or_invalid"


def pull(config_path):
    config = read_json(Path(config_path))
    project = config["project"]
    root = owned_root(config["local_root"], project, create=True)
    with exclusive_lock(root / ".backup.lock"):
        report = dict(project=project, status="alert", code="transfer_failed", completed_at=stamp())
        temporary = root / ".transfer.tar"
        try:
            if is_link(temporary):
                raise ValueError("Links refused")
            ssh = [config["ssh"], "-i", config["key"], "-o", "IdentitiesOnly=yes",
                   "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
                   "-o", "UserKnownHostsFile=" + config["known_hosts"],
                   "-o", "ConnectTimeout=15", "-o", "ServerAliveInterval=15",
                   "-o", "ServerAliveCountMax=3", config["host"]]
            remote = "cd " + shlex.quote(config["remote_code"]) + " && sudo -n python3 -m tools.staging_offsite_backup "
            common = " --root " + shlex.quote(config["remote_root"]) + " --project " + shlex.quote(project)
            # Bound disk consumption while receiving, rather than trusting remote archive size.
            limit = config.get("max_bytes", 20 * 1024**3)
            if not isinstance(limit, int) or not 1024**2 <= limit <= 100 * 1024**3:
                raise ValueError("Invalid size limit")
            with temporary.open("wb") as output:
                os.chmod(temporary, 0o600)
                process = subprocess.Popen(ssh + [remote + "export" + common], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
                deadline = threading.Timer(900, process.kill)
                deadline.daemon = True
                deadline.start()
                try:
                    transferred = 0
                    while True:
                        data = process.stdout.read(1024**2)
                        if not data:
                            break
                        transferred += len(data)
                        if transferred > limit or shutil.disk_usage(root).free < len(data) + 1024**3:
                            raise ValueError("Transfer exceeds limit")
                        output.write(data)
                    if process.wait(timeout=30) != 0:
                        raise RuntimeError("Remote export failed")
                finally:
                    deadline.cancel()
                    if process.poll() is None:
                        process.kill()
                    process.wait()
                    process.stdout.close()
            received = receive(temporary, root, project, limit)
            report["code"] = "acknowledgement_failed"
            response = subprocess.run(ssh + [remote + "ack" + common + " --run-id " + received["run_id"]
                                      + " --digest " + received["manifest_sha256"] + " --keep 7"],
                                      capture_output=True, timeout=300)
            if response.returncode != 0:
                raise RuntimeError("Remote acknowledgement failed")
            ack = json.loads(response.stdout)
            if ack.get("status") != "ok" or ack.get("run_id") != received["run_id"]:
                raise ValueError("Invalid acknowledgement")
            report["code"] = "retention_failed"
            removed = prune(root, project, config.get("keep", 14), False)
            report = dict(project=project, status="ok", code="offsite_verified", run_id=received["run_id"],
                          completed_at=stamp(), local_removed=removed, remote_removed=ack["removed"])
        except Exception:
            # Never log command output, keys, addresses, or raw exceptions.
            pass
        finally:
            if temporary.exists() and not is_link(temporary):
                temporary.unlink()
            write_json(root / "last-run.json", report)
        return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["export", "ack", "pull"])
    parser.add_argument("--root", type=Path)
    parser.add_argument("--project")
    parser.add_argument("--run-id")
    parser.add_argument("--digest")
    parser.add_argument("--keep", type=int, default=7)
    parser.add_argument("--config", type=Path)
    args = parser.parse_args()
    try:
        if args.action == "export":
            export_latest(args.root, args.project, sys.stdout.buffer)
            return 0
        if args.action == "ack":
            result = acknowledge(args.root, args.project, args.run_id, args.digest, args.keep)
        else:
            result = pull(args.config)
        print(json.dumps(result))
        return 0 if result["status"] == "ok" else 1
    except BusyError:
        print('{"status":"skipped","code":"backup_busy"}', file=sys.stderr)
        return 75
    except Exception:
        print('{"status":"alert","code":"offsite_operation_failed"}', file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
