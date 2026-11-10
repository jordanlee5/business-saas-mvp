"""Opt-in real Docker rehearsal: new synthetic projects only, no published ports.

Uses a locally available application image. All new projects are stopped afterwards;
their volumes, private recovery points and reports remain for inspection.
"""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import tempfile
import uuid
from unittest.mock import patch

from tools.staging_pg_backup import ROOT, StagingProject, prepare_secrets
from tools.staging_backup_job import run_job, exclusive_lock, BusyError
from tools.staging_backup_watch import inspect_backup, record_and_notify


SEED = '''
from tools.staging_postgres import configure
configure()
import sys, secrets
sys.path.insert(0, '/tmp')
import test_order_service as fixture
from app.database import SessionLocal
from app.auth import get_password_hash
from app.time_utils import utc8_now
fixture.NOW = utc8_now()
with SessionLocal() as db:
    example = fixture.OrderServiceTests()
    example.db = db
    example.seed_order_fixture()
    example.operator.admin_level = 'super_admin'
    example.operator.password_hash = get_password_hash(secrets.token_urlsafe(32))
    db.commit()
from pathlib import Path
Path('/data/synthetic-backup-marker.txt').write_text('virtual backup recovery marker')
print('synthetic fixtures created')
'''

CHECK = '''
from tools.staging_postgres import configure
configure()
from app.database import SessionLocal
from app.models import Member, ProductSku, PointsAccount
from pathlib import Path
from sqlalchemy import select
import json, urllib.request
with SessionLocal() as db:
    members = db.scalars(select(Member)).all()
    skus = db.scalars(select(ProductSku)).all()
    accounts = db.scalars(select(PointsAccount)).all()
    assert len(members) == 1 and len(skus) == 2 and len(accounts) == 1
    assert str(accounts[0].available_points) == '170.00'
assert Path('/data/synthetic-backup-marker.txt').read_text() == 'virtual backup recovery marker'
assert urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=5).status == 200
assert urllib.request.urlopen('http://127.0.0.1:8000/login', timeout=5).status == 200
print(json.dumps({'members':1,'skus':2,'available_points':'170.00','file_marker':'ok','health':200,'login':200}))
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    args = parser.parse_args()
    if os.environ.get("DATABASE_URL"):
        raise SystemExit("External database configuration refused")
    root = Path(tempfile.mkdtemp(prefix="saas-backup-rehearsal-"))
    prepare_secrets(root / "secrets")
    os.environ["STAGING_SECRET_DIR"] = str(root / "secrets")
    os.environ["STAGING_APP_IMAGE"] = args.image
    suffix = uuid.uuid4().hex[:12]
    source = StagingProject("saas-pg-test-job-" + suffix)
    restored = StagingProject("saas-pg-test-job-restored-" + suffix)
    checks = []
    notifications = []

    def watch_check():
        # Rehearsal projects have no installed timers; test real backup/container checks.
        with patch("tools.staging_backup_watch.unit_check", return_value="ok"):
            report = inspect_backup(source.name, scheduled)
        def capture(_secret, message):
            notifications.append(message)
            return "delivered"
        return record_and_notify(report, root / "watch", sender=capture)
    try:
        # Refuse any collision before provisioning or initializing a database.
        for project in (source, restored):
            assert not project.run("ps", "-a", "-q").stdout.strip()
            assert not subprocess.check_output(["docker", "volume", "ls", "-q", "--filter",
                "label=com.docker.compose.project=" + project.name]).strip()
        source.run("up", "-d", "--wait", "--wait-timeout", "120", "database", "ocr")
        source.run("run", "--rm", "--no-deps", "app", "python", "-m", "tools.staging_postgres", "initialize")
        source.run("create", "--no-build", "app")
        app, _ = source.container("app")
        # Test fixtures are copied into the disposable container, never the image.
        subprocess.run(["docker", "cp", str(ROOT / "test_order_service.py"), app + ":/tmp/test_order_service.py"],
                       check=True, capture_output=True)
        # One-off seed needs the fixture too; stdin contains test source, no credentials.
        source.run("run", "--rm", "--no-deps", "-v", str(ROOT / "test_order_service.py") + ":/tmp/test_order_service.py:ro",
                   "app", "python", "-", input=SEED.encode())
        source.run("up", "-d", "--no-build", "--wait", "--wait-timeout", "180", "app")
        before = json.loads(source.run("exec", "-T", "app", "python", "-", input=CHECK.encode()).stdout)
        checks.append("synthetic_business_and_http_before_backup")
        scheduled = root / "scheduled"
        code, first = run_job(source.name, scheduled)
        assert code == 0
        checks.append("real_pg_dump_files_verified_and_app_restarted")
        after = json.loads(source.run("exec", "-T", "app", "python", "-", input=CHECK.encode()).stdout)
        assert after == before
        checks.append("source_business_and_http_unchanged_after_backup")
        assert watch_check()["status"] == "ok" and not notifications
        success = (scheduled / "last-success.json").read_bytes()
        source.run("stop", "database")
        code, failure = run_job(source.name, scheduled)
        assert code == 1 and failure["code"] == "preflight_failed"
        assert (scheduled / "last-success.json").read_bytes() == success
        checks.append("database_failure_recorded_previous_backup_preserved")
        alert = watch_check()
        assert alert["status"] == "alert" and alert["notification"] == "delivered"
        assert watch_check()["notification"] == "not_needed" and len(notifications) == 1
        checks.append("real_failure_watch_event_and_simulated_notification_deduplicated")
        source.run("up", "-d", "--wait", "--wait-timeout", "120", "database")
        code, recovered = run_job(source.name, scheduled)
        assert code == 0 and recovered["run_id"] != first["run_id"]
        checks.append("backup_succeeds_after_database_recovery")
        recovery = watch_check()
        assert recovery["status"] == "ok" and recovery["notification"] == "delivered"
        assert watch_check()["notification"] == "not_needed" and len(notifications) == 2
        checks.append("real_recovery_watch_event_and_simulated_notification_deduplicated")
        with exclusive_lock(scheduled / ".backup.lock"):
            try:
                run_job(source.name, scheduled)
            except BusyError:
                checks.append("overlapping_job_rejected")
            else:
                raise AssertionError("Overlapping job was accepted")
        restored.restore(scheduled / recovered["backup"])
        actual = json.loads(restored.run("exec", "-T", "app", "python", "-", input=CHECK.encode()).stdout)
        assert actual == before
        checks.append("new_project_restores_virtual_members_skus_points_files_and_http")
        report = dict(format=1, completed_at=datetime.now(timezone.utc).isoformat(),
                      status="passed", checks=checks, business=actual,
                      scope="new isolated Docker projects; synthetic data only; notification transport simulated; scheduler separately verified")
        (root / "evidence.json").write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps(report, indent=2))
        print("Private rehearsal evidence: " + str(root / "evidence.json"))
    finally:
        for project in (source, restored):
            project.run("stop")


if __name__ == "__main__":
    main()
