from contextlib import redirect_stdout
from io import StringIO
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from sqlalchemy.orm import sessionmaker

from app import init_admin
from app.auth import get_password_hash, verify_password
from app.database import create_database_engine
from app.models import User
from tools import staging_server


class StagingPreparationTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.url = "sqlite:///" + (self.root / "staging.db").as_posix()
        self.enterContext(patch.object(staging_server, "STORE", self.root))
        self.enterContext(patch.object(staging_server, "DATABASE_URL", self.url))
        self.enterContext(patch.dict(os.environ, {"DATABASE_URL": self.url}))
        self.output = StringIO()
        self.enterContext(redirect_stdout(self.output))
        staging_server.initialize()
        self.engine = create_database_engine(self.url)
        self.addCleanup(self.engine.dispose)
        self.sessions = sessionmaker(bind=self.engine)
        self.enterContext(patch.object(init_admin, "engine", self.engine))
        self.enterContext(patch.object(init_admin, "SessionLocal", self.sessions))

    def create_admin(self, password="Virtual-Only-Password-2026"):
        with patch.object(init_admin.getpass, "getpass", side_effect=[password, password]):
            init_admin.init_admin()

    def test_new_store_requires_private_admin_and_preserves_state_on_retry(self):
        with self.assertRaises(ValueError):
            staging_server.preflight()
        self.create_admin()
        key = (self.root / "session.key").read_bytes()
        before = (self.root / "staging.db").read_bytes()
        staging_server.preflight()
        self.assertEqual(os.environ["MINIPROGRAM_SESSION_SIGNING_KEY"], key.decode())
        with self.assertRaises(FileExistsError):
            staging_server.initialize()
        self.assertEqual((self.root / "session.key").read_bytes(), key)
        self.assertEqual((self.root / "staging.db").read_bytes(), before)
        with self.sessions() as db:
            user = db.query(User).one()
            self.assertTrue(verify_password("Virtual-Only-Password-2026", user.password_hash))
            self.assertTrue(user.must_change_password)
        self.assertNotIn("Virtual-Only-Password", self.output.getvalue())

    def test_existing_admin_is_not_reset_or_prompted(self):
        self.create_admin()
        before = (self.root / "staging.db").read_bytes()
        with patch.object(init_admin.getpass, "getpass", side_effect=AssertionError("Unexpected prompt")):
            init_admin.init_admin()
        self.assertEqual((self.root / "staging.db").read_bytes(), before)

    def test_bad_password_or_non_private_terminal_creates_no_account(self):
        cases = [["short", "short"], ["Long-Enough-Password", "Mismatch-Password"]]
        for values in cases:
            with patch.object(init_admin.getpass, "getpass", side_effect=values), self.assertRaises(ValueError):
                init_admin.init_admin()
        with patch.object(init_admin.getpass, "getpass", side_effect=init_admin.getpass.GetPassWarning()), self.assertRaises(init_admin.getpass.GetPassWarning):
            init_admin.init_admin()
        with self.sessions() as db:
            self.assertEqual(db.query(User).count(), 0)

    def test_default_password_corrupt_key_and_wrong_database_fail_closed(self):
        self.create_admin()
        with self.sessions.begin() as db:
            db.query(User).one().password_hash = get_password_hash("admin123")
        with self.assertRaises(ValueError):
            staging_server.preflight()

    def test_invalid_key_and_database_are_rejected_without_creating_files(self):
        self.create_admin()
        (self.root / "session.key").write_text("invalid", encoding="ascii")
        with self.assertRaises(ValueError):
            staging_server.preflight()
        with patch.dict(os.environ, {"DATABASE_URL": "sqlite:///unrelated.db"}):
            with self.assertRaises(ValueError):
                staging_server.initialize()
            with self.assertRaises(ValueError):
                staging_server.preflight()
        self.assertFalse((self.root / "unrelated.db").exists())

    def test_serve_uses_single_worker_with_proxy_support(self):
        self.create_admin()
        with patch("uvicorn.run") as run:
            self.assertEqual(staging_server.main(["serve"]), 0)
        self.assertEqual(run.call_args.kwargs["workers"], 1)
        self.assertTrue(run.call_args.kwargs["proxy_headers"])
        self.assertFalse(run.call_args.kwargs["access_log"])
