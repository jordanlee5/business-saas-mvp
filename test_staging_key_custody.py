import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from tools import staging_key_custody as custody
from tools import staging_backup_crypto as crypto
from tools.staging_pg_backup import ROOT, verify_backup
from test_staging_backup_job import virtual_backup


@unittest.skipUnless(os.name == "posix", "Private key permissions require Linux/WSL")
class CustodyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        crypto.keygen(self.root / "keys")
        self.private = self.root / "keys/recovery-private.pem"
        self.public = self.root / "keys/recovery-public.pem"
        self.password = os.urandom(32)
        self.package = self.root / "usb/copy"

    def export(self):
        return custody.export(self.private, self.package, self.password)

    def test_export_recover_and_decrypt_synthetic_backup(self):
        before = self.private.read_bytes()
        identity = self.export()
        with self.assertRaises(Exception):
            custody.s.load_pem_private_key((self.package / "private-key.encrypted.pem").read_bytes(), None)
        restored = self.root / "recovered"
        self.assertEqual(custody.recover(self.package, restored, self.password, self.public), identity)
        virtual_backup(self.root / "source")
        crypto.encrypt(self.root / "source", self.root / "cipher", self.public)
        crypto.decrypt(self.root / "cipher", self.root / "plain", restored / "recovery-private.pem")
        self.assertEqual(verify_backup(self.root / "plain"), verify_backup(self.root / "source"))
        self.assertEqual(self.private.read_bytes(), before)

    def test_export_into_nonempty_drive_preserves_existing_documents(self):
        drive = self.package.parent
        drive.mkdir()
        existing = drive / "existing-document.txt"
        existing.write_bytes(b"existing-user-document-must-remain")
        self.export()
        self.assertEqual(existing.read_bytes(), b"existing-user-document-must-remain")
        self.assertEqual(set(p.name for p in drive.iterdir()), {"existing-document.txt", "copy"})

    def test_wrong_password_and_wrong_identity(self):
        self.export()
        crypto.keygen(self.root / "other")
        for password, public in ((b"wrong", self.public), (self.password, self.root / "other/recovery-public.pem")):
            with self.assertRaises(Exception):
                custody.recover(self.package, self.root / "out", password, public)
            self.assertFalse((self.root / "out").exists())

    def test_corruption_rejected(self):
        self.export()
        path = self.package / "private-key.encrypted.pem"
        path.write_bytes(path.read_bytes()[:64])
        with self.assertRaises(Exception):
            custody.verify(self.package, self.password, self.public)

    def test_existing_copy_source_overlap_weak_password_and_links_refused(self):
        self.export()
        with self.assertRaises(FileExistsError):
            self.export()
        for path,password in ((self.private.parent / "copy", self.password), (self.root / "weak", b"short")):
            with self.assertRaises(ValueError):
                custody.export(self.private, path, password)
            self.assertFalse(path.exists())
        (self.root / "link").symlink_to(self.package, target_is_directory=True)
        with self.assertRaises(ValueError):
            custody.verify(self.root / "link", self.password, self.public)

    def test_noninteractive_cli_refuses_without_reading_key(self):
        result = subprocess.run([sys.executable, "-m", "tools.staging_key_custody", "export", str(self.private),
                                 "--destination", str(self.package)], input=b"password", capture_output=True, cwd=ROOT)
        self.assertEqual(result.returncode, 1)
        self.assertFalse(self.package.exists())

    def test_installer_bundle_can_read_encrypted_recovery_point(self):
        installer = (ROOT / "deploy/staging-postgres/install-offsite-pull.ps1").read_text()
        names = set(re.findall(r"'([a-z_]+[.]py)'", installer))
        runner = self.root / "runner"
        (runner / "tools").mkdir(parents=True)
        for name in names:
            shutil.copyfile(ROOT / "tools" / name, runner / "tools" / name)
        virtual_backup(self.root / "source")
        crypto.encrypt(self.root / "source", self.root / "cipher", self.public)
        result = subprocess.run([sys.executable, "-c", "from pathlib import Path; from tools.staging_pg_backup import verify_backup; import sys; assert verify_backup(Path(sys.argv[1]))['format']==2", str(self.root / "cipher")], cwd=runner, capture_output=True, env={k:v for k,v in os.environ.items() if k != "PYTHONPATH"})
        self.assertEqual(result.returncode, 0, result.stderr.decode())
