"""Private, tag-free application image archive tied to a verified staging backup.

This is an operator-invoked recovery tool, not a scheduled backup or publisher.
Classic and single-image OCI-layout Docker save archives are accepted.
Only uncompressed layers are supported. No files are extracted on host.
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
    image_id(expected)
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
        if entry.get("RepoTags") not in (None, []):
            raise ValueError("Only an untagged image is accepted")
        allowed = {"manifest.json"}
        oci = "oci-layout" in members
        descriptors = None

        def descriptor(item):
            if not isinstance(item, dict):
                raise ValueError("Invalid blob descriptor")
            identity = image_id(item.get("digest"))
            name = "blobs/" + identity.replace(":", "/")
            member = members.get(name)
            if (member is None or not member.isfile() or
                    type(item.get("size")) is not int or item["size"] != member.size):
                raise ValueError("Missing blob or descriptor size mismatch")
            checksum = hashlib.sha256()
            with archive.extractfile(member) as stream:
                for block in iter(lambda: stream.read(1024**2), b""):
                    checksum.update(block)
            if "sha256:" + checksum.hexdigest() != identity:
                raise ValueError("Blob digest mismatch")
            allowed.add(name)
            return name, identity

        if oci:
            _, layout = read_json("oci-layout")
            _, index = read_json("index.json")
            if layout != {"imageLayoutVersion": "1.0.0"} or index.get("schemaVersion") != 2:
                raise ValueError("Unsupported OCI layout")
            roots = index.get("manifests")
            if not isinstance(roots, list) or len(roots) != 1:
                raise ValueError("Exactly one OCI image required")
            root_name, root_id = descriptor(roots[0])
            if root_id != expected or roots[0].get("mediaType") not in (
                    "application/vnd.docker.distribution.manifest.v2+json",
                    "application/vnd.oci.image.manifest.v1+json"):
                raise ValueError("OCI manifest identity mismatch")
            _, root = read_json(root_name)
            if root.get("schemaVersion") != 2 or root.get("subject") is not None:
                raise ValueError("Unsupported OCI manifest")
            config_descriptor = root.get("config")
            if not isinstance(config_descriptor, dict) or config_descriptor.get("mediaType") not in (
                    "application/vnd.docker.container.image.v1+json",
                    "application/vnd.oci.image.config.v1+json"):
                raise ValueError("Unsupported OCI configuration")
            config_name, config_id = descriptor(config_descriptor)
            descriptors = root.get("layers")
            if not isinstance(descriptors, list):
                raise ValueError("Invalid OCI layers")
            allowed.update(("oci-layout", "index.json", "blobs", "blobs/sha256"))
        else:
            config_name, config_id = expected[7:] + ".json", expected
        if entry.get("Config") != config_name:
            raise ValueError("Docker and OCI configurations disagree")
        raw, config = read_json(config_name)
        if "sha256:" + hashlib.sha256(raw).hexdigest() != config_id:
            raise ValueError("Image configuration identity mismatch")
        if not isinstance(config, dict) or not isinstance(config.get("rootfs"), dict):
            raise ValueError("Invalid image rootfs")
        layers = entry.get("Layers")
        hashes = config["rootfs"].get("diff_ids")
        if (not isinstance(layers, list) or not isinstance(hashes, list) or
                len(layers) != len(hashes) or len(set(layers)) != len(layers)):
            raise ValueError("Invalid image layers")
        allowed.add(config_name)
        if descriptors is not None:
            if len(descriptors) != len(layers):
                raise ValueError("Docker and OCI layers disagree")
            for name, expected_hash, item in zip(layers, hashes, descriptors):
                if not isinstance(item, dict) or item.get("mediaType") not in (
                        "application/vnd.docker.image.rootfs.diff.tar",
                        "application/vnd.oci.image.layer.v1.tar"):
                    raise ValueError("Only uncompressed OCI layers are supported")
                blob_name, blob_id = descriptor(item)
                if blob_name != name or blob_id != expected_hash:
                    raise ValueError("Docker/OCI layer or rootfs digest mismatch")

        for name, expected_hash in zip(layers, hashes):
            pattern = r"blobs/sha256/[0-9a-f]{64}" if oci else r"[0-9a-f]{64}/layer\.tar"
            if not isinstance(name, str) or not re.fullmatch(pattern, name):
                raise ValueError("Unsupported Docker archive layout")
            image_id(expected_hash)
            if not oci:
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
        return config_id


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
    # This mapping is derived from verified archive bytes, never sidecar claims.
    config_id = check_tar(archive, expected)
    return {**manifest, "config_id": config_id}


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
    manifest = verify(temporary, expected)
    # rename refuses a nonempty existing bundle; no existing archive is replaced.
    if destination.exists():
        raise ValueError("Destination appeared during export")
    temporary.rename(destination)
    return manifest


def load(backup, bundle):
    expected = image_id(verify_backup(Path(backup)).get("app_image_id"))
    manifest = verify(bundle, expected)
    docker("image", "load", "--input", str(Path(bundle) / "image.tar"))
    try:
        inspect(expected)
        runtime_id = expected
    except RuntimeError:
        # Classic Docker identifies the verified configuration, not the OCI root.
        if manifest["config_id"] == expected:
            raise
        inspect(manifest["config_id"])
        runtime_id = manifest["config_id"]
    return {**manifest, "runtime_image_id": runtime_id}


def require_recovery_image(expected, actual, bundle=None):
    """An alternative runtime ID requires a complete cryptographic archive proof."""
    image_id(expected)
    image_id(actual)
    proof = verify(bundle, expected) if bundle is not None else None
    if actual != expected and (proof is None or actual != proof["config_id"]):
        raise ValueError("Restore requires the backup's verified application image.")


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
        print(json.dumps(dict(status="ok", action=args.action, image_id=result["image_id"], config_id=result["config_id"],
                              runtime_image_id=result.get("runtime_image_id"), bytes=result["bytes"])))
        return 0
    except (OSError, ValueError, TypeError, KeyError, AttributeError, tarfile.TarError, RuntimeError, subprocess.SubprocessError):
        print("Image archive refused or failed; keep private files for investigation.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
