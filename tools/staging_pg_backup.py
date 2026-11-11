"""Stop-and-copy backup / restore into a new, dedicated PostgreSQL staging project."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def digest(path):
    checksum = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            checksum.update(block)
    return checksum.hexdigest()


def inventory(directory):
    result = {}
    for path in sorted(directory.rglob("*")):
        if path.is_symlink():
            raise ValueError("Symlinks are not accepted in staging backups.")
        if path.is_file():
            result[path.relative_to(directory).as_posix()] = dict(sha256=digest(path), bytes=path.stat().st_size)
    return result


def verify_backup(directory):
    if directory.is_symlink():
        raise ValueError("Backup directory cannot be a symlink.")
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf8"))
    if (not isinstance(manifest, dict) or manifest.get("format") != 1 or
            manifest.get("scope") != "dedicated-postgres-staging"):
        raise ValueError("Unknown backup format.")
    files = manifest.get("files")
    if not isinstance(files, dict) or "database.dump" not in files or "data/session.key" not in files:
        raise ValueError("Incomplete staging backup.")
    actual = inventory(directory)
    actual.pop("manifest.json", None)
    if actual != files:
        raise ValueError("Backup file inventory or digest mismatch.")
    if any(name != "database.dump" and not name.startswith("data/") for name in files):
        raise ValueError("Unexpected backup file.")
    return manifest


def prepare_secrets(directory):
    directory = directory.resolve()
    if directory.is_relative_to(ROOT):
        raise ValueError("Keep database secrets outside the repository.")
    directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    os.chmod(directory, 0o700)
    for name in ("owner-password", "app-password"):
        with (directory / name).open("x", encoding="ascii") as stream:
            stream.write(secrets.token_hex(32))
        # Host parent is private; the explicitly granted Docker secret must be
        # readable by the unprivileged application user inside its container.
        os.chmod(directory / name, 0o444)


class StagingProject:
    def __init__(self, name):
        if not re.fullmatch(r"saas-pg-test-[a-z0-9][a-z0-9-]{0,48}", name):
            raise ValueError("Use an explicitly named saas-pg-test-* project.")
        self.name = name
        executable = os.environ.get("STAGING_COMPOSE_BIN")
        self.command = ([executable] if executable else ["docker", "compose"]) + [
            "-p", name, "-f", str(ROOT / "deploy/staging-postgres/compose.yaml")]
        config = json.loads(self.run("config", "--format", "json").stdout)
        for name, volume in config["volumes"].items():
            if volume.get("external") or volume.get("name") != self.name + "_" + name:
                raise ValueError("Only project-owned named volumes are supported.")
        for service in ("app", "database"):
            if config["services"][service].get("ports"):
                raise ValueError("Application and database must not have published ports.")
        self.config = config

    def run(self, *args, input=None):
        result = subprocess.run([*self.command, *args], input=input, capture_output=True)
        if result.returncode:
            raise RuntimeError("Staging Compose operation failed; inspect the dedicated project's logs.")
        return result

    def container(self, service):
        values = self.run("ps", "-a", "-q", service).stdout.decode().split()
        if len(values) != 1:
            raise ValueError("Expected exactly one staging service container.")
        data = json.loads(subprocess.check_output(["docker", "inspect", values[0]]))[0]
        labels = data["Config"]["Labels"]
        if labels.get("com.docker.compose.project") != self.name or labels.get("com.docker.compose.service") != service:
            raise ValueError("Wrong container identity.")
        return values[0], data

    def sql(self, statement):
        return self.run("exec", "-T", "database", "psql", "-X", "-A", "-t", "-v", "ON_ERROR_STOP=1",
                        "-U", "staging_owner", "-d", "saas_staging", "-c", statement).stdout.decode().strip()

    def identity(self):
        if self.sql("select current_database() || ':' || current_user") != "saas_staging:staging_owner":
            raise ValueError("Wrong database identity.")

    def backup(self, destination):
        if destination.resolve().is_relative_to(ROOT):
            raise ValueError("Backups contain private files; use a directory outside the repository.")
        destination.mkdir(mode=0o700, parents=True, exist_ok=False)
        os.chmod(destination, 0o700)
        self.identity()
        app, data = self.container("app")
        was_running = data["State"]["Running"]
        try:
            self.run("stop", "app")
            if self.container("app")[1]["State"]["Running"]:
                raise ValueError("Stop all application writes before backup.")
            with (destination / "database.dump").open("xb") as stream:
                result = subprocess.run([*self.command, "exec", "-T", "database",
                    "pg_dump", "-U", "staging_owner", "-d", "saas_staging",
                    "--format=custom", "--no-owner", "--no-acl"], stdout=stream, stderr=subprocess.PIPE)
                if result.returncode:
                    raise RuntimeError("PostgreSQL dump failed.")
            (destination / "data").mkdir()
            subprocess.run(["docker", "cp", app + ":/data/.", str(destination / "data")],
                           check=True, capture_output=True)
            files = inventory(destination)
            if "data/session.key" not in files:
                raise ValueError("Missing signing key.")
            for path in destination.rglob("*"):
                os.chmod(path, 0o700 if path.is_dir() else 0o600)
            manifest = dict(format=1, scope="dedicated-postgres-staging", source_project=self.name,
                            app_image_id=data["Image"], files=files,
                            completed_at=datetime.now(timezone.utc).isoformat())
            (destination / "manifest.json").write_text(json.dumps(manifest, indent=2)+"\n", encoding="utf8")
            os.chmod(destination / "manifest.json", 0o600)
            verify_backup(destination)
        finally:
            if was_running:
                self.run("start", "app")

    def restore(self, source, image_bundle=None):
        manifest = verify_backup(source)
        if self.run("ps", "-a", "-q").stdout.strip():
            raise ValueError("Restore requires a completely new project.")
        existing = subprocess.check_output(["docker", "volume", "ls", "-q",
                                            "--filter", "label=com.docker.compose.project=" + self.name])
        if existing.strip():
            raise ValueError("Restore refuses existing project volumes.")
        # Also reject unlabeled volumes with the intended names.
        for volume in self.config["volumes"].values():
            probe = subprocess.run(["docker", "volume", "inspect", volume["name"]], capture_output=True)
            if probe.returncode == 0:
                raise ValueError("Restore refuses an existing named volume.")
        # Restore with the exact application image used for this recovery point.
        image = self.config["services"]["app"]["image"]
        image_id = subprocess.check_output(["docker", "image", "inspect", image, "--format", "{{.Id}}"]).decode().strip()
        from tools.staging_image_archive import require_recovery_image
        require_recovery_image(manifest["app_image_id"], image_id, image_bundle)
        # OCR must recover the same release; a tag cannot silently select another.
        ocr_image = self.config["services"].get("ocr", {}).get("image")
        if ocr_image:
            ocr_id = subprocess.check_output(["docker", "image", "inspect", ocr_image,
                                              "--format", "{{.Id}}"]).decode().strip()
            if ocr_id != image_id:
                raise ValueError("OCR and application recovery images must match.")
        self.run("up", "-d", "--wait", "--wait-timeout", "120", "database")
        self.identity()
        if self.sql("select count(*) from information_schema.tables where table_schema='public'") != "0":
            raise ValueError("Restore requires an empty schema.")
        with (source / "database.dump").open("rb") as stream:
            result = subprocess.run([*self.command, "exec", "-T", "database", "pg_restore",
                "-U", "staging_owner", "-d", "saas_staging", "--role=saas_staging",
                "--no-owner", "--no-acl", "--single-transaction", "--exit-on-error"],
                stdin=stream, capture_output=True)
        if result.returncode:
            raise RuntimeError("PostgreSQL restore failed; keep this new project stopped for inspection.")
        # Restoring a historical login must not revive a token revoked after backup.
        if self.sql("select to_regclass('public.backoffice_sessions') is not null") == "t":
            self.sql("delete from backoffice_sessions")
        self.run("create", "--no-build", "app")
        app, _ = self.container("app")
        subprocess.run(["docker", "cp", str(source / "data") + "/.", app + ":/data"],
                       check=True, capture_output=True)
        # The normal app drops all capabilities. Only this one-off repair gets
        # ownership capabilities, on the newly created project's /data volume.
        self.run("run", "--rm", "--no-deps", "--user", "0", "--cap-add", "CHOWN",
                 "--cap-add", "DAC_OVERRIDE", "--cap-add", "FOWNER",
                 "app", "chown", "-R", "10001:10001", "/data")
        check_files = "from pathlib import Path; import json; from tools.staging_pg_backup import inventory; print(json.dumps(inventory(Path('/data'))))"
        copied = json.loads(self.run("run", "--rm", "--no-deps", "app", "python", "-c", check_files).stdout)
        expected = {name[5:]: value for name, value in manifest["files"].items() if name.startswith("data/")}
        if copied != expected:
            raise ValueError("Restored file inventory or digest mismatch.")
        self.run("run", "--rm", "--no-deps", "app", "python", "-m", "tools.staging_postgres", "check")
        self.run("up", "-d", "--no-build", "--wait", "--wait-timeout", "180", "app")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("secrets", "backup", "restore", "verify"))
    parser.add_argument("directory", type=Path)
    parser.add_argument("--project")
    parser.add_argument("--image-bundle", type=Path, help="Verified archive proof for OCI/classic ID compatibility")
    args = parser.parse_args(argv)
    try:
        if args.action == "secrets":
            prepare_secrets(args.directory)
        elif args.action == "verify":
            verify_backup(args.directory)
        else:
            if not args.project:
                raise ValueError("Specify a dedicated staging project.")
            project = StagingProject(args.project)
            if args.action == "backup":
                project.backup(args.directory)
            else:
                project.restore(args.directory, args.image_bundle)
        print("Staging " + args.action + " completed.")
        return 0
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
        print("Staging operation refused or failed. Existing backup files were preserved; inspect the dedicated project before retrying.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
