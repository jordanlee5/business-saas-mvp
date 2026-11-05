"""Prepare a dedicated empty staging store, or run its single application worker."""
import argparse
import os
from pathlib import Path
import secrets
import sys

from alembic import command
from alembic.config import Config
from sqlalchemy import select

from app.auth import verify_password
from app.database import create_database_engine
from app.models import User
from app.schema_readiness import assert_database_schema_ready

STORE = Path("/data")
DATABASE_URL = "sqlite:////data/staging.db"
ROOT = Path(__file__).resolve().parents[1]


def initialize():
    if os.environ.get("DATABASE_URL") != DATABASE_URL:
        raise ValueError("Staging DATABASE_URL must point to the dedicated /data/staging.db.")
    STORE.mkdir(parents=True, exist_ok=True)
    # Exclusive creation also prevents concurrent initialization or reuse of an old database.
    with (STORE / "staging.db").open("xb"):
        pass
    with (STORE / "session.key").open("x", encoding="ascii") as stream:
        os.chmod(stream.name, 0o600)
        stream.write(secrets.token_urlsafe(48))
    config = Config(str(ROOT / "alembic.ini"))
    config.attributes["database_url"] = DATABASE_URL
    command.upgrade(config, "head")
    print("Empty staging store created. Run python -m app.init_admin before starting the service.")


def preflight(*, database_url=None):
    if database_url is None and (os.environ.get("DATABASE_URL") != DATABASE_URL or not (STORE / "staging.db").is_file()):
        raise ValueError("Dedicated staging database is missing or incorrectly configured.")
    key_path = STORE / "session.key"
    if key_path.is_symlink() or key_path.stat().st_size > 256:
        raise ValueError("Invalid staging session key file.")
    key = key_path.read_text(encoding="ascii")
    if not 32 <= len(key) <= 256 or any(ord(char) < 33 for char in key):
        raise ValueError("Invalid staging session key.")
    engine = create_database_engine(database_url or DATABASE_URL)
    try:
        assert_database_schema_ready(engine)
        with engine.connect() as connection:
            admins = connection.execute(select(User.password_hash).where(
                User.role == "admin", User.admin_level == "super_admin", User.is_active.is_(True)
            )).scalars().all()
        if not admins or any(not value or verify_password("admin123", value) for value in admins):
            raise ValueError("Create a super administrator with a private password before starting staging.")
    finally:
        engine.dispose()
    os.environ["MINIPROGRAM_SESSION_SIGNING_KEY"] = key


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("initialize", "check", "serve"))
    args = parser.parse_args(argv)
    try:
        if args.action == "initialize":
            initialize()
        else:
            preflight()
            if args.action == "check":
                print("Staging store, schema and administrator checks passed.")
            else:
                import uvicorn
                # Compose exposes only the gateway. Forwarded headers are trusted
                # on this dedicated container network, never on a published app port.
                uvicorn.run("app.main:app", host="0.0.0.0", port=8000, workers=1,
                            proxy_headers=True, forwarded_allow_ips="*", access_log=False)
        return 0
    except (OSError, ValueError):
        print("Staging preparation failed. Check the dedicated store, schema and administrator. Existing files were preserved.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
