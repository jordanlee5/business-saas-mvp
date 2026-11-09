import contextlib
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from tools import release_bundle as release


class ReleaseBundleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "source"
        self.repo.mkdir()
        self.output = self.root / "bundle"
        self.git("init", "-q")
        for name in release.REQUIRED:
            path = self.repo / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("virtual-deployment-input\n")
        self.revision = self.commit()

    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.repo), *args], stderr=subprocess.PIPE).decode().strip()

    def commit(self):
        self.git("add", ".")
        self.git("-c", "user.name=Virtual Test", "-c", "user.email=virtual@example.invalid",
                 "-c", "core.hooksPath=/nonexistent", "commit", "-qm", "Virtual release")
        return self.git("rev-parse", "HEAD")

    def build(self):
        return release.create(self.repo, self.revision, self.output)

    def verify(self, report):
        return release.verify(self.output, self.revision, report["manifest_sha256"])

    def test_immutable_commit_excludes_worktree_private_and_business_files(self):
        for name in (".env", "saas_mvp.db", "uploads/private.txt", "app/untracked.key"):
            path = self.repo / name
            path.parent.mkdir(exist_ok=True, parents=True)
            path.write_text("virtual-private-worktree-data")
        (self.repo / "app/main.py").write_text("uncommitted-change")
        report = self.build()
        self.verify(report)
        self.assertEqual((self.output / "app/main.py").read_text(), "virtual-deployment-input\n")
        self.assertFalse((self.output / ".env").exists())
        self.assertFalse((self.output / "saas_mvp.db").exists())
        self.assertFalse((self.output / "uploads").exists())
        self.assertFalse((self.output / "app/untracked.key").exists())

    def test_repeat_build_same_manifest_and_existing_destination_refused(self):
        first = self.build()
        second = release.create(self.repo, self.revision, self.root / "bundle-two")
        self.assertEqual(first, second)
        with self.assertRaises(ValueError): self.build()
        self.verify(first)

    def test_tracked_secret_content_and_private_filename_refused_before_output(self):
        for name, data in (("app/config.py", "-----BEGIN " + "PRIVATE KEY-----\nvirtual-only"),
                           ("app/server-config.json", "{}")):
            with self.subTest(name=name):
                p = self.repo / name
                p.write_text(data)
                self.revision = self.commit()
                with self.assertRaises(ValueError): self.build()
                self.assertFalse(self.output.exists())
                p.unlink()

    def test_missing_extra_and_modified_files_rejected(self):
        report = self.build()
        p = self.output / "app/main.py"
        original = p.read_bytes()
        for change in ("missing", "modified", "extra"):
            with self.subTest(change=change):
                if change == "missing": p.unlink()
                if change == "modified": p.write_text("changed")
                if change == "extra": (self.output / "unexpected.txt").write_text("extra")
                with self.assertRaises(ValueError): self.verify(report)
                p.write_bytes(original)
                if change == "extra": (self.output / "unexpected.txt").unlink()
                self.verify(report)

    def test_rewritten_manifest_and_wrong_revision_rejected(self):
        report = self.build()
        with self.assertRaises(ValueError): release.verify(self.output, "0"*40, report["manifest_sha256"])
        p = self.output / "app/main.py"
        p.write_text("modified")
        manifest_path = self.output / "release-manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["files"]["app/main.py"].update(sha256=release.sha(p.read_bytes()), bytes=p.stat().st_size)
        manifest_path.write_text(json.dumps(manifest))
        with self.assertRaises(ValueError): self.verify(report)

    def test_tracked_symlink_refused(self):
        # Use a Git object fixture: Windows need not enable symlink creation.
        blob = subprocess.check_output(["git", "-C", str(self.repo), "hash-object", "-w", "--stdin"], input=b"../../private").decode().strip()
        self.git("update-index", "--add", "--cacheinfo", "120000," + blob + ",app/link")
        self.git("-c", "user.name=Virtual Test", "-c", "user.email=virtual@example.invalid",
                 "-c", "core.hooksPath=/nonexistent", "commit", "-qm", "Virtual symlink")
        self.revision = self.git("rev-parse", "HEAD")
        with self.assertRaises(ValueError): self.build()
        self.assertFalse(self.output.exists())

    def test_invalid_reference_and_repository_destination_refused(self):
        for revision, output in (("HEAD", self.output), (self.revision, self.repo / "bundle")):
            with self.assertRaises(ValueError): release.create(self.repo, revision, output)

    def test_cli_refusal_does_not_echo_private_content(self):
        (self.repo / "app/config.py").write_text("-----BEGIN " + "PRIVATE KEY-----\nDO-NOT-ECHO")
        self.revision = self.commit()
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = release.main(["create", "--repo", str(self.repo), "--revision", self.revision,
                                 "--output", str(self.output)])
        self.assertEqual(code, 1)
        self.assertNotIn("DO-NOT-ECHO", out.getvalue() + err.getvalue())


if __name__ == "__main__":
    unittest.main()
