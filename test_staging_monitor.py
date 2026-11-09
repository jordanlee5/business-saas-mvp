import contextlib
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from tools import staging_monitor as monitor, staging_pg_backup as backup

PROJECT = "saas-pg-test-monitor"
NOW = datetime(2026, 11, 9, tzinfo=timezone.utc)


class StagingMonitorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "data").mkdir()
        (self.root / "data/session.key").write_text("synthetic-test-only")
        (self.root / "database.dump").write_text("synthetic-dump")
        self.manifest = dict(format=1, scope="dedicated-postgres-staging", source_project=PROJECT,
                             completed_at=NOW.isoformat(), files=backup.inventory(self.root))
        self.save()

    def save(self):
        (self.root / "manifest.json").write_text(json.dumps(self.manifest))

    def report(self, **kwargs):
        with patch.object(monitor, "service_check", return_value="ok"):
            return monitor.inspect_staging(PROJECT, [self.root], self.root,
                min_free_gib=0.00001, min_free_percent=0.00001, now=NOW, **kwargs)

    def test_healthy_and_no_backup_mutation(self):
        before = backup.inventory(self.root)
        self.assertEqual(self.report()["status"], "ok")
        self.assertEqual(before, backup.inventory(self.root))

    def test_stale_future_missing_time_wrong_project(self):
        for field, value, code in (
            ("completed_at", (NOW-timedelta(hours=37)).isoformat(), "backup_stale"),
            ("completed_at", (NOW+timedelta(hours=1)).isoformat(), "backup_time_invalid"),
            ("completed_at", None, "backup_time_missing"),
            ("completed_at", "2026-11-09", "backup_time_invalid"),
            ("source_project", "saas-pg-test-other", "backup_wrong_project")):
            with self.subTest(code=code):
                self.manifest[field] = value
                self.save()
                self.assertEqual(self.report()["checks"][-1]["code"], code)

    def test_damaged_missing_and_repaired_backup(self):
        dump = self.root / "database.dump"
        original = dump.read_bytes()
        for mode in ("changed", "missing", "extra"):
            with self.subTest(mode=mode):
                if mode == "changed": dump.write_text("changed")
                if mode == "missing": dump.unlink()
                if mode == "extra": (self.root / "extra").write_text("unexpected")
                self.assertEqual(self.report()["checks"][-1]["code"], "backup_invalid_or_missing")
                dump.write_bytes(original)
                if mode == "extra": (self.root / "extra").unlink()
                self.assertEqual(self.report()["status"], "ok")

    def test_non_object_backup_manifest_is_reported_not_crashed(self):
        for value in ([], None, "malformed"):
            (self.root / "manifest.json").write_text(json.dumps(value))
            self.assertEqual(self.report()["checks"][-1]["code"], "backup_invalid_or_missing")

    def test_low_bytes_or_percent_and_recovery(self):
        for total, free in ((100*1024**3, 4*1024**3), (1000*1024**3, 50*1024**3),
                            (100*1024**3, 20*1024**3)):
            with patch.object(monitor, "service_check", return_value="ok"), patch.object(
                    shutil, "disk_usage", return_value=shutil._ntuple_diskusage(total, total-free, free)):
                report = monitor.inspect_staging(PROJECT, [self.root], self.root, now=NOW)
                self.assertEqual(report["checks"][3]["code"], "ok" if free == 20*1024**3 else "disk_low")

    def test_docker_timeout_redacted_and_other_checks_continue(self):
        with patch.object(monitor, "docker", side_effect=subprocess.TimeoutExpired("private-value", 10)):
            result = monitor.inspect_staging(PROJECT, [self.root], self.root, now=NOW)
        self.assertEqual(len(result["checks"]), 5)
        self.assertEqual(result["checks"][0]["code"], "container_check_failed")
        self.assertNotIn("private-value", json.dumps(result))

    def test_missing_duplicate_stopped_unhealthy_and_wrong_labels(self):
        labels = {"com.docker.compose.project": PROJECT, "com.docker.compose.service": "app"}
        for ids, status, health, project, code in (
            ("", "running", "healthy", PROJECT, "container_missing_or_duplicate"),
            ("one two", "running", "healthy", PROJECT, "container_missing_or_duplicate"),
            ("one", "exited", "healthy", PROJECT, "container_not_running"),
            ("one", "running", "unhealthy", PROJECT, "container_not_healthy"),
            ("one", "running", "starting", PROJECT, "container_not_healthy"),
            ("one", "running", "healthy", "other", "container_identity_mismatch"),
            ("one", "running", "healthy", PROJECT, "ok")):
            labels["com.docker.compose.project"] = project
            data = "\n".join(json.dumps(v) for v in (labels, status, health))
            with self.subTest(code=code), patch.object(monitor, "docker", side_effect=[ids, data]):
                self.assertEqual(monitor.service_check(PROJECT, "app"), code)

    def test_invalid_configuration_never_calls_docker(self):
        for kwargs in ({"project": "production"}, {"min_free_gib": -1},
                       {"max_backup_hours": float("nan")}, {"disk_paths": []}):
            args = dict(project=PROJECT, disk_paths=[self.root], backup=self.root)
            args.update(kwargs)
            with patch.object(monitor, "docker") as call, self.assertRaises(ValueError):
                monitor.inspect_staging(**args)
            call.assert_not_called()

    def test_cli_exit_codes_no_raw_paths_or_exceptions(self):
        for status, expected in (("ok", 0), ("alert", 1)):
            with patch.object(monitor, "inspect_staging", return_value={"status": status}), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(monitor.main(["--project", PROJECT, "--disk-path", "private-path",
                                               "--backup", "private-path"]), expected)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(monitor.main(["--project", "private-invalid-name", "--disk-path", ".", "--backup", "."]), 2)
        self.assertNotIn("private-invalid-name", out.getvalue())


if __name__ == "__main__":
    unittest.main()
