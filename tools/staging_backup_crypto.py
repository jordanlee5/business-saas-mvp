"""Authenticated encrypted recovery points. Private keys never belong on the source host."""
import argparse
import base64
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import tarfile
import tempfile

from tools.staging_pg_backup import ROOT, digest, inventory, verify_backup

LIMIT = 20 * 1024**3
SUITE = "RSA-OAEP-SHA256+AES-256-GCM"


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def b64(value):
    return base64.b64encode(value).decode("ascii")


def unb64(value):
    return base64.b64decode(value, validate=True)


def safe(path):
    path = Path(os.path.abspath(path))
    for p in (path, *path.parents):
        if p.is_symlink() or (p.exists() and getattr(p.lstat(), "st_file_attributes", 0) & 0x400):
            raise ValueError("Links refused")
    if path.is_relative_to(ROOT) or ROOT.is_relative_to(path):
        raise ValueError("Use private storage outside repository")
    return path


def new_directory(path):
    path = safe(path)
    path.mkdir(mode=0o700, parents=True, exist_ok=False)
    os.chmod(path, 0o700)
    return path


def keygen(directory):
    # POSIX private directory semantics are required; Windows custodians can use WSL.
    if os.name != "posix":
        raise ValueError("Generate and hold keys in a private Linux/WSL directory")
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives import serialization as s
    directory = new_directory(directory)
    key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    values = {"recovery-private.pem": key.private_bytes(s.Encoding.PEM, s.PrivateFormat.PKCS8, s.NoEncryption()),
              "recovery-public.pem": key.public_key().public_bytes(s.Encoding.PEM, s.PublicFormat.SubjectPublicKeyInfo)}
    for name, data in values.items():
        with (directory / name).open("xb") as stream:
            stream.write(data)
        os.chmod(directory / name, 0o600)


def public_key(path):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    key = serialization.load_pem_public_key(safe(path).read_bytes())
    if not isinstance(key, rsa.RSAPublicKey) or key.key_size < 3072:
        raise ValueError("RSA key must be at least 3072 bits")
    return key


def padding():
    from cryptography.hazmat.primitives.asymmetric import padding as p
    from cryptography.hazmat.primitives import hashes
    return p.OAEP(mgf=p.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None)


def aad(manifest):
    return canonical({k: v for k, v in manifest.items() if k not in ("files", "tag")})


def verify_encrypted(directory, manifest):
    if (manifest.get("format") != 2 or manifest.get("scope") != "dedicated-postgres-staging"
            or manifest.get("suite") != SUITE or len(unb64(manifest["nonce"])) != 12
            or len(unb64(manifest["tag"])) != 16 or len(unb64(manifest["wrapped_key"])) < 384):
        raise ValueError("Invalid encryption envelope")
    files = inventory(directory)
    files.pop("manifest.json", None)
    if set(files) != {"payload.enc"} or files != manifest.get("files") or files["payload.enc"]["bytes"] > LIMIT:
        raise ValueError("Encrypted backup inventory mismatch")
    return manifest


def crypt_stream(source, target, context):
    total = 0
    while True:
        block = source.read(1024 * 1024)
        if not block:
            break
        total += len(block)
        if total > LIMIT:
            raise ValueError("Recovery point exceeds encryption size limit")
        target.write(context.update(block))
    target.write(context.finalize())


def encrypt(source, destination, recipient):
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    source = safe(source)
    original = verify_backup(source)
    if original["format"] != 1:
        raise ValueError("Source must be a plain recovery point")
    key = public_key(recipient)
    destination = safe(destination)
    if destination.is_relative_to(source) or source.is_relative_to(destination):
        raise ValueError("Separate source and destination required")
    destination = new_directory(destination)
    data_key, nonce = os.urandom(32), os.urandom(12)
    manifest = dict(format=2, scope=original["scope"], source_project=original["source_project"],
                    completed_at=original["completed_at"], app_image_id=original.get("app_image_id"),
                    suite=SUITE, nonce=b64(nonce), wrapped_key=b64(key.encrypt(data_key, padding())))
    context = Cipher(algorithms.AES(data_key), modes.GCM(nonce)).encryptor()
    context.authenticate_additional_data(aad(manifest))
    # Temporary plaintext stays in a private directory and is never published as a success.
    with tempfile.TemporaryFile(dir=destination) as archive:
        with tarfile.open(fileobj=archive, mode="w") as tar:
            for name in ["manifest.json", *original["files"]]:
                tar.add(source / name, arcname=name, recursive=False)
        if verify_backup(source) != original:
            raise ValueError("Source changed while encrypting")
        archive.seek(0)
        with (destination / "payload.enc").open("xb") as target:
            crypt_stream(archive, target, context)
    os.chmod(destination / "payload.enc", 0o600)
    manifest.update(tag=b64(context.tag), files=inventory(destination))
    (destination / "manifest.json").write_bytes(canonical(manifest))
    os.chmod(destination / "manifest.json", 0o600)
    return verify_backup(destination)


def decrypt(source, destination, identity):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    source, destination, identity = safe(source), safe(destination), safe(identity)
    if destination.exists() or destination.is_relative_to(source) or identity.is_relative_to(source):
        raise ValueError("New destination and separate private key required")
    if os.name != "posix" or identity.stat().st_mode & 0o077 or identity.parent.stat().st_mode & 0o077:
        raise ValueError("Private key requires Linux/WSL owner-only permissions")
    manifest = verify_backup(source)
    if manifest["format"] != 2:
        raise ValueError("Encrypted recovery point required")
    key = serialization.load_pem_private_key(identity.read_bytes(), password=None)
    data_key = key.decrypt(unb64(manifest["wrapped_key"]), padding())
    context = Cipher(algorithms.AES(data_key), modes.GCM(unb64(manifest["nonce"]), unb64(manifest["tag"]))).decryptor()
    context.authenticate_additional_data(aad(manifest))
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".decrypt-", dir=destination.parent) as temp:
        work = Path(temp)
        with tempfile.TemporaryFile(dir=work) as archive:
            with (source / "payload.enc").open("rb") as encrypted:
                crypt_stream(encrypted, archive, context)
            # GCM finalize has succeeded before parsing or publishing any plaintext.
            archive.seek(0)
            out = work / "verified"
            out.mkdir(mode=0o700)
            seen, total = set(), 0
            with tarfile.open(fileobj=archive, mode="r:") as tar:
                for member in tar:
                    name = PurePosixPath(member.name)
                    total += member.size
                    if (not member.isfile() or name.is_absolute() or ".." in name.parts
                            or "\\" in member.name or ":" in member.name or member.name in seen
                            or not name.parts or len(seen) >= 100000 or total > LIMIT
                            or member.size < 0):
                        raise ValueError("Unsafe encrypted payload")
                    seen.add(member.name)
                    target = out.joinpath(*name.parts)
                    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    with tar.extractfile(member) as src, target.open("xb") as dst:
                        shutil.copyfileobj(src, dst)
                    os.chmod(target, 0o600)
            original = verify_backup(out)
            if original["format"] != 1 or any(original.get(k) != manifest.get(k) for k in
                    ("source_project", "completed_at", "app_image_id")):
                raise ValueError("Envelope identity mismatch")
            out.rename(destination)
    return original


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("keygen", "encrypt", "decrypt"))
    parser.add_argument("destination", type=Path)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--key", type=Path)
    args = parser.parse_args()
    try:
        if args.action == "keygen":
            keygen(args.destination)
        else:
            globals()[args.action](args.source, args.destination, args.key)
    except Exception:
        print("Encryption operation refused or failed; existing recovery points preserved.")
        return 1
    print("Encryption operation completed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
