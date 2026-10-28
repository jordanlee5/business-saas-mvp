import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app import wechat_local as local


class WechatLocalTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name) / "private"
        local.initialize(self.directory)

    def write(self, value):
        (self.directory / "server-config.json").write_text(json.dumps(value), encoding="utf-8")

    def test_initialize_never_replaces_existing_key(self):
        before = local.load_config(self.directory)
        with self.assertRaises(FileExistsError):
            local.initialize(self.directory)
        self.assertEqual(local.load_config(self.directory), before)
        self.assertEqual(before[local.SECRET_KEY], "")
        self.assertGreaterEqual(len(before[local.SIGNING_KEY]), 32)

    def test_repository_directory_rejected_before_creation(self):
        with self.assertRaises(local.ConfigurationError):
            local.initialize(local.PROJECT_ROOT / "private-wechat-test")
        self.assertFalse((local.PROJECT_ROOT / "private-wechat-test").exists())

    def test_check_only_reports_status(self):
        config = local.load_config(self.directory)
        config[local.SECRET_KEY] = "PRIVATE-TEST-SECRET"
        self.write(config)
        report = local.check(self.directory)
        output = json.dumps(report)
        self.assertTrue(report["app_secret_present"])
        self.assertTrue(report["project_app_id_matches"])
        self.assertTrue(report["privacy_approval_gate_empty"])
        self.assertTrue(report["url_check_enabled"])
        self.assertTrue(report["https_api_origin_empty"])
        self.assertFalse(report["wechat_credentials_verified"])
        self.assertNotIn(config[local.SECRET_KEY], output)
        self.assertNotIn(config[local.SIGNING_KEY], output)

    def test_malformed_config_errors_never_print_contents(self):
        private = "DO-NOT-PRINT-PRIVATE-TEST-VALUE"
        path = self.directory / "server-config.json"
        path.write_text('{"bad":"' + private, encoding="utf-8")
        output = io.StringIO()
        with contextlib.redirect_stderr(output):
            result = local.main(["--directory", str(self.directory), "check"])
        self.assertEqual(result, 1)
        self.assertNotIn(private, output.getvalue())

    def test_wrong_app_id_unknown_fields_and_weak_key_rejected(self):
        original = local.load_config(self.directory)
        for updates in ({local.APP_ID_KEY: "wx0000000000000000"},
                        {local.SIGNING_KEY: "short"}, {"DATABASE_URL": "arbitrary"},
                        {local.SECRET_KEY: "contains\nnewline"}, {local.SECRET_KEY: 123}):
            with self.subTest(updates=list(updates)):
                self.write({**original, **updates})
                with self.assertRaises(local.ConfigurationError):
                    local.load_config(self.directory)

    def test_secret_entry_requires_hidden_interactive_input(self):
        before = local.load_config(self.directory)
        with patch.object(local.sys.stdin, "isatty", return_value=False), \
                patch.object(local.getpass, "getpass") as prompt:
            with self.assertRaises(local.ConfigurationError):
                local.configure_secret(self.directory)
        prompt.assert_not_called()
        self.assertEqual(local.load_config(self.directory), before)

    def test_mismatched_secret_preserves_file(self):
        before = local.load_config(self.directory)
        with patch.object(local.sys.stdin, "isatty", return_value=True), \
                patch.object(local.getpass, "getpass", side_effect=["one", "two"]):
            with self.assertRaises(local.ConfigurationError):
                local.configure_secret(self.directory)
        self.assertEqual(local.load_config(self.directory), before)

    def test_secret_update_preserves_signing_key(self):
        before = local.load_config(self.directory)
        with patch.object(local.sys.stdin, "isatty", return_value=True), \
                patch.object(local.getpass, "getpass", return_value="TEST-SECRET-ONLY"):
            local.configure_secret(self.directory)
        after = local.load_config(self.directory)
        self.assertEqual(after[local.SIGNING_KEY], before[local.SIGNING_KEY])
        self.assertEqual(after[local.SECRET_KEY], "TEST-SECRET-ONLY")
        self.assertEqual([p.name for p in self.directory.iterdir()], ["server-config.json"])

    def test_missing_secret_never_starts_server(self):
        with patch.object(local.subprocess, "run") as run:
            with self.assertRaises(local.ConfigurationError):
                local.serve(self.directory)
        run.assert_not_called()

    def test_launch_uses_child_environment_and_existing_local_database(self):
        root = Path(self.temporary.name) / "app"
        root.mkdir()
        (root / "saas_mvp.db").touch()
        config = local.load_config(self.directory)
        config[local.SECRET_KEY] = "TEST-SECRET-ONLY"
        self.write(config)
        inherited = {"DATABASE_URL": "do-not-use", "POSTGRES_TEST_ALLOW_RESET": "1"}
        with patch.object(local, "PROJECT_ROOT", root), \
                patch.object(local, "check", return_value={"project_app_id_matches": True}), \
                patch.dict(os.environ, inherited), patch.object(local.subprocess, "run") as run:
            run.return_value.returncode = 0
            self.assertEqual(local.serve(self.directory), 0)
            self.assertEqual(os.environ["DATABASE_URL"], "do-not-use")
        args, kwargs = run.call_args
        self.assertNotIn(config[local.SECRET_KEY], str(args))
        self.assertEqual(kwargs["env"][local.SECRET_KEY], config[local.SECRET_KEY])
        self.assertNotIn("POSTGRES_TEST_ALLOW_RESET", kwargs["env"])
        self.assertEqual(kwargs["env"]["DATABASE_URL"], "sqlite:///" + (root / "saas_mvp.db").as_posix())
        self.assertIn("127.0.0.1", args[0])

    def test_missing_database_not_created(self):
        root = Path(self.temporary.name) / "app"
        root.mkdir()
        config = local.load_config(self.directory)
        config[local.SECRET_KEY] = "TEST-SECRET-ONLY"
        self.write(config)
        with patch.object(local, "PROJECT_ROOT", root), \
                patch.object(local, "check", return_value={"project_app_id_matches": True}), \
                patch.object(local.subprocess, "run") as run:
            with self.assertRaises(local.ConfigurationError):
                local.serve(self.directory)
        run.assert_not_called()
        self.assertFalse((root / "saas_mvp.db").exists())


if __name__ == "__main__":
    unittest.main()
