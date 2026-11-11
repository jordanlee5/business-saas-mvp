import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools import staging_backup_crypto as crypto
from tools import staging_backup_job as job
from tools import staging_offsite_backup as offsite
from tools.staging_pg_backup import verify_backup, digest
from test_staging_backup_job import virtual_backup, PROJECT


@unittest.skipUnless(os.name == "posix", "Private key custody requires Linux/WSL")
class EncryptionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        crypto.keygen(cls.root / "keys")
        crypto.keygen(cls.root / "wrong")

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def setUp(self):
        self.work = Path(tempfile.mkdtemp(dir=self.root))
        self.source = self.work / "source"
        virtual_backup(self.source)
        self.encrypted = self.work / "encrypted"
        self.public = self.root / "keys/recovery-public.pem"
        self.private = self.root / "keys/recovery-private.pem"
        crypto.encrypt(self.source, self.encrypted, self.public)

    def test_roundtrip_and_source_unchanged(self):
        before = verify_backup(self.source)
        out = self.work / "out"
        crypto.decrypt(self.encrypted, out, self.private)
        self.assertEqual(verify_backup(out), before)
        self.assertEqual(verify_backup(self.source), before)
        self.assertNotIn(b"virtual-member", (self.encrypted / "payload.enc").read_bytes())

    def test_wrong_key_and_missing_key_no_publication(self):
        for key in (self.root / "wrong/recovery-private.pem", self.root / "missing"):
            out = self.work / "out"
            with self.assertRaises(Exception):
                crypto.decrypt(self.encrypted, out, key)
            self.assertFalse(out.exists())

    def test_modified_cipher_even_with_recomputed_hash_rejected(self):
        payload = self.encrypted / "payload.enc"
        data = bytearray(payload.read_bytes()); data[50] ^= 1; payload.write_bytes(data)
        with self.assertRaises(ValueError):
            verify_backup(self.encrypted)
        manifest = json.loads((self.encrypted / "manifest.json").read_text())
        manifest["files"]["payload.enc"]["sha256"] = digest(payload)
        (self.encrypted / "manifest.json").write_text(json.dumps(manifest))
        with self.assertRaises(Exception):
            crypto.decrypt(self.encrypted, self.work / "out", self.private)
        self.assertFalse((self.work / "out").exists())

    def test_metadata_authenticated(self):
        path = self.encrypted / "manifest.json"
        manifest = json.loads(path.read_text()); manifest["source_project"] = "saas-pg-test-other"
        path.write_text(json.dumps(manifest))
        with self.assertRaises(Exception):
            crypto.decrypt(self.encrypted, self.work / "out", self.private)
        self.assertFalse((self.work / "out").exists())

    def test_no_overwrite_and_permissions(self):
        before = (self.encrypted / "payload.enc").read_bytes()
        with self.assertRaises(FileExistsError):
            crypto.encrypt(self.source, self.encrypted, self.public)
        with self.assertRaises(ValueError):
            crypto.decrypt(self.encrypted, self.source, self.private)
        self.assertEqual(before, (self.encrypted / "payload.enc").read_bytes())
        self.assertEqual(self.private.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.private.parent.stat().st_mode & 0o777, 0o700)

    def test_key_permission_refused(self):
        self.private.chmod(0o644)
        try:
            with self.assertRaises(ValueError):
                crypto.decrypt(self.encrypted, self.work / "out", self.private)
        finally:
            self.private.chmod(0o600)

    def test_job_pull_receipt_rotation_and_failure_preserves_success(self):
        root = self.work / "scheduled"
        with patch.dict(os.environ, {"STAGING_BACKUP_PUBLIC_KEY": str(self.public)}), \
                patch.object(job, "StagingProject") as project, \
                patch.object(job, "service_check", return_value="ok"):
            project.return_value.backup.side_effect = virtual_backup
            reports = []
            local = self.work / "local"
            for _ in range(3):
                import time
                time.sleep(1.05)  # Distinct recovery timestamps for retention ordering.
                code, report = job.run_job(PROJECT, root, min_free_gib=.001)
                self.assertEqual(code, 0); reports.append(report)
                self.assertEqual(verify_backup(root / report["backup"])["format"], 2)
                archive = self.work / (report["run_id"] + ".tar")
                with archive.open("wb") as stream:
                    offsite.export_latest(root, PROJECT, stream)
                received = offsite.receive(archive, local, PROJECT)
                offsite.acknowledge(root, PROJECT, received["run_id"], received["manifest_sha256"])
            self.assertFalse(list(root.glob(".plaintext-*")))
            with job.exclusive_lock(root / ".backup.lock"):
                offsite.prune(root, PROJECT, 2, True)
            self.assertFalse((root / reports[0]["backup"]).exists())
            self.assertTrue((root / reports[-1]["backup"]).exists())
            previous = (root / "last-success.json").read_bytes()
            with patch.object(crypto, "encrypt", side_effect=RuntimeError("do-not-log")):
                code, failed = job.run_job(PROJECT, root, min_free_gib=.001)
            self.assertEqual((code, failed["code"]), (1, "backup_encryption_failed"))
            self.assertEqual(previous, (root / "last-success.json").read_bytes())
            self.assertNotIn("do-not-log", (root / "last-run.json").read_text())
            from tools.staging_backup_watch import inspect_backup
            with patch("tools.staging_backup_watch.unit_check", return_value="ok"), \
                    patch("tools.staging_backup_watch.service_check", return_value="ok"):
                checks = inspect_backup(PROJECT, root)["checks"]
            self.assertIn("backup_job_failed", [c["code"] for c in checks])

    def test_invalid_public_key_fails_before_backup(self):
        with patch.dict(os.environ, {"STAGING_BACKUP_PUBLIC_KEY": str(self.private)}), \
                patch.object(job, "StagingProject") as project:
            code, report = job.run_job(PROJECT, self.work / "scheduled", min_free_gib=.001)
            self.assertEqual(code, 1)
            project.assert_not_called()
