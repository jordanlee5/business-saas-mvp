"""Create a deployment directory from immutable Git objects; never copy the worktree."""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys

from tools.check_secrets import inspect

ROOT_FILES = {"Dockerfile", ".dockerignore", "requirements.txt", "requirements-dev.txt", "alembic.ini"}
ROOT_DIRS = {"app", "migrations", "tools", "deploy"}
REQUIRED = ROOT_FILES | {"app/main.py", "deploy/staging-postgres/compose.yaml"}


def selected(name):
    p = PurePosixPath(name)
    return name in ROOT_FILES or len(p.parts) > 1 and p.parts[0] in ROOT_DIRS


def safe_name(name):
    p = PurePosixPath(name)
    return (isinstance(name, str) and not p.is_absolute() and str(p) == name and
            not any(part in {"..", ".git", "__pycache__", "node_modules", ".runtime"} for part in p.parts)
            and "\\" not in name and ":" not in name and selected(name))


def sha(data):
    return hashlib.sha256(data).hexdigest()


def git(repo, *args):
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, timeout=60)
    if result.returncode:
        raise ValueError("Git object unavailable")
    return result.stdout


def validate_revision(value):
    if not re.fullmatch(r"[0-9a-f]{40}", value):
        raise ValueError("Use the full reviewed Git commit")


def create(repo, revision, output):
    validate_revision(revision)
    repo = repo.resolve()
    output = output.absolute()
    if output.exists() or output.is_symlink() or output.resolve().is_relative_to(repo):
        raise ValueError("Use a new output directory outside the source repository")
    if git(repo, "rev-parse", "--verify", revision + "^{commit}").decode().strip() != revision:
        raise ValueError("Expected a commit")
    files, payload = {}, {}
    for entry in git(repo, "ls-tree", "-r", "-z", revision).split(b"\0"):
        if not entry:
            continue
        meta, raw_name = entry.split(b"\t", 1)
        mode, kind, oid = meta.decode().split()
        name = raw_name.decode("utf8")
        if not selected(name):
            continue
        if not safe_name(name) or mode not in {"100644", "100755"} or kind != "blob":
            raise ValueError("Unsafe deployment entry")
        data = git(repo, "cat-file", "blob", oid)
        if inspect(name, data):
            raise ValueError("Secret gate rejected a deployment file")
        payload[name] = data
        files[name] = {"sha256": sha(data), "bytes": len(data), "mode": mode}
    if not REQUIRED.issubset(files):
        raise ValueError("Deployment inputs missing")
    manifest = {"format": 1, "scope": "saas-deployment-source", "git_commit": revision, "files": files}
    raw = (json.dumps(manifest, sort_keys=True, indent=2) + "\n").encode("utf8")
    # Validate every object before creating the output. Partial I/O failures remain
    # visible; never overwrite or delete an existing directory on retry.
    output.mkdir(parents=True, mode=0o700, exist_ok=False)
    for name, data in payload.items():
        path = output / name
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("xb") as stream:
            stream.write(data)
        path.chmod(0o755 if files[name]["mode"] == "100755" else 0o644)
    with (output / "release-manifest.json").open("xb") as stream:
        stream.write(raw)
    return {"git_commit": revision, "manifest_sha256": sha(raw), "files": len(files)}


def verify(directory, revision, manifest_sha256):
    validate_revision(revision)
    if not re.fullmatch(r"[0-9a-f]{64}", manifest_sha256):
        raise ValueError("Expected independently recorded manifest hash")
    if directory.is_symlink():
        raise ValueError("Symlink directory refused")
    manifest_path = directory / "release-manifest.json"
    if manifest_path.is_symlink():
        raise ValueError("Symlink manifest refused")
    raw = manifest_path.read_bytes()
    if sha(raw) != manifest_sha256:
        raise ValueError("Manifest changed")
    manifest = json.loads(raw)
    if (not isinstance(manifest, dict) or manifest.get("format") != 1 or
            manifest.get("scope") != "saas-deployment-source" or manifest.get("git_commit") != revision):
        raise ValueError("Wrong release identity")
    files = manifest.get("files")
    if not isinstance(files, dict) or not REQUIRED.issubset(files):
        raise ValueError("Incomplete manifest")
    actual = set()
    for path in directory.rglob("*"):
        if path.is_symlink():
            raise ValueError("Symlinks refused")
        if path.is_file():
            actual.add(path.relative_to(directory).as_posix())
        elif not path.is_dir():
            raise ValueError("Special files refused")
    if actual != set(files) | {"release-manifest.json"}:
        raise ValueError("Missing or extra deployment files")
    for name, entry in files.items():
        if not safe_name(name) or not isinstance(entry, dict) or entry.get("mode") not in {"100644", "100755"}:
            raise ValueError("Unsafe manifest entry")
        path = directory / name
        data = path.read_bytes()
        if entry.get("bytes") != len(data) or entry.get("sha256") != sha(data) or inspect(name, data):
            raise ValueError("Deployment content rejected")
        if os.name != "nt" and bool(path.stat().st_mode & 0o111) != (entry["mode"] == "100755"):
            raise ValueError("Executable mode changed")
    return {"git_commit": revision, "manifest_sha256": manifest_sha256, "files": len(files)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    build = commands.add_parser("create")
    build.add_argument("--repo", required=True, type=Path)
    build.add_argument("--revision", required=True)
    build.add_argument("--output", required=True, type=Path)
    check = commands.add_parser("verify")
    check.add_argument("--directory", required=True, type=Path)
    check.add_argument("--revision", required=True)
    check.add_argument("--manifest-sha256", required=True)
    args = parser.parse_args(argv)
    try:
        result = (create(args.repo, args.revision, args.output) if args.action == "create" else
                  verify(args.directory, args.revision, args.manifest_sha256))
        print(json.dumps({"status": "ok", **result}))
        return 0
    except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError):
        print("Release bundle refused or incomplete. No private values are displayed; use a new destination after inspection.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
