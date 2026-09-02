import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text

from app.migration_baseline import (
    ALEMBIC_CONFIG_PATH,
    BASELINE_REVISION,
)
from app.migration_rehearsal import (
    build_sqlite_database_url,
    get_business_fingerprint,
    get_current_revision,
)
from app.migration_upgrade_rehearsal import (
    MigrationUpgradeRehearsalError,
    capture_legacy_snapshot,
    fingerprint_legacy_snapshot,
    rehearse_mall_core_upgrade,
)
from app.schema_readiness import CURRENT_SCHEMA_REVISION


def build_config(database_url: str) -> Config:
    config = Config(str(ALEMBIC_CONFIG_PATH))
    config.attributes["database_url"] = database_url
    return config


def create_baseline_database(database_path: Path) -> None:
    database_url = build_sqlite_database_url(database_path)
    command.upgrade(build_config(database_url), BASELINE_REVISION)
    engine = create_engine(database_url)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO users "
                    "(id, username, password_hash, role, admin_level, "
                    "is_active, must_change_password, service_rate, "
                    "upstream_cost_rate, service_rate_mode, "
                    "upstream_cost_rate_mode, created_at) VALUES "
                    "(1, 'rehearsal-admin', 'test-only', 'admin', "
                    "'super_admin', 1, 0, 6.5, 3.2, 'external', "
                    "'internal', '2026-09-02 08:00:00')"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO upload_batches "
                    "(id, user_id, filename, total_rows, success_rows, "
                    "failed_rows, acceptance_status, created_at) VALUES "
                    "(10, 1, 'history.xlsx', 1, 1, 0, '已承接', "
                    "'2026-09-02 08:01:00')"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO business_records "
                    "(id, user_id, batch_id, business_no, "
                    "public_business_no, name, phone, plate_number, "
                    "points_amount, bank_card, record_service_rate, "
                    "record_upstream_cost_rate, "
                    "record_service_rate_mode, "
                    "record_upstream_cost_rate_mode, created_at) VALUES "
                    "(20, 1, 10, '8', 'BR-REHEARSAL-0001', '历史客户', "
                    "'13800000000', '京A00001', 100.25, "
                    "'6222000000000001', 6.5, 3.2, 'external', "
                    "'internal', '2026-09-02 08:02:00')"
                )
            )
    finally:
        engine.dispose()


class MigrationUpgradeRehearsalTests(unittest.TestCase):
    def test_rehearsal_upgrades_only_copy_and_preserves_legacy_data(
        self,
    ):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "directory with spaces"
            root.mkdir()
            source_path = root / "source.db"
            backup_directory = root / "backups"
            create_baseline_database(source_path)
            source_fingerprint = get_business_fingerprint(source_path)
            source_snapshot = capture_legacy_snapshot(source_path)

            result = rehearse_mall_core_upgrade(
                build_sqlite_database_url(source_path),
                backup_directory=backup_directory,
            )

            self.assertEqual(
                get_current_revision(source_path),
                BASELINE_REVISION,
            )
            self.assertEqual(
                get_current_revision(result.backup_database),
                BASELINE_REVISION,
            )
            self.assertEqual(
                get_current_revision(result.rehearsal_database),
                CURRENT_SCHEMA_REVISION,
            )
            self.assertEqual(
                get_business_fingerprint(source_path),
                source_fingerprint,
            )
            self.assertEqual(
                capture_legacy_snapshot(result.backup_database),
                source_snapshot,
            )
            self.assertEqual(
                result.legacy_data_fingerprint,
                fingerprint_legacy_snapshot(source_snapshot),
            )
            self.assertNotEqual(
                result.backup_database,
                result.rehearsal_database,
            )

    def test_unversioned_database_is_rejected_without_output(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source_path = root / "unversioned.db"
            backup_directory = root / "backups"
            source_path.touch()

            with self.assertRaisesRegex(
                MigrationUpgradeRehearsalError,
                "要求源库位于初始基线",
            ):
                rehearse_mall_core_upgrade(
                    build_sqlite_database_url(source_path),
                    backup_directory=backup_directory,
                )

            self.assertFalse(backup_directory.exists())

    def test_already_upgraded_database_is_rejected_without_output(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source_path = root / "head.db"
            backup_directory = root / "backups"
            command.upgrade(
                build_config(build_sqlite_database_url(source_path)),
                "head",
            )

            with self.assertRaisesRegex(
                MigrationUpgradeRehearsalError,
                BASELINE_REVISION,
            ):
                rehearse_mall_core_upgrade(
                    build_sqlite_database_url(source_path),
                    backup_directory=backup_directory,
                )

            self.assertFalse(backup_directory.exists())

    def test_drifted_baseline_is_rejected_without_output(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source_path = root / "drifted.db"
            backup_directory = root / "backups"
            create_baseline_database(source_path)
            engine = create_engine(
                build_sqlite_database_url(source_path)
            )
            try:
                with engine.begin() as connection:
                    connection.execute(
                        text("DROP INDEX ix_business_records_phone")
                    )
            finally:
                engine.dispose()

            with self.assertRaisesRegex(
                MigrationUpgradeRehearsalError,
                "未通过 0001 结构与数据完整性预检",
            ):
                rehearse_mall_core_upgrade(
                    build_sqlite_database_url(source_path),
                    backup_directory=backup_directory,
                )

            self.assertFalse(backup_directory.exists())

    def test_approved_legacy_missing_index_remains_compatible(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source_path = root / "compatible-legacy.db"
            backup_directory = root / "backups"
            create_baseline_database(source_path)
            engine = create_engine(
                build_sqlite_database_url(source_path)
            )
            try:
                with engine.begin() as connection:
                    connection.execute(
                        text(
                            "DROP INDEX "
                            "ix_business_records_business_no"
                        )
                    )
            finally:
                engine.dispose()

            result = rehearse_mall_core_upgrade(
                build_sqlite_database_url(source_path),
                backup_directory=backup_directory,
            )

            self.assertEqual(
                get_current_revision(result.rehearsal_database),
                CURRENT_SCHEMA_REVISION,
            )


if __name__ == "__main__":
    unittest.main()
