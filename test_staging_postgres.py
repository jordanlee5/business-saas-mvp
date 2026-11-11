import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from tools import staging_pg_backup


class PostgresStagingSafetyTests(unittest.TestCase):
    def test_external_database_url_rejected_before_connecting(self):
        result = subprocess.run([sys.executable, "-m", "tools.staging_postgres", "initialize"],
            env={**os.environ, "DATABASE_URL": "postgresql://private:do-not-echo@production/db"},
            capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 1)
        self.assertNotIn("do-not-echo", result.stdout + result.stderr)

    def backup(self, root):
        (root/"data").mkdir()
        (root/"data/session.key").write_text("synthetic-key")
        (root/"database.dump").write_bytes(b"synthetic-dump")
        manifest = dict(format=1,scope="dedicated-postgres-staging",files=staging_pg_backup.inventory(root))
        (root/"manifest.json").write_text(json.dumps(manifest))
        return manifest

    def test_tamper_missing_and_extra_files_rejected_before_docker(self):
        for change in ("tamper", "missing", "extra"):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as directory:
                root=Path(directory)
                self.backup(root)
                staging_pg_backup.verify_backup(root)
                if change=="tamper":(root/"database.dump").write_bytes(b"changed")
                elif change=="missing":(root/"data/session.key").unlink()
                else:(root/"unexpected.txt").write_text("extra")
                with patch("subprocess.run") as docker, self.assertRaises(ValueError):
                    staging_pg_backup.verify_backup(root)
                docker.assert_not_called()

    def test_restore_rejects_existing_project_without_changing_it(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            self.backup(root)
            project=object.__new__(staging_pg_backup.StagingProject)
            project.name="saas-pg-test-existing"
            with patch.object(project, "run", return_value=subprocess.CompletedProcess([],0,b"existing\n")) as run:
                with self.assertRaises(ValueError):project.restore(root)
            run.assert_called_once_with("ps","-a","-q")

    def test_project_name_cannot_target_regular_deployment(self):
        with patch("subprocess.run") as docker, self.assertRaises(ValueError):
            staging_pg_backup.StagingProject("production")
        docker.assert_not_called()

    def test_secrets_are_private_and_never_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)/"private"
            staging_pg_backup.prepare_secrets(root)
            initial=(root/"app-password").read_bytes()
            self.assertEqual(len(initial),64)
            self.assertNotEqual(initial,(root/"owner-password").read_bytes())
            with self.assertRaises(FileExistsError):staging_pg_backup.prepare_secrets(root)
            self.assertEqual(initial,(root/"app-password").read_bytes())

    def test_restore_image_mismatch_fails_before_start_or_database_write(self):
        for mismatch in ("app", "ocr"):
            with self.subTest(mismatch=mismatch), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                manifest = self.backup(root)
                expected = "sha256:" + "a" * 64
                wrong = "sha256:" + "b" * 64
                manifest["app_image_id"] = expected
                (root / "manifest.json").write_text(json.dumps(manifest))
                project = object.__new__(staging_pg_backup.StagingProject)
                project.name = "saas-pg-test-new"
                project.config = {"volumes": {"data": {"name": "saas-pg-test-new_data"}},
                                  "services": {"app": {"image": expected}, "ocr": {"image": wrong}}}
                outputs = [b"", (wrong if mismatch == "app" else expected).encode()]
                if mismatch == "ocr":
                    outputs.append(wrong.encode())
                with patch.object(project, "run", return_value=subprocess.CompletedProcess([], 0, b"")) as run, \
                        patch.object(project, "sql") as sql, \
                        patch("subprocess.check_output", side_effect=outputs), \
                        patch("subprocess.run", return_value=subprocess.CompletedProcess([], 1)), \
                        self.assertRaises(ValueError):
                    project.restore(root)
                run.assert_called_once_with("ps", "-a", "-q")
                sql.assert_not_called()
