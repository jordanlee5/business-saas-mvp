"""Password-protected offline recovery-key copy. No network or database access."""
import argparse
import getpass
import hashlib
import json
import os
from pathlib import Path
import sys
import warnings

from cryptography.hazmat.primitives import serialization as s
from cryptography.hazmat.primitives.asymmetric import rsa
from tools.staging_backup_crypto import safe, new_directory, padding


def public_bytes(key):
    return key.public_key().public_bytes(s.Encoding.PEM, s.PublicFormat.SubjectPublicKeyInfo)


def fingerprint(key):
    return hashlib.sha256(public_bytes(key)).hexdigest()


def validate(key):
    if not isinstance(key, rsa.RSAPrivateKey) or key.key_size < 3072:
        raise ValueError("Recovery RSA key required")
    challenge = os.urandom(32)
    if key.decrypt(key.public_key().encrypt(challenge, padding()), padding()) != challenge:
        raise ValueError("Recovered key challenge failed")
    return key


def private_source(path):
    path = safe(path)
    if os.name != "posix" or path.stat().st_mode & 0o077 or path.parent.stat().st_mode & 0o077:
        raise ValueError("Source key requires owner-only Linux/WSL storage")
    return validate(s.load_pem_private_key(path.read_bytes(), password=None))


def write_new(path, data):
    with path.open("xb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    os.chmod(path, 0o600)


def verify(directory, password, expected_public):
    directory, expected_public = safe(directory), safe(expected_public)
    paths = [directory / n for n in ("private-key.encrypted.pem", "public.pem", "custody.json")]
    for path in paths:
        safe(path)
    if {p.name for p in directory.iterdir()} != {p.name for p in paths}:
        raise ValueError("Unexpected key package files")
    key = validate(s.load_pem_private_key(paths[0].read_bytes(), password=password))
    expected = s.load_pem_public_key(expected_public.read_bytes()).public_bytes(s.Encoding.PEM, s.PublicFormat.SubjectPublicKeyInfo)
    if public_bytes(key) != expected or paths[1].read_bytes() != expected:
        raise ValueError("Wrong recovery identity")
    metadata = json.loads(paths[2].read_text())
    if metadata != {"format": 1, "public_sha256": fingerprint(key)}:
        raise ValueError("Wrong custody metadata")
    return key


def export(source, destination, password):
    if not isinstance(password, bytes) or len(password) < 16 or len(password) > 1024:
        raise ValueError("Use a unique long passphrase of at least 16 bytes")
    source, destination = safe(source), safe(destination)
    if destination.is_relative_to(source.parent):
        raise ValueError("Use separate offline storage")
    key = private_source(source)
    encrypted = key.private_bytes(s.Encoding.PEM, s.PrivateFormat.PKCS8, s.BestAvailableEncryption(password))
    destination = new_directory(destination)
    write_new(destination / "private-key.encrypted.pem", encrypted)
    write_new(destination / "public.pem", public_bytes(key))
    write_new(destination / "custody.json", json.dumps({"format": 1, "public_sha256": fingerprint(key)}).encode())
    # Re-read the actual output, not the in-memory serialization.
    recovered = verify(destination, password, source.parent / "recovery-public.pem")
    if fingerprint(recovered) != fingerprint(key):
        raise ValueError("Offline read-back failed")
    return fingerprint(key)


def recover(directory, destination, password, expected_public):
    key = verify(directory, password, expected_public)
    if os.name != "posix":
        raise ValueError("Recover only into owner-only Linux/WSL storage")
    destination = safe(destination)
    if destination.is_relative_to(safe(directory)):
        raise ValueError("Do not place plaintext key on offline media")
    destination = new_directory(destination)
    # DrvFS cannot guarantee owner-only POSIX permissions; never write plaintext there.
    if destination.stat().st_mode & 0o077:
        raise ValueError("Destination cannot enforce private permissions")
    write_new(destination / "recovery-private.pem", key.private_bytes(s.Encoding.PEM, s.PrivateFormat.PKCS8, s.NoEncryption()))
    write_new(destination / "recovery-public.pem", public_bytes(key))
    return fingerprint(private_source(destination / "recovery-private.pem"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("export", "verify", "recover"))
    parser.add_argument("source", type=Path)
    parser.add_argument("--destination", type=Path)
    parser.add_argument("--public-key", type=Path)
    args = parser.parse_args()
    try:
        if not sys.stdin.isatty():
            raise ValueError("Interactive password input required")
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            password = getpass.getpass("Offline key password (hidden): ").encode("utf8")
            if args.action == "export" and password != getpass.getpass("Repeat password (hidden): ").encode("utf8"):
                raise ValueError("Passwords differ")
        if args.action == "export":
            result = export(args.source, args.destination, password)
        elif args.action == "recover":
            result = recover(args.source, args.destination, password, args.public_key)
        else:
            result = fingerprint(verify(args.source, password, args.public_key))
        print("Key copy verified. Public fingerprint: " + result)
        return 0
    except Exception:
        print("Key operation refused or failed. Original key preserved; no password or key details logged.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
