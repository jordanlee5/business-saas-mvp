import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from app.migration_baseline import ALEMBIC_CONFIG_PATH
from app.schema_readiness import CURRENT_SCHEMA_REVISION


PREVIOUS_REVISION = "0009_order_shipping_completion"


def build_config(database_url: str) -> Config:
    config = Config(str(ALEMBIC_CONFIG_PATH))
    config.attributes["database_url"] = database_url
    return config


def insert_order(connection, *, order_id, status, extra_columns="", values=""):
    connection.execute(text(
        "INSERT INTO orders "
        "(id, order_public_id, idempotency_key, member_id, status, "
        "total_points, total_cost_amount, total_quantity, created_at, "
        f"updated_at{extra_columns}) VALUES "
        f"({order_id}, 'ORD-REFUND-{order_id}', "
        f"'order-refund-{order_id}', 1, '{status}', 10, 2, 1, "
        f"CURRENT_TIMESTAMP, CURRENT_TIMESTAMP{values})"
    ))


LIFECYCLE_COLUMNS = (
    ", shipping_carrier, tracking_number, shipped_at, completed_at"
)
LIFECYCLE_VALUES = (
    ", '顺丰速运', 'SF-REFUND', "
    "'2026-09-17 12:00:00', '2026-09-17 13:00:00'"
)


class OrderRefundMigrationTests(unittest.TestCase):
    def test_upgrade_adds_refund_evidence_and_is_reversible(self):
        with TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "order-refund.db"
            url = f"sqlite:///{path.as_posix()}"
            config = build_config(url)
            command.upgrade(config, PREVIOUS_REVISION)
            command.upgrade(config, "head")
            command.check(config)

            engine = create_engine(url)
            try:
                inspector = inspect(engine)
                columns = {
                    column["name"]
                    for column in inspector.get_columns("orders")
                }
                self.assertTrue(
                    {"refund_reason", "refunded_at"} <= columns
                )
                indexes = {
                    index["name"]
                    for index in inspector.get_indexes("orders")
                }
                self.assertIn("ix_orders_refunded_at", indexes)
                with engine.connect() as connection:
                    revision = connection.execute(text(
                        "SELECT version_num FROM alembic_version"
                    )).scalar_one()
                self.assertEqual(revision, CURRENT_SCHEMA_REVISION)
            finally:
                engine.dispose()

            command.downgrade(config, PREVIOUS_REVISION)
            engine = create_engine(url)
            try:
                columns = {
                    column["name"]
                    for column in inspect(engine).get_columns("orders")
                }
                self.assertNotIn("refund_reason", columns)
                self.assertNotIn("refunded_at", columns)
            finally:
                engine.dispose()

    def test_upgrade_rejects_legacy_refunded_status_without_evidence(self):
        with TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "legacy-refunded.db"
            url = f"sqlite:///{path.as_posix()}"
            config = build_config(url)
            command.upgrade(config, PREVIOUS_REVISION)
            engine = create_engine(url)
            try:
                with engine.begin() as connection:
                    insert_order(connection, order_id=1, status="REFUNDED")
            finally:
                engine.dispose()

            with self.assertRaisesRegex(RuntimeError, "缺少积分、库存"):
                command.upgrade(config, "head")
            engine = create_engine(url)
            try:
                with engine.connect() as connection:
                    revision = connection.execute(text(
                        "SELECT version_num FROM alembic_version"
                    )).scalar_one()
                self.assertEqual(revision, PREVIOUS_REVISION)
            finally:
                engine.dispose()

    def test_constraints_reject_incomplete_or_time_inverted_refund(self):
        with TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "refund-constraints.db"
            url = f"sqlite:///{path.as_posix()}"
            command.upgrade(build_config(url), "head")
            engine = create_engine(url)
            try:
                invalid_rows = (
                    (1, "REFUNDED", LIFECYCLE_COLUMNS, LIFECYCLE_VALUES),
                    (
                        2,
                        "COMPLETED",
                        LIFECYCLE_COLUMNS + ", refund_reason, refunded_at",
                        LIFECYCLE_VALUES
                        + ", '错误状态退款', '2026-09-17 14:00:00'",
                    ),
                    (
                        3,
                        "REFUNDED",
                        LIFECYCLE_COLUMNS + ", refund_reason, refunded_at",
                        LIFECYCLE_VALUES
                        + ", '', '2026-09-17 14:00:00'",
                    ),
                    (
                        4,
                        "REFUNDED",
                        LIFECYCLE_COLUMNS + ", refund_reason, refunded_at",
                        LIFECYCLE_VALUES
                        + ", '时间倒置', '2026-09-17 12:30:00'",
                    ),
                )
                for order_id, status, extra_columns, values in invalid_rows:
                    with self.subTest(order_id=order_id):
                        with self.assertRaises(Exception):
                            with engine.begin() as connection:
                                insert_order(
                                    connection,
                                    order_id=order_id,
                                    status=status,
                                    extra_columns=extra_columns,
                                    values=values,
                                )

                with engine.begin() as connection:
                    insert_order(
                        connection,
                        order_id=5,
                        status="REFUNDED",
                        extra_columns=(
                            LIFECYCLE_COLUMNS
                            + ", refund_reason, refunded_at"
                        ),
                        values=(
                            LIFECYCLE_VALUES
                            + ", '客户整单退货', '2026-09-17 14:00:00'"
                        ),
                    )
            finally:
                engine.dispose()

    def test_downgrade_refuses_to_discard_refund_evidence(self):
        with TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "refund-downgrade.db"
            url = f"sqlite:///{path.as_posix()}"
            config = build_config(url)
            command.upgrade(config, "head")
            engine = create_engine(url)
            try:
                with engine.begin() as connection:
                    insert_order(
                        connection,
                        order_id=1,
                        status="REFUNDED",
                        extra_columns=(
                            LIFECYCLE_COLUMNS
                            + ", refund_reason, refunded_at"
                        ),
                        values=(
                            LIFECYCLE_VALUES
                            + ", '客户整单退货', '2026-09-17 14:00:00'"
                        ),
                    )
            finally:
                engine.dispose()

            with self.assertRaisesRegex(RuntimeError, "退款及资源恢复证据"):
                command.downgrade(config, PREVIOUS_REVISION)

            engine = create_engine(url)
            try:
                with engine.begin() as connection:
                    connection.execute(text(
                        "UPDATE orders SET status = 'COMPLETED', "
                        "refund_reason = NULL, refunded_at = NULL "
                        "WHERE id = 1"
                    ))
            finally:
                engine.dispose()
            command.downgrade(config, PREVIOUS_REVISION)


if __name__ == "__main__":
    unittest.main()
