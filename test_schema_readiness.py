import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, text

from app.migration_baseline import (
    ALEMBIC_CONFIG_PATH,
    BASELINE_REVISION,
)
from app.schema_readiness import (
    CURRENT_SCHEMA_REVISION,
    DatabaseSchemaNotReadyError,
    REQUIRED_APPLICATION_TABLES,
    assert_database_schema_ready,
)


def build_sqlite_url(database_path: Path) -> str:
    return f"sqlite:///{database_path.as_posix()}"


def upgrade(database_url: str, revision: str) -> None:
    config = Config(str(ALEMBIC_CONFIG_PATH))
    config.attributes["database_url"] = database_url
    command.upgrade(config, revision)


class SchemaReadinessTests(unittest.TestCase):
    def test_current_head_is_ready(self):
        with TemporaryDirectory() as temporary_directory:
            database_url = build_sqlite_url(
                Path(temporary_directory) / "ready.db"
            )
            upgrade(database_url, "head")
            engine = create_engine(database_url)
            try:
                result = assert_database_schema_ready(engine)
            finally:
                engine.dispose()

            self.assertEqual(
                result.revision,
                CURRENT_SCHEMA_REVISION,
            )
            self.assertEqual(
                result.checked_tables,
                REQUIRED_APPLICATION_TABLES,
            )

    def test_readiness_revision_matches_migration_head(self):
        config = Config(str(ALEMBIC_CONFIG_PATH))

        self.assertEqual(
            ScriptDirectory.from_config(config).get_current_head(),
            CURRENT_SCHEMA_REVISION,
        )

    def test_unversioned_database_is_rejected(self):
        with TemporaryDirectory() as temporary_directory:
            engine = create_engine(
                build_sqlite_url(
                    Path(temporary_directory) / "unversioned.db"
                )
            )
            try:
                with self.assertRaisesRegex(
                    DatabaseSchemaNotReadyError,
                    "尚未接入 Alembic",
                ):
                    assert_database_schema_ready(engine)
            finally:
                engine.dispose()

    def test_baseline_database_is_rejected_as_outdated(self):
        with TemporaryDirectory() as temporary_directory:
            database_url = build_sqlite_url(
                Path(temporary_directory) / "baseline.db"
            )
            upgrade(database_url, BASELINE_REVISION)
            engine = create_engine(database_url)
            try:
                with self.assertRaisesRegex(
                    DatabaseSchemaNotReadyError,
                    CURRENT_SCHEMA_REVISION,
                ):
                    assert_database_schema_ready(engine)
            finally:
                engine.dispose()

    def test_false_head_stamp_without_tables_is_rejected(self):
        with TemporaryDirectory() as temporary_directory:
            engine = create_engine(
                build_sqlite_url(
                    Path(temporary_directory) / "false-stamp.db"
                )
            )
            try:
                with engine.begin() as connection:
                    connection.execute(
                        text(
                            "CREATE TABLE alembic_version "
                            "(version_num VARCHAR(64) NOT NULL)"
                        )
                    )
                    connection.execute(
                        text(
                            "INSERT INTO alembic_version "
                            "(version_num) VALUES (:revision)"
                        ),
                        {"revision": CURRENT_SCHEMA_REVISION},
                    )

                with self.assertRaisesRegex(
                    DatabaseSchemaNotReadyError,
                    "缺少表",
                ):
                    assert_database_schema_ready(engine)
            finally:
                engine.dispose()


if __name__ == "__main__":
    unittest.main()
