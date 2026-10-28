"""Local-only WeChat configuration. Secrets never belong in the mini program.

Run from the repository with: python -m app.wechat_local --directory PATH ACTION
The dedicated private directory must be outside the repository.
"""

import argparse
import getpass
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import tempfile
import warnings


PROJECT_ROOT = Path(__file__).resolve().parent.parent
APP_ID = "wx954783174df6dfdf"
APP_ID_KEY = "WECHAT_MINIPROGRAM_APP_ID"
SECRET_KEY = "WECHAT_MINIPROGRAM_APP_SECRET"
SIGNING_KEY = "MINIPROGRAM_SESSION_SIGNING_KEY"
KEYS = {APP_ID_KEY, SECRET_KEY, SIGNING_KEY}


class ConfigurationError(Exception):
    """Messages must never include values read from a private file."""


def private_directory(directory: Path) -> Path:
    target = directory.expanduser().resolve()
    if target.is_relative_to(PROJECT_ROOT):
        raise ConfigurationError("Private configuration must be outside the repository.")
    return target


def _secure_new_directory(directory: Path) -> None:
    # Refuse existing directories: never change an unrelated directory's ACL.
    directory.mkdir(mode=0o700, parents=False, exist_ok=False)
    if os.name == "nt":
        identity = subprocess.run(
            ["whoami", "/user", "/fo", "csv", "/nh"],
            check=True, capture_output=True, text=True,
        ).stdout
        sid = re.search(r"S-1-\d+(?:-\d+)+", identity)
        if not sid:
            raise ConfigurationError("Unable to determine the local account.")
        subprocess.run(
            ["icacls", str(directory), "/inheritance:r", "/grant:r",
             "*" + sid.group() + ":(OI)(CI)F"],
            check=True, capture_output=True,
        )


def initialize(directory: Path) -> None:
    target = private_directory(directory)
    _secure_new_directory(target)
    config = {APP_ID_KEY: APP_ID, SECRET_KEY: "", SIGNING_KEY: secrets.token_urlsafe(48)}
    with (target / "server-config.json").open("x", encoding="utf-8") as stream:
        json.dump(config, stream, indent=2)
    if os.name != "nt":
        (target / "server-config.json").chmod(0o600)


def _valid_secret(value: str, minimum: int = 1) -> bool:
    return minimum <= len(value) <= 256 and all(33 <= ord(c) <= 126 for c in value)


def load_config(directory: Path) -> dict[str, str]:
    try:
        path = private_directory(directory) / "server-config.json"
        if path.is_symlink() or path.stat().st_size > 4096:
            raise ConfigurationError("Invalid private configuration file.")
        config = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise ConfigurationError("Private configuration is absent or unreadable.") from None
    if not isinstance(config, dict) or set(config) != KEYS or not all(
        isinstance(value, str) for value in config.values()
    ):
        raise ConfigurationError("Invalid private configuration fields.")
    if config[APP_ID_KEY] != APP_ID or not _valid_secret(config[SIGNING_KEY], 32):
        raise ConfigurationError("AppID or session signing configuration is invalid.")
    if config[SECRET_KEY] and not _valid_secret(config[SECRET_KEY]):
        raise ConfigurationError("AppSecret format is invalid.")
    return config


def configure_secret(directory: Path) -> None:
    target = private_directory(directory)
    config = load_config(target)
    # getpass can fall back to echoed stdin. Refuse that fallback explicitly.
    if not sys.stdin.isatty():
        raise ConfigurationError("Open an interactive terminal to enter AppSecret.")
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        try:
            value = getpass.getpass("WeChat AppSecret (hidden): ")
            confirmation = getpass.getpass("Repeat AppSecret (hidden): ")
        except getpass.GetPassWarning:
            raise ConfigurationError("Hidden input is unavailable in this terminal.") from None
    if not _valid_secret(value) or value != confirmation:
        raise ConfigurationError("AppSecret entries are invalid or do not match; unchanged.")
    config[SECRET_KEY] = value
    # The replacement inherits the dedicated directory's restricted ACL.
    descriptor, temporary = tempfile.mkstemp(prefix="config-", suffix=".tmp", dir=target)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(config, stream, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target / "server-config.json")
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def check(directory: Path) -> dict[str, object]:
    config = load_config(directory)
    project = json.loads((PROJECT_ROOT / "miniprogram/project.config.json").read_text("utf-8"))
    privacy = (PROJECT_ROOT / "miniprogram/src/services/privacy.ts").read_text("utf-8")
    api = (PROJECT_ROOT / "miniprogram/src/config.ts").read_text("utf-8")
    return {
        "app_id": APP_ID,
        "project_app_id_matches": project.get("appid") == config[APP_ID_KEY],
        "url_check_enabled": project.get("setting", {}).get("urlCheck") is True,
        "app_secret_present": bool(config[SECRET_KEY]),
        "session_signing_key_valid": True,
        "privacy_approval_gate_empty": "const APPROVED_PRIVACY_APP_ID = '';" in privacy,
        "https_api_origin_empty": "const HTTPS_API_ORIGIN = '';" in api,
        "wechat_credentials_verified": False,
        "wechat_platform_acceptance": "DEFERRED",
    }


def serve(directory: Path) -> int:
    config = load_config(directory)
    report = check(directory)
    if not report["project_app_id_matches"] or not config[SECRET_KEY]:
        raise ConfigurationError("Matching AppID and a locally entered AppSecret are required.")
    # This helper is deliberately limited to the existing local SQLite database.
    # Do not migrate, create a missing database, or use an inherited DATABASE_URL.
    database = PROJECT_ROOT / "saas_mvp.db"
    if not database.is_file():
        raise ConfigurationError("Existing local database is required; no database was created.")
    environment = dict(os.environ)
    environment.update(config)
    environment["DATABASE_URL"] = "sqlite:///" + database.as_posix()
    for name in ("POSTGRES_TEST_DATABASE_URL", "POSTGRES_TEST_ALLOW_RESET"):
        environment.pop(name, None)
    # No reload/worker process or secret in command-line arguments. The app's
    # lifespan checks schema readiness; this command performs no migration.
    return subprocess.run(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
         "--port", "8000", "--no-access-log"],
        cwd=PROJECT_ROOT, env=environment,
    ).returncode


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", required=True, type=Path)
    parser.add_argument("action", choices=("initialize", "configure-secret", "check", "serve"))
    args = parser.parse_args(argv)
    try:
        if args.action == "initialize":
            initialize(args.directory)
            print("Private configuration initialized; AppSecret still required.")
        elif args.action == "configure-secret":
            configure_secret(args.directory)
            print("AppSecret saved locally. No WeChat verification has been performed.")
        elif args.action == "check":
            print(json.dumps(check(args.directory), indent=2))
        else:
            return serve(args.directory)
        return 0
    except ConfigurationError as error:
        print(str(error), file=sys.stderr)
    except (OSError, ValueError, subprocess.SubprocessError, EOFError):
        print("Local configuration operation failed; private values are not displayed.", file=sys.stderr)
    except KeyboardInterrupt:
        print("Cancelled.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
