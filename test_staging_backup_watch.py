from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from tools import staging_backup_watch as watch
from tools.staging_pg_backup import inventory

PROJECT = "saas-pg-test-watch-unit"
NOW = datetime(2026, 11, 10, 8, tzinfo=timezone.utc)


class WatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.backup_root = self.root / "scheduled"
        self.backup_root.mkdir()
        self.state_root = self.root / "watch"
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, {}).start()
        os.environ.pop("DATABASE_URL", None)
        patch.object(watch, "service_check", return_value="ok").start()
        patch.object(watch, "unit_check", return_value="ok").start()
        patch.object(watch.shutil, "disk_usage", return_value=Mock(total=100*1024**3, free=50*1024**3)).start()

    def save(self, name, **values):
        data = dict(project=PROJECT, started_at=NOW.isoformat(), completed_at=NOW.isoformat(), status="ok")
        data.update(values)
        (self.backup_root / name).write_text(json.dumps(data))

    def point(self, when=NOW):
        relative = "backups/20261110T080000Z-" + "a" * 32
        path = self.backup_root / relative
        (path / "data").mkdir(parents=True)
        (path / "database.dump").write_bytes(b"synthetic recovery point")
        (path / "data/session.key").write_bytes(b"synthetic only")
        manifest = dict(format=1, scope="dedicated-postgres-staging", source_project=PROJECT,
                        completed_at=when.isoformat(), files=inventory(path))
        (path / "manifest.json").write_text(json.dumps(manifest))
        self.save("last-success.json", backup=relative, completed_at=when.isoformat())
        self.save("last-run.json")
        return path

    def inspect(self, **kwargs):
        return watch.inspect_backup(PROJECT, self.backup_root, now=NOW, **kwargs)

    def codes(self, report):
        return {c["code"] for c in report["checks"] if c["status"] == "alert"}

    def test_first_deadline_is_bounded_and_not_renewed(self):
        self.assertIn("backup_missing", self.codes(self.inspect()))
        self.assertEqual(self.inspect(first_deadline=(NOW+timedelta(hours=20)).isoformat())["status"], "ok")
        self.assertIn("backup_missing", self.codes(self.inspect(first_deadline=NOW.isoformat())))
        self.assertIn("backup_invalid", self.codes(self.inspect(first_deadline=(NOW+timedelta(days=10)).isoformat())))

    def test_verified_point_corruption_wrong_project_and_traversal(self):
        path = self.point()
        self.assertEqual(self.inspect()["status"], "ok")
        (path / "database.dump").write_bytes(b"corruption")
        self.assertIn("backup_invalid", self.codes(self.inspect()))
        self.save("last-success.json", project="saas-pg-test-other", backup="../outside")
        self.assertIn("backup_invalid", self.codes(self.inspect()))
        self.save("last-success.json", backup="../outside")
        self.assertIn("backup_invalid", self.codes(self.inspect()))

    def test_stale_point_and_failed_job_are_alerts(self):
        self.point(NOW-timedelta(hours=37))
        self.save("last-run.json", status="alert")
        self.assertTrue({"backup_stale", "backup_job_failed"} <= self.codes(self.inspect()))

    def test_normal_pause_then_hung_job_and_other_service_failure(self):
        self.point()
        self.save("last-run.json", status="running")
        with patch.object(watch, "service_check", side_effect=lambda p,s: "container_not_running" if s=="app" else "ok"):
            self.assertEqual(self.inspect()["status"], "ok")
            self.save("last-run.json", status="running", started_at=(NOW-timedelta(minutes=16)).isoformat())
            self.assertTrue({"backup_overdue_running", "container_not_running"} <= self.codes(self.inspect()))
        with patch.object(watch, "service_check", return_value="container_not_healthy"):
            self.assertIn("container_not_healthy", self.codes(self.inspect()))

    def test_disk_low_scheduler_failure_and_external_database(self):
        self.point()
        with patch.object(watch.shutil, "disk_usage", return_value=Mock(total=100, free=0)), patch.object(watch,"unit_check",return_value="backup_unit_failed"):
            self.assertTrue({"disk_low", "backup_unit_failed"} <= self.codes(self.inspect()))
        with patch.dict(os.environ, {"DATABASE_URL": "external-refused"}):
            with self.assertRaises(ValueError):
                self.inspect()

    def report(self, failed=True):
        return dict(project=PROJECT, checked_at=NOW.isoformat(), status="alert" if failed else "ok",
                    checks=[dict(check="backup", code="backup_job_failed" if failed else "ok", status="alert" if failed else "ok")])

    def test_delivery_dedup_reminder_and_recovery(self):
        sender = Mock(return_value="delivered")
        def run(failed=True, hours=0):
            return watch.record_and_notify(self.report(failed), self.state_root, now=NOW+timedelta(hours=hours), sender=sender)
        self.assertEqual(run(False)["notification"], "not_needed")
        self.assertEqual(run()["notification"], "delivered")
        self.assertEqual(run()["notification"], "not_needed")
        self.assertEqual(run(hours=6)["notification"], "delivered")
        self.assertEqual(run(False,6)["notification"], "delivered")
        self.assertEqual(run(False,6)["notification"], "not_needed")
        self.assertEqual(sender.call_count,3)

    def test_failed_delivery_retried_and_missing_config_recorded(self):
        sender = Mock(side_effect=["delivery_failed", "delivered"])
        for expected in ("delivery_failed", "delivered"):
            self.assertEqual(watch.record_and_notify(self.report(),self.state_root,now=NOW,sender=sender)["notification"], expected)
        latest = json.loads((self.state_root/"latest.json").read_text())
        self.assertFalse(latest["notification_configured"])
        self.assertEqual(watch.send_wecom(None,"test"),"not_configured")

    def test_other_project_cannot_reuse_notification_state(self):
        watch.record_and_notify(self.report(),self.state_root,now=NOW,sender=lambda *_:"not_configured")
        report = self.report()
        report["project"] = "saas-pg-test-other"
        with self.assertRaises(ValueError):
            watch.record_and_notify(report,self.state_root,now=NOW)

    def test_transport_uses_tls_fixed_host_no_proxy_or_redirect_and_checks_provider(self):
        secret = self.root / "url.txt"
        secret.write_text("https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=" + "synthetic"*4)
        secret.chmod(0o600)
        response=Mock(status=200)
        response.__enter__=Mock(return_value=response)
        response.__exit__=Mock(return_value=False)
        with patch.object(watch.urllib.request,"build_opener") as build:
            build.return_value.open.return_value=response
            for data,expected in ((b'{"errcode":0}',"delivered"),(b'{"errcode":1}',"provider_rejected"),(b'{}',"provider_rejected"),(b'bad',"delivery_failed")):
                response.read.return_value=data
                self.assertEqual(watch.send_wecom(secret,"test"),expected)
            args=build.call_args.args
            self.assertEqual(args[0].proxies,{})
            self.assertIsInstance(args[1],watch.NoRedirect)
            self.assertEqual(build.return_value.open.call_args.kwargs["timeout"],10)
            secret.write_text("http://example.invalid/?key=" + "synthetic"*4)
            self.assertEqual(watch.send_wecom(secret,"test"),"configuration_invalid")


class SchedulerTests(unittest.TestCase):
    def test_failed_service_and_disabled_timer(self):
        def result(text):
            return subprocess.CompletedProcess([],0,text,"")
        with patch.object(watch.subprocess,"run",side_effect=[result("LoadState=loaded\nActiveState=failed\nResult=exit-code")]):
            self.assertEqual(watch.unit_check(PROJECT),"backup_unit_failed")
        with patch.object(watch.subprocess,"run",side_effect=[result("LoadState=loaded\nActiveState=inactive\nResult=success"),result("LoadState=loaded\nActiveState=inactive\nUnitFileState=disabled")]):
            self.assertEqual(watch.unit_check(PROJECT),"backup_timer_inactive")


if __name__ == "__main__":
    unittest.main()
