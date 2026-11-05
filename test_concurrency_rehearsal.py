"""The load-test entry point must fail before touching an unapproved database."""
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest


class ConcurrentRehearsalSafetyTests(unittest.TestCase):
    def test_postgres_requires_disposable_target_and_explicit_opt_in(self):
        target = "postgresql+psycopg://load_runner:do-not-echo-secret@database:5432/mall_concurrency_test"
        with TemporaryDirectory() as directory:
            sentinel = Path(directory) / "must-not-exist.db"
            base = dict(os.environ, DATABASE_URL="sqlite:///" + sentinel.as_posix(),
                        MALL_LOAD_TEST_POSTGRES_URL=target, MALL_LOAD_TEST_ALLOW_RESET="1",
                        PYTHONIOENCODING="utf8")
            cases = [
                {"MALL_LOAD_TEST_ALLOW_RESET": ""},
                {"MALL_LOAD_TEST_POSTGRES_URL": target.replace("mall_concurrency_test", "business")},
                {"MALL_LOAD_TEST_POSTGRES_URL": target.replace("@database:", "@localhost:")},
                {"DATABASE_URL": target},
            ]
            for changes in cases:
                with self.subTest(changes=list(changes)):
                    result = subprocess.run(
                        [sys.executable, "-m", "tools.rehearse_concurrent_mall", "--postgres", "--levels", "1,2"],
                        cwd=Path(__file__).resolve().parent, env={**base, **changes},
                        capture_output=True, text=True, encoding="utf8", timeout=20,
                    )
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("RuntimeError", result.stderr)
                    self.assertNotIn("do-not-echo-secret", result.stdout + result.stderr)
                    self.assertFalse(sentinel.exists())
