import unittest
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from app import models as application_models
from app.database import Base
from app.migration_baseline import (
    ALEMBIC_CONFIG_PATH,
    BASELINE_REVISION,
)
from app.schema_readiness import CURRENT_SCHEMA_REVISION


MALL_CORE_REVISION = CURRENT_SCHEMA_REVISION
MALL_CORE_TABLES = {
    "members",
    "member_wechat_bindings",
    "points_accounts",
    "points_grants",
    "points_ledger_entries",
}


def build_sqlite_url(database_path: Path) -> str:
    return f"sqlite:///{database_path.as_posix()}"


def build_alembic_config(database_url: str) -> Config:
    config = Config(str(ALEMBIC_CONFIG_PATH))
    config.attributes["database_url"] = database_url
    return config


def get_revision(database_url: str) -> str | None:
    engine = create_engine(database_url)
    try:
        with engine.connect() as connection:
            return MigrationContext.configure(
                connection
            ).get_current_revision()
    finally:
        engine.dispose()


def create_baseline_with_historical_data(
    database_url: str,
) -> None:
    command.upgrade(
        build_alembic_config(database_url),
        BASELINE_REVISION,
    )
    engine = create_engine(database_url)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO users "
                    "(id, username, password_hash, role, "
                    "admin_level, is_active, "
                    "must_change_password, service_rate, "
                    "upstream_cost_rate, service_rate_mode, "
                    "upstream_cost_rate_mode, created_at) "
                    "VALUES "
                    "(1, 'history-admin', 'test-only', 'admin', "
                    "'super_admin', 1, 0, 6.5, 3.2, "
                    "'external', 'internal', "
                    "'2026-08-01 09:00:00')"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO upload_batches "
                    "(id, user_id, filename, total_rows, "
                    "success_rows, failed_rows, "
                    "acceptance_status, created_at) "
                    "VALUES "
                    "(10, 1, 'historical.xlsx', 1, 1, 0, "
                    "'已承接', '2026-08-01 10:00:00')"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO business_records "
                    "(id, user_id, batch_id, business_no, "
                    "public_business_no, name, phone, "
                    "plate_number, points_amount, bank_card, "
                    "record_service_rate, "
                    "record_upstream_cost_rate, "
                    "record_service_rate_mode, "
                    "record_upstream_cost_rate_mode, "
                    "created_at) VALUES "
                    "(20, 1, 10, '8', 'BR-HISTORY-0001', "
                    "'历史客户', '13800000000', '鲁A00001', "
                    "1234.56, '6222000000000001', 6.5, 3.2, "
                    "'external', 'internal', "
                    "'2026-08-01 10:01:00')"
                )
            )
    finally:
        engine.dispose()


def get_historical_snapshot(
    database_url: str,
) -> tuple[tuple[object, ...], tuple[object, ...]]:
    engine = create_engine(database_url)
    try:
        with engine.connect() as connection:
            batch = connection.execute(
                text(
                    "SELECT id, user_id, filename, total_rows, "
                    "success_rows, failed_rows, "
                    "acceptance_status, created_at "
                    "FROM upload_batches WHERE id = 10"
                )
            ).one()
            business = connection.execute(
                text(
                    "SELECT id, user_id, batch_id, business_no, "
                    "public_business_no, name, phone, "
                    "plate_number, points_amount, bank_card, "
                    "record_service_rate, "
                    "record_upstream_cost_rate, "
                    "record_service_rate_mode, "
                    "record_upstream_cost_rate_mode, created_at "
                    "FROM business_records WHERE id = 20"
                )
            ).one()
            return tuple(batch), tuple(business)
    finally:
        engine.dispose()


def insert_member_with_two_grants(
    database_url: str,
) -> None:
    engine = create_engine(database_url)
    activated_at = datetime(2026, 9, 2, 10, 0, 0)
    expires_at = datetime(2027, 9, 2, 10, 0, 0)
    try:
        with engine.begin() as connection:
            connection.execute(
                application_models.UploadBatch.__table__.insert(),
                {
                    "id": 11,
                    "user_id": 1,
                    "filename": "mall.xlsx",
                    "redemption_mode": "MALL_REDEMPTION",
                    "claim_deadline": datetime(2026, 12, 31),
                },
            )
            connection.execute(
                application_models.BusinessRecord.__table__.insert(),
                [
                    {
                        "id": 21,
                        "user_id": 1,
                        "batch_id": 11,
                        "business_no": "9",
                        "public_business_no": "BR-MALL-0001",
                        "points_amount": 100,
                        "redemption_mode": "MALL_REDEMPTION",
                        "claim_status": "ACTIVATED",
                    },
                    {
                        "id": 22,
                        "user_id": 1,
                        "batch_id": 11,
                        "business_no": "10",
                        "public_business_no": "BR-MALL-0002",
                        "points_amount": 200,
                        "redemption_mode": "MALL_REDEMPTION",
                        "claim_status": "ACTIVATED",
                    },
                ],
            )
            connection.execute(
                application_models.Member.__table__.insert().values(
                    id=1,
                    member_public_id="MEMBER-0001",
                )
            )
            connection.execute(
                application_models.PointsAccount
                .__table__
                .insert()
                .values(
                    id=1,
                    member_id=1,
                    available_points=Decimal("300.00"),
                )
            )
            connection.execute(
                application_models.PointsGrant.__table__.insert(),
                [
                    {
                        "id": 1,
                        "account_id": 1,
                        "business_record_id": 21,
                        "granted_points": Decimal("100.00"),
                        "available_points": Decimal("100.00"),
                        "activated_at": activated_at,
                        "expires_at": expires_at,
                    },
                    {
                        "id": 2,
                        "account_id": 1,
                        "business_record_id": 22,
                        "granted_points": Decimal("200.00"),
                        "available_points": Decimal("200.00"),
                        "activated_at": activated_at,
                        "expires_at": expires_at,
                    },
                ],
            )
    finally:
        engine.dispose()


class MallCoreMigrationTests(unittest.TestCase):
    def test_upgrade_preserves_history_and_backfills_cash_channel(
        self,
    ):
        with TemporaryDirectory() as temporary_directory:
            database_url = build_sqlite_url(
                Path(temporary_directory) / "history.db"
            )
            config = build_alembic_config(database_url)
            create_baseline_with_historical_data(database_url)
            snapshot_before = get_historical_snapshot(database_url)

            command.upgrade(config, "head")
            command.upgrade(config, "head")
            command.check(config)

            self.assertEqual(
                get_revision(database_url),
                MALL_CORE_REVISION,
            )
            self.assertEqual(
                get_historical_snapshot(database_url),
                snapshot_before,
            )

            engine = create_engine(database_url)
            try:
                inspector = inspect(engine)
                self.assertTrue(
                    MALL_CORE_TABLES.issubset(
                        inspector.get_table_names()
                    )
                )
                with engine.connect() as connection:
                    batch_channel = connection.execute(
                        text(
                            "SELECT redemption_mode, "
                            "claim_deadline FROM upload_batches "
                            "WHERE id = 10"
                        )
                    ).one()
                    business_channel = connection.execute(
                        text(
                            "SELECT redemption_mode, claim_status "
                            "FROM business_records WHERE id = 20"
                        )
                    ).one()
            finally:
                engine.dispose()

            self.assertEqual(
                tuple(batch_channel),
                ("CASH_REBATE", None),
            )
            self.assertEqual(
                tuple(business_channel),
                ("CASH_REBATE", None),
            )

    def test_downgrade_and_reupgrade_keep_historical_rows(self):
        with TemporaryDirectory() as temporary_directory:
            database_url = build_sqlite_url(
                Path(temporary_directory) / "round-trip.db"
            )
            config = build_alembic_config(database_url)
            create_baseline_with_historical_data(database_url)
            snapshot_before = get_historical_snapshot(database_url)
            command.upgrade(config, "head")

            command.downgrade(config, BASELINE_REVISION)

            self.assertEqual(
                get_revision(database_url),
                BASELINE_REVISION,
            )
            self.assertEqual(
                get_historical_snapshot(database_url),
                snapshot_before,
            )
            engine = create_engine(database_url)
            try:
                inspector = inspect(engine)
                self.assertFalse(
                    MALL_CORE_TABLES.intersection(
                        inspector.get_table_names()
                    )
                )
                self.assertNotIn(
                    "redemption_mode",
                    {
                        column["name"]
                        for column in inspector.get_columns(
                            "business_records"
                        )
                    },
                )
            finally:
                engine.dispose()

            command.upgrade(config, "head")
            self.assertEqual(
                get_historical_snapshot(database_url),
                snapshot_before,
            )

    def test_one_member_can_hold_multiple_points_grants(self):
        with TemporaryDirectory() as temporary_directory:
            database_url = build_sqlite_url(
                Path(temporary_directory) / "multiple-grants.db"
            )
            create_baseline_with_historical_data(database_url)
            command.upgrade(
                build_alembic_config(database_url),
                "head",
            )
            insert_member_with_two_grants(database_url)

            engine = create_engine(database_url)
            try:
                with engine.connect() as connection:
                    grant_count = connection.execute(
                        text(
                            "SELECT COUNT(*) FROM points_grants "
                            "WHERE account_id = 1"
                        )
                    ).scalar_one()
                self.assertEqual(grant_count, 2)

                with self.assertRaises(IntegrityError):
                    with engine.begin() as connection:
                        connection.execute(
                            application_models.PointsGrant
                            .__table__
                            .insert()
                            .values(
                                account_id=1,
                                business_record_id=21,
                                granted_points=Decimal("1.00"),
                                available_points=Decimal("1.00"),
                                activated_at=datetime(
                                    2026, 9, 2, 10, 0, 0
                                ),
                                expires_at=datetime(
                                    2027, 9, 2, 10, 0, 0
                                ),
                            )
                        )
            finally:
                engine.dispose()

    def test_channel_and_ledger_constraints_fail_closed(self):
        with TemporaryDirectory() as temporary_directory:
            database_url = build_sqlite_url(
                Path(temporary_directory) / "constraints.db"
            )
            create_baseline_with_historical_data(database_url)
            command.upgrade(
                build_alembic_config(database_url),
                "head",
            )
            insert_member_with_two_grants(database_url)
            engine = create_engine(database_url)
            try:
                with self.assertRaises(IntegrityError):
                    with engine.begin() as connection:
                        connection.execute(
                            text(
                                "UPDATE business_records "
                                "SET claim_status = 'ACTIVATED' "
                                "WHERE id = 20"
                            )
                        )

                with self.assertRaises(IntegrityError):
                    with engine.begin() as connection:
                        connection.execute(
                            text(
                                "UPDATE business_records "
                                "SET redemption_mode = "
                                "'MALL_REDEMPTION' "
                                "WHERE id = 20"
                            )
                        )

                with self.assertRaises(IntegrityError):
                    with engine.begin() as connection:
                        connection.execute(
                            application_models.PointsLedgerEntry
                            .__table__
                            .insert()
                            .values(
                                grant_id=1,
                                entry_type="GRANT",
                                idempotency_key="zero-delta",
                            )
                        )

                with self.assertRaises(IntegrityError):
                    with engine.begin() as connection:
                        connection.execute(
                            text(
                                "UPDATE points_grants "
                                "SET available_points = 90, "
                                "reserved_points = 20 "
                                "WHERE id = 1"
                            )
                        )

                with self.assertRaises(IntegrityError):
                    with engine.begin() as connection:
                        connection.execute(
                            application_models.PointsLedgerEntry
                            .__table__
                            .insert()
                            .values(
                                grant_id=1,
                                entry_type="ADJUST",
                                available_points_delta=Decimal("1.00"),
                                idempotency_key="missing-audit",
                            )
                        )

                with engine.begin() as connection:
                    connection.execute(
                        application_models.PointsLedgerEntry
                        .__table__
                        .insert()
                        .values(
                            grant_id=1,
                            entry_type="GRANT",
                            available_points_delta=Decimal("100.00"),
                            idempotency_key="grant-business-21",
                        )
                    )

                with self.assertRaises(IntegrityError):
                    with engine.begin() as connection:
                        connection.execute(
                            application_models.PointsLedgerEntry
                            .__table__
                            .insert()
                            .values(
                                grant_id=2,
                                entry_type="GRANT",
                                available_points_delta=Decimal("200.00"),
                                idempotency_key="grant-business-21",
                            )
                        )
            finally:
                engine.dispose()

    def test_model_metadata_contains_only_current_schema(self):
        expected_new_columns = {
            "upload_batches": {
                "redemption_mode",
                "claim_deadline",
            },
            "business_records": {
                "redemption_mode",
                "claim_status",
            },
        }

        self.assertTrue(
            MALL_CORE_TABLES.issubset(Base.metadata.tables)
        )
        for table_name, column_names in expected_new_columns.items():
            with self.subTest(table_name=table_name):
                self.assertTrue(
                    column_names.issubset(
                        Base.metadata.tables[table_name].columns.keys()
                    )
                )


if __name__ == "__main__":
    unittest.main()
