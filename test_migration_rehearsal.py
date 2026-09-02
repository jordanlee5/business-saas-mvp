import sqlite3
import unittest
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine

from app import models as application_models
from app.migration_baseline import (
    BASELINE_REVISION,
    BaselineAdoptionError,
    validate_and_adopt_baseline,
)
from app.migration_rehearsal import (
    MigrationRehearsalError,
    build_sqlite_database_url,
    get_business_fingerprint,
    rehearse_baseline_adoption,
)


def create_current_database(database_path: Path) -> None:
    database_url = build_sqlite_database_url(database_path)
    config = Config(
        str(Path(__file__).resolve().parent / "alembic.ini")
    )
    with patch.dict(
        os.environ,
        {"DATABASE_URL": database_url},
    ):
        command.upgrade(config, BASELINE_REVISION)

    engine = create_engine(database_url)
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "DROP TABLE alembic_version"
            )
            connection.execute(
                application_models.User.__table__.insert().values(
                    username="rehearsal-marker",
                    password_hash="test-only",
                    role="admin",
                )
            )
    finally:
        engine.dispose()


def get_current_revision(database_path: Path) -> str | None:
    engine = create_engine(
        build_sqlite_database_url(database_path)
    )
    try:
        with engine.connect() as connection:
            return MigrationContext.configure(
                connection
            ).get_current_revision()
    finally:
        engine.dispose()


def count_marker_users(database_path: Path) -> int:
    connection = sqlite3.connect(database_path)
    try:
        return connection.execute(
            "SELECT COUNT(*) FROM users "
            "WHERE username = 'rehearsal-marker'"
        ).fetchone()[0]
    finally:
        connection.close()


class MigrationRehearsalTests(unittest.TestCase):
    def test_rehearsal_stamps_only_copy_and_preserves_source(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "directory with spaces"
            root.mkdir()
            source_path = root / "source.db"
            backup_directory = root / "backups"
            create_current_database(source_path)
            source_bytes_before = source_path.read_bytes()

            result = rehearse_baseline_adoption(
                build_sqlite_database_url(source_path),
                backup_directory=backup_directory,
            )

            self.assertEqual(
                source_path.read_bytes(),
                source_bytes_before,
            )
            self.assertIsNone(get_current_revision(source_path))
            self.assertIsNone(
                get_current_revision(result.backup_database)
            )
            self.assertEqual(
                get_current_revision(result.rehearsal_database),
                BASELINE_REVISION,
            )
            self.assertEqual(
                count_marker_users(source_path),
                1,
            )
            self.assertEqual(
                count_marker_users(result.backup_database),
                1,
            )
            self.assertEqual(
                count_marker_users(result.rehearsal_database),
                1,
            )
            self.assertEqual(
                get_business_fingerprint(source_path),
                result.business_fingerprint,
            )
            self.assertEqual(
                get_business_fingerprint(
                    result.rehearsal_database
                ),
                result.business_fingerprint,
            )
            self.assertFalse(result.source_was_already_adopted)
            self.assertTrue(
                result.backup_database.parent.samefile(
                    backup_directory
                )
            )
            self.assertNotEqual(
                result.backup_database,
                result.rehearsal_database,
            )

    def test_partial_schema_fails_without_modifying_source(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source_path = root / "partial.db"
            backup_directory = root / "backups"
            engine = create_engine(
                build_sqlite_database_url(source_path)
            )
            try:
                application_models.User.__table__.create(
                    bind=engine
                )
            finally:
                engine.dispose()
            source_bytes_before = source_path.read_bytes()

            with self.assertRaises(BaselineAdoptionError):
                rehearse_baseline_adoption(
                    build_sqlite_database_url(source_path),
                    backup_directory=backup_directory,
                )

            self.assertEqual(
                source_path.read_bytes(),
                source_bytes_before,
            )
            self.assertIsNone(get_current_revision(source_path))
            for database_path in backup_directory.glob("*.db"):
                self.assertIsNone(
                    get_current_revision(database_path)
                )

    def test_rehearsal_is_repeatable_after_source_adoption(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source_path = root / "adopted.db"
            backup_directory = root / "backups"
            create_current_database(source_path)
            validate_and_adopt_baseline(
                build_sqlite_database_url(source_path),
                apply=True,
            )
            source_bytes_before = source_path.read_bytes()

            result = rehearse_baseline_adoption(
                build_sqlite_database_url(source_path),
                backup_directory=backup_directory,
            )

            self.assertEqual(
                source_path.read_bytes(),
                source_bytes_before,
            )
            self.assertTrue(result.source_was_already_adopted)
            self.assertEqual(
                get_current_revision(result.backup_database),
                BASELINE_REVISION,
            )
            self.assertEqual(
                get_current_revision(result.rehearsal_database),
                BASELINE_REVISION,
            )
            self.assertEqual(
                get_business_fingerprint(source_path),
                result.business_fingerprint,
            )

    def test_non_sqlite_database_is_rejected_before_output(self):
        with TemporaryDirectory() as temporary_directory:
            backup_directory = (
                Path(temporary_directory) / "backups"
            )

            with self.assertRaisesRegex(
                MigrationRehearsalError,
                "只支持本地 SQLite",
            ):
                rehearse_baseline_adoption(
                    "postgresql://example.invalid/test",
                    backup_directory=backup_directory,
                )

            self.assertFalse(backup_directory.exists())

    def test_missing_database_is_rejected_before_output(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source_path = root / "missing.db"
            backup_directory = root / "backups"

            with self.assertRaisesRegex(
                MigrationRehearsalError,
                "未找到源数据库文件",
            ):
                rehearse_baseline_adoption(
                    build_sqlite_database_url(source_path),
                    backup_directory=backup_directory,
                )

            self.assertFalse(source_path.exists())
            self.assertFalse(backup_directory.exists())


if __name__ == "__main__":
    unittest.main()
