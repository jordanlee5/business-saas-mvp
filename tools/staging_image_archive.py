"""Private, tag-free application image archive tied to a verified staging backup.

This is an operator-invoked recovery tool, not a scheduled backup or publisher.
Only classic Docker save archives are accepted. No files are extracted on host.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import uuid

from tools.staging_pg_backup import ROOT, digest, verify_backup

MAX_BYTES = 20 * 1024**3
TIMEOUT = 900


def image_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", value):
        raise ValueError("Invalid image identity")
    return value


def private_path(path):
    path = Path(path).absolute()
    for part in (path, *path.parents):
        if part.exists() or part.is_symlink():
            info = part.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise ValueError("Linked paths refused")
    if path.resolve().is_relative_to(ROOT.resolve()):
        raise ValueError("Keep image archives outside the repository")
    return path


def json_file(path):
    if not path.is_file() or path.stat().st_size > 65536:
        raise ValueError("Invalid manifest")
    return json.loads(path.read_text(encoding="utf8"))


def check_tar(path, expected):
    """Check one untagged image, its configuration identity and every layer hash."""
    with tarfile.open(path, "r:") as archive:
        members = {}
        for member in archive:
            name = member.name.rstrip("/")
            parts = PurePosixPath(name).parts
            if (not parts or name.startswith("/") or ".." in parts or "\\" in name or
                    name in members or len(members) >= 10000 or
                    not (member.isfile() or member.isdir())):
                raise ValueError("Unsafe image archive")
            members[name] = member

        def read_json(name):
            member = members.get(name)
            if member is None or not member.isfile() or member.size > 4 * 1024**2:
                raise ValueError("Invalid Docker manifest/config")
            with archive.extractfile(member) as stream:
                raw = stream.read()
            return raw, json.loads(raw)

        _, manifest = read_json("manifest.json")
        if not isinstance(manifest, list) or len(manifest) != 1 or not isinstance(manifest[0], dict):
            raise ValueError("Exactly one image required")
        entry = manifest[0]
        config_name = expected[7:] + ".json"
        if entry.get("Config") != config_name or entry.get("RepoTags") not in (None, []):
            raise ValueError("Only the exact untagged image is accepted")
        raw, config = read_json(config_name)
        if "sha256:" + hashlib.sha256(raw).hexdigest() != expected:
            raise ValueError("Image configuration identity mismatch")
        if not isinstance(config, dict) or not isinstance(config.get("rootfs"), dict):
            raise ValueError("Invalid image rootfs")
        layers = entry.get("Layers")
        hashes = config["rootfs"].get("diff_ids")
        if (not isinstance(layers, list) or not isinstance(hashes, list) or
                len(layers) != len(hashes) or len(set(layers)) != len(layers)):
            raise ValueError("Invalid image layers")
        allowed = {"manifest.json", config_name}
        for name, expected_hash in zip(layers, hashes):
            if not isinstance(name, str) or not re.fullmatch(r"[0-9a-f]{64}/layer\.tar", name):
                raise ValueError("Unsupported Docker archive layout")
            image_id(expected_hash)
            parent = name.split("/")[0]
            allowed.update((parent, parent + "/VERSION", parent + "/json", name))
            member = members.get(name)
            if member is None or not member.isfile():
                raise ValueError("Missing layer")
            checksum = hashlib.sha256()
            with archive.extractfile(member) as stream:
                for block in iter(lambda: stream.read(1024**2), b""):
                    checksum.update(block)
            if "sha256:" + checksum.hexdigest() != expected_hash:
                raise ValueError("Layer identity mismatch")
        if set(members) - allowed:
            raise ValueError("Unexpected archive contents")


def verify(bundle, expected):
    expected = image_id(expected)
    bundle = private_path(bundle)
    if not bundle.is_dir() or {p.name for p in bundle.iterdir()} != {"image.tar", "manifest.json"}:
        raise ValueError("Incomplete archive bundle")
    for name in ("image.tar", "manifest.json"):
        private_path(bundle / name)
        if not stat.S_ISREG((bundle / name).stat().st_mode):
            raise ValueError("Regular files required")
    manifest = json_file(bundle / "manifest.json")
    archive = bundle / "image.tar"
    if (not isinstance(manifest, dict) or manifest.get("format") != 1 or
            manifest.get("scope") != "staging-application-image" or manifest.get("image_id") != expected or
            not 0 < archive.stat().st_size <= MAX_BYTES or
            manifest.get("bytes") != archive.stat().st_size or manifest.get("sha256") != digest(archive)):
        raise ValueError("Archive digest or identity mismatch")
    check_tar(archive, expected)
    return manifest


def docker(*args):
    result = subprocess.run(["docker", *args], capture_output=True, timeout=TIMEOUT)
    if result.returncode:
        raise RuntimeError("Docker image operation failed")
    return result.stdout


def inspect(expected):
    # Never print environment, labels, credentials or full image metadata.
    actual = docker("image", "inspect", expected, "--format", "{{.Id}}").decode().strip()
    if actual != expected:
        raise ValueError("Docker image identity mismatch")


def create(backup, destination):
    expected = image_id(verify_backup(Path(backup)).get("app_image_id"))
    destination = private_path(destination)
    if destination.exists():
        raise ValueError("Destination exists; verify it instead")
    inspect(expected)
    size = int(docker("image", "inspect", expected, "--format", "{{.Size}}").decode().strip())
    if not 0 < size <= MAX_BYTES or shutil.disk_usage(destination.parent).free < size * 2 + 1024**3:
        raise ValueError("Insufficient archive space")
    temporary = destination.with_name(destination.name + ".partial-" + uuid.uuid4().hex)
    temporary.mkdir(mode=0o700)
    os.chmod(temporary, 0o700)
    # Preserve partial data for investigation; never publish it as successful.
    docker("image", "save", expected, "--output", str(temporary / "image.tar"))
    archive = temporary / "image.tar"
    os.chmod(archive, 0o600)
    manifest = dict(format=1, scope="staging-application-image", image_id=expected,
                    bytes=archive.stat().st_size, sha256=digest(archive),
                    completed_at=datetime.now(timezone.utc).isoformat())
    (temporary / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf8")
    os.chmod(temporary / "manifest.json", 0o600)
    verify(temporary, expected)
    # rename refuses a nonempty existing bundle; no existing archive is replaced.
    if destination.exists():
        raise ValueError("Destination appeared during export")
    temporary.rename(destination)
    return manifest


def load(backup, bundle):
    expected = image_id(verify_backup(Path(backup)).get("app_image_id"))
    manifest = verify(bundle, expected)
    docker("image", "load", "--input", str(Path(bundle) / "image.tar"))
    inspect(expected)
    return manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("create", "verify", "load"))
    parser.add_argument("--backup", type=Path, required=True)
    parser.add_argument("--bundle", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.action == "create":
            result = create(args.backup, args.bundle)
        elif args.action == "load":
            result = load(args.backup, args.bundle)
        else:
            expected = image_id(verify_backup(args.backup).get("app_image_id"))
            result = verify(args.bundle, expected)
        print(json.dumps(dict(status="ok", action=args.action, image_id=result["image_id"], bytes=result["bytes"])))
        return 0
    except (OSError, ValueError, TypeError, KeyError, AttributeError, tarfile.TarError, RuntimeError, subprocess.SubprocessError):
        print("Image archive refused or failed; keep private files for investigation.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
