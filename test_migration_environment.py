import io
import os
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect

from app import models as application_models  # noqa: F401
from app.database import Base


PROJECT_ROOT = Path(__file__).resolve().parent
ALEMBIC_CONFIG_PATH = PROJECT_ROOT / "alembic.ini"


def build_alembic_config() -> Config:
    return Config(str(ALEMBIC_CONFIG_PATH))


def build_sqlite_url(database_path: Path) -> str:
    return f"sqlite:///{database_path.as_posix()}"


class MigrationEnvironmentTests(unittest.TestCase):
    def test_config_uses_project_relative_migration_directory(self):
        config = build_alembic_config()

        self.assertEqual(
            Path(config.get_main_option("script_location")).resolve(),
            PROJECT_ROOT / "migrations",
        )
        self.assertIsNone(
            config.get_main_option("sqlalchemy.url")
        )

    def test_upgrade_head_builds_current_schema_and_is_repeatable(self):
        with TemporaryDirectory() as temporary_directory:
            database_path = (
                Path(temporary_directory) / "migration.db"
            )
            database_url = build_sqlite_url(database_path)

            with patch.dict(
                os.environ,
                {"DATABASE_URL": database_url},
            ):
                command.upgrade(build_alembic_config(), "head")
                command.upgrade(build_alembic_config(), "head")

            engine = create_engine(database_url)
            try:
                table_names = set(
                    inspect(engine).get_table_names()
                )
            finally:
                engine.dispose()

            self.assertEqual(
                table_names - {"alembic_version"},
                set(Base.metadata.tables),
            )

    def test_autogenerate_sees_current_schema_as_unchanged(self):
        with TemporaryDirectory() as temporary_directory:
            database_path = (
                Path(temporary_directory) / "current-schema.db"
            )
            database_url = build_sqlite_url(database_path)
            engine = create_engine(database_url)
            try:
                Base.metadata.create_all(bind=engine)
            finally:
                engine.dispose()

            with patch.dict(
                os.environ,
                {"DATABASE_URL": database_url},
            ):
                with redirect_stdout(io.StringIO()):
                    command.stamp(
                        build_alembic_config(),
                        "head",
                    )
                    command.check(build_alembic_config())


if __name__ == "__main__":
    unittest.main()
