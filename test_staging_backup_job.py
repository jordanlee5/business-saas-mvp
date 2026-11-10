import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from tools import staging_backup_job as job
from tools.staging_pg_backup import inventory

PROJECT = "saas-pg-test-scheduler-unit"


def virtual_backup(destination):
    destination.mkdir()
    (destination / "data").mkdir()
    (destination / "data/session.key").write_text("synthetic-test-only")
    (destination / "database.dump").write_bytes(b"virtual-member-order-points")
    (destination / "manifest.json").write_text(json.dumps(dict(
        format=1, scope="dedicated-postgres-staging", source_project=PROJECT,
        completed_at=job.stamp(), files=inventory(destination))))


class ScheduledBackupTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "scheduled"
        self.env = patch.dict(os.environ, {})
        self.env.start()
        os.environ.pop("DATABASE_URL", None)
        self.addCleanup(self.env.stop)
        self.backend = patch.object(job, "StagingProject").start()
        self.addCleanup(patch.stopall)
        self.backend.return_value.backup.side_effect = virtual_backup
        self.health = patch.object(job, "service_check", return_value="ok").start()
        patch.object(job.shutil, "disk_usage", return_value=type("Disk", (), {"free": 10 * 1024**3})()).start()

    def run_job(self):
        return job.run_job(PROJECT, self.root, health_wait=0)

    def test_success_failure_and_recovery_preserve_previous_backup(self):
        code, report = self.run_job()
        self.assertEqual(code, 0)
        original = (self.root / "last-success.json").read_bytes()
        original_dump = self.root / report["backup"] / "database.dump"
        self.backend.return_value.backup.side_effect = RuntimeError("secret-do-not-log")
        code, failed = self.run_job()
        self.assertEqual((code, failed["code"]), (1, "backup_failed"))
        self.assertEqual((self.root / "last-success.json").read_bytes(), original)
        self.assertNotIn("secret-do-not-log", (self.root / "last-run.json").read_text())
        self.backend.return_value.backup.side_effect = virtual_backup
        code, recovered = self.run_job()
        self.assertEqual(code, 0)
        self.assertNotEqual(recovered["run_id"], report["run_id"])
        self.assertEqual(original_dump.read_bytes(), b"virtual-member-order-points")
        self.assertEqual(len(list((self.root / "runs").glob("*.json"))), 3)

    def test_corruption_and_wrong_source_never_publish_success(self):
        for mode in ("corrupt", "wrong_project"):
            with self.subTest(mode=mode):
                def bad(destination):
                    virtual_backup(destination)
                    if mode == "corrupt":
                        (destination / "database.dump").write_bytes(b"damaged")
                    else:
                        path = destination / "manifest.json"
                        manifest = json.loads(path.read_text())
                        manifest["source_project"] = "saas-pg-test-other"
                        path.write_text(json.dumps(manifest))
                self.backend.return_value.backup.side_effect = bad
                code, report = self.run_job()
                self.assertEqual((code, report["code"]), (1, "backup_invalid"))
                self.assertFalse((self.root / "last-success.json").exists())

    def test_restart_failure_is_failure_even_with_complete_manifest(self):
        def failed_restart(destination):
            virtual_backup(destination)
            raise RuntimeError("start failed")
        self.backend.return_value.backup.side_effect = failed_restart
        self.assertEqual(self.run_job()[0], 1)
        self.assertFalse((self.root / "last-success.json").exists())

    def test_unhealthy_after_restart_retains_previous_success(self):
        self.run_job()
        previous = (self.root / "last-success.json").read_bytes()
        self.health.side_effect = ["ok", "ok", "container_not_healthy"]
        code, report = self.run_job()
        self.assertEqual((code, report["code"]), (1, "service_recovery_failed"))
        self.assertEqual((self.root / "last-success.json").read_bytes(), previous)

    def test_stopped_app_is_not_started_by_job(self):
        self.health.return_value = "container_not_running"
        self.assertEqual(self.run_job()[1]["code"], "preflight_failed")
        self.backend.return_value.backup.assert_not_called()

    def test_low_disk_refuses_before_docker(self):
        with patch.object(job.shutil, "disk_usage", return_value=type("Disk", (), {"free": 0})()):
            self.assertEqual(self.run_job()[1]["code"], "disk_low")
        self.backend.assert_not_called()

    def test_invalid_project_environment_and_thresholds(self):
        for kwargs in ({"project_name": "production"}, {"min_free_gib": float("nan")},
                       {"health_wait": -1}):
            options = dict(project_name=PROJECT, root=self.root)
            options.update(kwargs)
            with self.assertRaises(ValueError):
                job.run_job(**options)
        with patch.dict(os.environ, {"DATABASE_URL": "not-a-test-database"}):
            with self.assertRaises(ValueError):
                self.run_job()
        self.backend.assert_not_called()

    def test_repository_destination_refused(self):
        with self.assertRaises(ValueError):
            job.run_job(PROJECT, job.ROOT / "private-backup")
        self.backend.assert_not_called()

    def test_root_cannot_be_reused_for_other_project(self):
        self.run_job()
        self.backend.reset_mock()
        with self.assertRaises(ValueError):
            job.run_job("saas-pg-test-different", self.root)
        self.backend.assert_not_called()

    def test_overlap_in_separate_process_and_crash_release(self):
        self.run_job()
        previous = (self.root / "last-run.json").read_bytes()
        program = "from pathlib import Path; from tools.staging_backup_job import exclusive_lock; import sys,time\nwith exclusive_lock(Path(sys.argv[1])):\n print('locked',flush=True)\n time.sleep(30)"
        child = subprocess.Popen([sys.executable, "-c", program, str(self.root / ".backup.lock")],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.assertEqual(child.stdout.readline().strip(), "locked")
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = job.main(["--project", PROJECT, "--root", str(self.root)])
            self.assertEqual(code, 75)
            self.assertEqual(json.loads(output.getvalue())["code"], "backup_already_running")
            self.assertEqual((self.root / "last-run.json").read_bytes(), previous)
        finally:
            child.kill()
            child.communicate(timeout=10)
        self.assertEqual(self.run_job()[0], 0)

    @unittest.skipIf(os.name == "nt", "Symlink creation requires privileges on Windows")
    def test_symlink_root_and_lock_refused(self):
        target = Path(self.temporary.name) / "target"
        target.mkdir()
        self.root.symlink_to(target, target_is_directory=True)
        with self.assertRaises(ValueError):
            self.run_job()
        self.root.unlink()
        self.root.mkdir()
        (self.root / ".backup.lock").symlink_to(target / "lock")
        with self.assertRaises(ValueError):
            self.run_job()

    def test_status_write_failure_nonzero_and_no_exception_leak(self):
        with patch.object(job, "write_json", side_effect=OSError("private-value")):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = job.main(["--project", PROJECT, "--root", str(self.root)])
            self.assertEqual(code, 2)
            self.assertNotIn("private-value", output.getvalue())


if __name__ == "__main__":
    unittest.main()
