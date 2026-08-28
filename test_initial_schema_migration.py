import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import Boolean, Integer, create_engine, inspect, text
from sqlalchemy.schema import DefaultClause

from app import models as application_models  # noqa: F401
from app.database import Base
from app.migration_baseline import (
    ALEMBIC_CONFIG_PATH,
    BASELINE_REVISION,
    BaselineAdoptionError,
    is_compatible_legacy_schema_difference,
    normalize_server_default,
    validate_and_adopt_baseline,
)


def build_sqlite_url(database_path: Path) -> str:
    return f"sqlite:///{database_path.as_posix()}"


def build_alembic_config(database_url: str) -> Config:
    config = Config(str(ALEMBIC_CONFIG_PATH))
    config.attributes["database_url"] = database_url
    return config


def get_current_revision(database_url: str) -> str | None:
    engine = create_engine(database_url)
    try:
        with engine.connect() as connection:
            return MigrationContext.configure(
                connection
            ).get_current_revision()
    finally:
        engine.dispose()


def get_application_table_names(
    database_url: str,
) -> set[str]:
    engine = create_engine(database_url)
    try:
        return set(inspect(engine).get_table_names()) - {
            "alembic_version"
        }
    finally:
        engine.dispose()


def get_foreign_key_targets(
    database_url: str,
    table_name: str,
) -> set[tuple[tuple[str, ...], str, tuple[str, ...]]]:
    engine = create_engine(database_url)
    try:
        foreign_keys = inspect(engine).get_foreign_keys(
            table_name
        )
        return {
            (
                tuple(foreign_key["constrained_columns"]),
                foreign_key["referred_table"],
                tuple(foreign_key["referred_columns"]),
            )
            for foreign_key in foreign_keys
        }
    finally:
        engine.dispose()


def create_current_schema(database_url: str) -> None:
    engine = create_engine(database_url)
    try:
        Base.metadata.create_all(bind=engine)
    finally:
        engine.dispose()


def insert_marker_user(database_url: str) -> None:
    engine = create_engine(database_url)
    try:
        with engine.begin() as connection:
            connection.execute(
                application_models.User.__table__.insert().values(
                    username="baseline-marker",
                    password_hash="test-only",
                    role="admin",
                )
            )
    finally:
        engine.dispose()


def count_marker_users(database_url: str) -> int:
    engine = create_engine(database_url)
    try:
        with engine.connect() as connection:
            return connection.execute(
                text(
                    "SELECT COUNT(*) FROM users "
                    "WHERE username = 'baseline-marker'"
                )
            ).scalar_one()
    finally:
        engine.dispose()


class InitialSchemaMigrationTests(unittest.TestCase):
    def test_empty_database_upgrade_downgrade_is_repeatable(self):
        with TemporaryDirectory() as temporary_directory:
            database_url = build_sqlite_url(
                Path(temporary_directory) / "empty.db"
            )
            config = build_alembic_config(database_url)

            command.upgrade(config, "head")
            command.check(config)
            self.assertEqual(
                get_current_revision(database_url),
                BASELINE_REVISION,
            )
            self.assertEqual(
                get_application_table_names(database_url),
                set(Base.metadata.tables),
            )

            command.downgrade(config, "base")
            self.assertEqual(
                get_application_table_names(database_url),
                set(),
            )

            command.upgrade(config, "head")
            self.assertEqual(
                get_current_revision(database_url),
                BASELINE_REVISION,
            )

    def test_voucher_batch_foreign_key_targets_voucher_batches(self):
        model_targets = {
            foreign_key.target_fullname
            for foreign_key in (
                application_models.VoucherRecord
                .__table__
                .c.batch_id
                .foreign_keys
            )
        }
        self.assertEqual(
            model_targets,
            {"voucher_upload_batches.id"},
        )

        with TemporaryDirectory() as temporary_directory:
            database_url = build_sqlite_url(
                Path(temporary_directory) / "foreign-key.db"
            )
            command.upgrade(
                build_alembic_config(database_url),
                "head",
            )

            targets = get_foreign_key_targets(
                database_url,
                "voucher_records",
            )

            self.assertIn(
                (
                    ("batch_id",),
                    "voucher_upload_batches",
                    ("id",),
                ),
                targets,
            )
            self.assertNotIn(
                (
                    ("batch_id",),
                    "upload_batches",
                    ("id",),
                ),
                targets,
            )

    def test_known_legacy_missing_indexes_are_compatible(self):
        with TemporaryDirectory() as temporary_directory:
            database_url = build_sqlite_url(
                Path(temporary_directory) / "legacy-indexes.db"
            )
            create_current_schema(database_url)
            insert_marker_user(database_url)
            engine = create_engine(database_url)
            try:
                with engine.begin() as connection:
                    connection.execute(
                        text(
                            "DROP INDEX "
                            "ix_business_records_business_no"
                        )
                    )
                    connection.execute(
                        text(
                            "DROP INDEX "
                            "ix_voucher_records_batch_id"
                        )
                    )
                    connection.execute(
                        text(
                            "DROP INDEX "
                            "ix_voucher_records_file_hash"
                        )
                    )
            finally:
                engine.dispose()

            result = validate_and_adopt_baseline(database_url)

            self.assertFalse(result.applied)
            self.assertFalse(result.already_adopted)
            self.assertIsNone(
                get_current_revision(database_url)
            )

    def test_legacy_profile_requires_exact_type_and_default(self):
        self.assertEqual(
            normalize_server_default(
                DefaultClause(text("(CURRENT_TIMESTAMP)"))
            ),
            "CURRENT_TIMESTAMP",
        )
        self.assertEqual(
            normalize_server_default(
                DefaultClause(text("((0.0))"))
            ),
            "0.0",
        )
        self.assertEqual(
            normalize_server_default(
                DefaultClause(text("(0) + (1)"))
            ),
            "(0) + (1)",
        )
        self.assertTrue(
            is_compatible_legacy_schema_difference(
                (
                    "modify_type",
                    None,
                    "users",
                    "is_active",
                    {},
                    Integer(),
                    Boolean(),
                )
            )
        )
        self.assertFalse(
            is_compatible_legacy_schema_difference(
                (
                    "modify_type",
                    None,
                    "users",
                    "service_rate",
                    {},
                    Integer(),
                    Boolean(),
                )
            )
        )
        self.assertTrue(
            is_compatible_legacy_schema_difference(
                (
                    "modify_default",
                    None,
                    "upload_batches",
                    "acceptance_status",
                    {},
                    DefaultClause(text("'已承接'")),
                    None,
                )
            )
        )
        self.assertFalse(
            is_compatible_legacy_schema_difference(
                (
                    "modify_default",
                    None,
                    "upload_batches",
                    "acceptance_status",
                    {},
                    DefaultClause(text("'待承接'")),
                    None,
                )
            )
        )
        self.assertTrue(
            is_compatible_legacy_schema_difference(
                (
                    "modify_default",
                    None,
                    "admin_action_logs",
                    "created_at",
                    {},
                    DefaultClause(text("(CURRENT_TIMESTAMP)")),
                    None,
                )
            )
        )
        self.assertTrue(
            is_compatible_legacy_schema_difference(
                (
                    "modify_default",
                    None,
                    "voucher_records",
                    "voucher_amount",
                    {},
                    DefaultClause(text("((0.0))")),
                    None,
                )
            )
        )
        self.assertFalse(
            is_compatible_legacy_schema_difference(
                (
                    "modify_default",
                    None,
                    "voucher_records",
                    "voucher_amount",
                    {},
                    DefaultClause(text("(1.0)")),
                    None,
                )
            )
        )

        voucher_batch_constraint = next(
            constraint
            for constraint in (
                application_models.VoucherRecord
                .__table__
                .foreign_key_constraints
            )
            if tuple(constraint.columns.keys()) == ("batch_id",)
        )
        self.assertTrue(
            is_compatible_legacy_schema_difference(
                ("add_fk", voucher_batch_constraint)
            )
        )

    def test_unknown_missing_index_is_rejected(self):
        with TemporaryDirectory() as temporary_directory:
            database_url = build_sqlite_url(
                Path(temporary_directory) / "unknown-index.db"
            )
            create_current_schema(database_url)
            engine = create_engine(database_url)
            try:
                with engine.begin() as connection:
                    connection.execute(
                        text("DROP INDEX ix_business_records_phone")
                    )
            finally:
                engine.dispose()

            with self.assertRaisesRegex(
                BaselineAdoptionError,
                "数据库结构与初始基线不一致",
            ):
                validate_and_adopt_baseline(database_url)

            self.assertIsNone(
                get_current_revision(database_url)
            )

    def test_business_batch_id_collision_cannot_mask_orphan(self):
        with TemporaryDirectory() as temporary_directory:
            database_url = build_sqlite_url(
                Path(temporary_directory) / "batch-collision.db"
            )
            create_current_schema(database_url)
            engine = create_engine(database_url)
            try:
                with engine.begin() as connection:
                    connection.execute(
                        application_models.User.__table__.insert().values(
                            id=1,
                            username="collision-marker",
                            password_hash="test-only",
                            role="admin",
                        )
                    )
                    connection.execute(
                        application_models.UploadBatch
                        .__table__
                        .insert()
                        .values(
                            id=36,
                            user_id=1,
                            filename="business.xlsx",
                        )
                    )
                    connection.execute(
                        application_models.VoucherRecord
                        .__table__
                        .insert()
                        .values(
                            id=65,
                            uploader_id=1,
                            batch_id=36,
                            filename="voucher.png",
                        )
                    )
            finally:
                engine.dispose()

            with self.assertRaisesRegex(
                BaselineAdoptionError,
                "voucher_batch_orphans=1",
            ):
                validate_and_adopt_baseline(
                    database_url,
                    apply=True,
                )

            self.assertIsNone(
                get_current_revision(database_url)
            )

    def test_existing_schema_check_is_read_only(self):
        with TemporaryDirectory() as temporary_directory:
            database_url = build_sqlite_url(
                Path(temporary_directory) / "check-only.db"
            )
            create_current_schema(database_url)
            insert_marker_user(database_url)

            result = validate_and_adopt_baseline(database_url)

            self.assertFalse(result.applied)
            self.assertFalse(result.already_adopted)
            self.assertIsNone(
                get_current_revision(database_url)
            )
            self.assertEqual(
                count_marker_users(database_url),
                1,
            )

    def test_apply_stamps_schema_without_changing_business_data(self):
        with TemporaryDirectory() as temporary_directory:
            database_url = build_sqlite_url(
                Path(temporary_directory) / "adopt.db"
            )
            create_current_schema(database_url)
            insert_marker_user(database_url)
            tables_before = get_application_table_names(
                database_url
            )

            first_result = validate_and_adopt_baseline(
                database_url,
                apply=True,
            )
            second_result = validate_and_adopt_baseline(
                database_url,
                apply=True,
            )

            self.assertTrue(first_result.applied)
            self.assertFalse(first_result.already_adopted)
            self.assertFalse(second_result.applied)
            self.assertTrue(second_result.already_adopted)
            self.assertEqual(
                get_current_revision(database_url),
                BASELINE_REVISION,
            )
            self.assertEqual(
                get_application_table_names(database_url),
                tables_before,
            )
            self.assertEqual(
                count_marker_users(database_url),
                1,
            )

    def test_partial_schema_is_rejected_without_stamp(self):
        with TemporaryDirectory() as temporary_directory:
            database_url = build_sqlite_url(
                Path(temporary_directory) / "partial.db"
            )
            engine = create_engine(database_url)
            try:
                application_models.User.__table__.create(
                    bind=engine
                )
            finally:
                engine.dispose()

            with self.assertRaises(BaselineAdoptionError):
                validate_and_adopt_baseline(
                    database_url,
                    apply=True,
                )

            self.assertIsNone(
                get_current_revision(database_url)
            )

    def test_unknown_existing_revision_is_rejected(self):
        with TemporaryDirectory() as temporary_directory:
            database_url = build_sqlite_url(
                Path(temporary_directory) / "unknown.db"
            )
            create_current_schema(database_url)
            engine = create_engine(database_url)
            try:
                with engine.begin() as connection:
                    connection.execute(
                        text(
                            "CREATE TABLE alembic_version ("
                            "version_num VARCHAR(32) NOT NULL)"
                        )
                    )
                    connection.execute(
                        text(
                            "INSERT INTO alembic_version "
                            "(version_num) VALUES ('unexpected')"
                        )
                    )
            finally:
                engine.dispose()

            with self.assertRaises(BaselineAdoptionError):
                validate_and_adopt_baseline(
                    database_url,
                    apply=True,
                )

            self.assertEqual(
                get_current_revision(database_url),
                "unexpected",
            )


if __name__ == "__main__":
    unittest.main()
