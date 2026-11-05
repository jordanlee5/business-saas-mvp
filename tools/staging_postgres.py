"""Dedicated PostgreSQL test deployment. Never accepts an existing application URL."""
import argparse
import os
from pathlib import Path
import secrets
import sys

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import URL
from sqlalchemy.exc import SQLAlchemyError

STORE = Path("/data")
SECRET = Path("/run/secrets/staging_app_password")
DATABASE = "saas_staging"
USER = "saas_staging"
ROOT = Path(__file__).resolve().parents[1]


def configure():
    if os.environ.get("DATABASE_URL", "").strip() or "app.database" in sys.modules:
        raise ValueError("Use a fresh staging process without DATABASE_URL.")
    password = SECRET.read_text(encoding="ascii").strip()
    if SECRET.is_symlink() or not 32 <= len(password) <= 128 or not password.isalnum():
        raise ValueError("Invalid staging database secret.")
    url = URL.create("postgresql+psycopg", username=USER, password=password,
                     host="database", port=5432, database=DATABASE).render_as_string(hide_password=False)
    os.environ["DATABASE_URL"] = url
    return url


def assert_identity(connection):
    identity = connection.execute(text("select current_database(),current_user")).one()
    if tuple(identity) != (DATABASE, USER):
        raise ValueError("Wrong staging database identity.")
    if connection.execute(text("select rolsuper from pg_roles where rolname=current_user")).scalar_one():
        raise ValueError("The application role must not be a superuser.")


def initialize(url):
    from alembic import command
    from alembic.config import Config
    engine = create_engine(url)
    try:
        with engine.begin() as connection:
            assert_identity(connection)
            connection.execute(text("select pg_advisory_xact_lock(73190511)"))
            if inspect(connection).get_table_names():
                raise ValueError("Initialization requires an empty staging database.")
            STORE.mkdir(parents=True, exist_ok=True)
            if any(STORE.iterdir()):
                # A Docker image creates an empty uploads directory on first mount.
                if any(p.name != "uploads" or not p.is_dir() or p.is_symlink() or any(p.iterdir())
                       for p in STORE.iterdir()):
                    raise ValueError("Initialization requires an empty staging data volume.")
            with (STORE / "session.key").open("x", encoding="ascii") as stream:
                os.chmod(stream.name, 0o600)
                stream.write(secrets.token_urlsafe(48))
            config = Config(str(ROOT / "alembic.ini"))
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
        print("Empty PostgreSQL staging store created. Create the administrator next.")
    finally:
        engine.dispose()


def preflight(url):
    engine = create_engine(url)
    try:
        with engine.connect() as connection:
            assert_identity(connection)
    finally:
        engine.dispose()
    from tools import staging_server
    staging_server.preflight(database_url=url)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("initialize", "admin", "check", "serve"))
    args = parser.parse_args(argv)
    try:
        url = configure()
        if args.action == "initialize":
            initialize(url)
        elif args.action == "admin":
            from app.init_admin import init_admin
            init_admin()
        else:
            preflight(url)
            if args.action == "serve":
                import uvicorn
                workers = int(os.environ.get("STAGING_WEB_WORKERS", "1"))
                if not 1 <= workers <= 4:
                    raise ValueError("Choose 1 to 4 PostgreSQL staging workers.")
                uvicorn.run("app.main:app", host="0.0.0.0", port=8000, workers=workers,
                            proxy_headers=True, forwarded_allow_ips="*", access_log=False)
            else:
                print("PostgreSQL staging identity, schema, key and administrator checks passed.")
        return 0
    except (OSError, ValueError, RuntimeError, SQLAlchemyError, EOFError):
        # No URL, password, SQL parameters or traceback in operator output.
        print("PostgreSQL staging preparation failed. Check the private secret, empty target, schema and administrator.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
