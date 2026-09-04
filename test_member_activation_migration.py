import unittest
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory

from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from app.migration_baseline import ALEMBIC_CONFIG_PATH
from app.schema_readiness import CURRENT_SCHEMA_REVISION


PREVIOUS_REVISION = "0002_mall_core_foundation"
ACTIVATION_TABLE = "member_activation_credentials"


def build_config(database_url: str) -> Config:
    config = Config(str(ALEMBIC_CONFIG_PATH))
    config.attributes["database_url"] = database_url
    return config


def current_revision(database_url: str) -> str | None:
    engine = create_engine(database_url)
    try:
        with engine.connect() as connection:
            return MigrationContext.configure(
                connection
            ).get_current_revision()
    finally:
        engine.dispose()


class MemberActivationMigrationTests(unittest.TestCase):
    def test_upgrade_constraints_and_round_trip(self):
        with TemporaryDirectory() as temporary_directory:
            database_path = Path(temporary_directory) / "activation.db"
            database_url = f"sqlite:///{database_path.as_posix()}"
            config = build_config(database_url)
            command.upgrade(config, PREVIOUS_REVISION)
            command.upgrade(config, "head")
            command.check(config)

            self.assertEqual(
                current_revision(database_url),
                CURRENT_SCHEMA_REVISION,
            )
            engine = create_engine(database_url)
            try:
                inspector = inspect(engine)
                self.assertIn(ACTIVATION_TABLE, inspector.get_table_names())
                columns = {
                    column["name"]
                    for column in inspector.get_columns(ACTIVATION_TABLE)
                }
                self.assertTrue(
                    {
                        "business_record_id",
                        "security_method",
                        "secret_algorithm",
                        "secret_iterations",
                        "secret_salt",
                        "secret_digest",
                        "failed_attempts",
                        "max_attempts",
                        "issue_version",
                        "status",
                        "issued_at",
                        "expires_at",
                    }.issubset(columns)
                )
                indexes = {
                    index["name"]: index
                    for index in inspector.get_indexes(ACTIVATION_TABLE)
                }
                self.assertTrue(
                    indexes[
                        "ix_member_activation_credentials_business_record_id"
                    ]["unique"]
                )
            finally:
                engine.dispose()

            command.downgrade(config, PREVIOUS_REVISION)
            engine = create_engine(database_url)
            try:
                self.assertNotIn(
                    ACTIVATION_TABLE,
                    inspect(engine).get_table_names(),
                )
            finally:
                engine.dispose()
            command.upgrade(config, "head")
            self.assertEqual(
                current_revision(database_url),
                CURRENT_SCHEMA_REVISION,
            )

    def test_database_rejects_invalid_credential_state(self):
        with TemporaryDirectory() as temporary_directory:
            database_path = Path(temporary_directory) / "constraints.db"
            database_url = f"sqlite:///{database_path.as_posix()}"
            command.upgrade(build_config(database_url), "head")
            engine = create_engine(database_url)
            issued_at = datetime(2026, 9, 3, 18, 0, 0)
            expires_at = datetime(2026, 9, 30, 23, 59, 59)
            try:
                with engine.begin() as connection:
                    connection.execute(
                        text(
                            "INSERT INTO users "
                            "(id, username, password_hash, role) VALUES "
                            "(1, 'partner', 'test', 'partner')"
                        )
                    )
                    connection.execute(
                        text(
                            "INSERT INTO upload_batches "
                            "(id, user_id, filename, acceptance_status, "
                            "redemption_mode, claim_deadline) VALUES "
                            "(1, 1, 'mall.xlsx', '已承接', "
                            "'MALL_REDEMPTION', :expires_at)"
                        ),
                        {"expires_at": expires_at},
                    )
                    connection.execute(
                        text(
                            "INSERT INTO business_records "
                            "(id, user_id, batch_id, business_no, "
                            "public_business_no, phone, plate_number, "
                            "points_amount, redemption_mode, claim_status) "
                            "VALUES (1, 1, 1, '1', 'BR-ACT-1', "
                            "'13800000000', '桂A00001', 100, "
                            "'MALL_REDEMPTION', 'PENDING_ACTIVATION')"
                        )
                    )

                base_values = {
                    "business_record_id": 1,
                    "security_method": "ONE_TIME_CODE",
                    "secret_algorithm": "PBKDF2_SHA256",
                    "secret_iterations": 600000,
                    "secret_salt": "a" * 32,
                    "secret_digest": "b" * 64,
                    "failed_attempts": 0,
                    "max_attempts": 5,
                    "issue_version": 1,
                    "status": "ACTIVE",
                    "issued_at": issued_at,
                    "expires_at": expires_at,
                    "updated_at": issued_at,
                }
                for field, invalid_value in (
                    ("security_method", "EMAIL_LINK"),
                    ("status", "UNKNOWN"),
                    ("status", "LOCKED"),
                    ("failed_attempts", 6),
                    ("max_attempts", 0),
                    ("secret_iterations", 99999),
                    ("secret_iterations", 2000001),
                    ("secret_salt", "short"),
                ):
                    with self.subTest(field=field):
                        values = dict(base_values)
                        values[field] = invalid_value
                        columns = ", ".join(values)
                        parameters = ", ".join(
                            f":{name}" for name in values
                        )
                        with self.assertRaises(IntegrityError):
                            with engine.begin() as connection:
                                connection.execute(
                                    text(
                                        f"INSERT INTO {ACTIVATION_TABLE} "
                                        f"({columns}) VALUES ({parameters})"
                                    ),
                                    values,
                                )
            finally:
                engine.dispose()


if __name__ == "__main__":
    unittest.main()
