import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from app.migration_baseline import ALEMBIC_CONFIG_PATH
from app.schema_readiness import CURRENT_SCHEMA_REVISION


PREVIOUS_REVISION = "0008_order_reservation"


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
        f"({order_id}, 'ORD-LIFECYCLE-{order_id}', "
        f"'order-lifecycle-{order_id}', 1, '{status}', 10, 2, 1, "
        f"CURRENT_TIMESTAMP, CURRENT_TIMESTAMP{values})"
    ))


class OrderLifecycleMigrationTests(unittest.TestCase):
    def test_upgrade_adds_shipping_completion_evidence_and_is_reversible(self):
        with TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "order-lifecycle.db"
            url = f"sqlite:///{path.as_posix()}"
            config = build_config(url)
            command.upgrade(config, PREVIOUS_REVISION)
            command.upgrade(config, "head")
            command.check(config)

            engine = create_engine(url)
            try:
                inspector = inspect(engine)
                order_columns = {
                    column["name"]
                    for column in inspector.get_columns("orders")
                }
                self.assertTrue(
                    {
                        "shipping_carrier",
                        "tracking_number",
                        "shipped_at",
                        "completed_at",
                    }
                    <= order_columns
                )
                indexes = {
                    index["name"]
                    for index in inspector.get_indexes("orders")
                }
                self.assertTrue(
                    {
                        "ix_orders_tracking_number",
                        "ix_orders_shipped_at",
                        "ix_orders_completed_at",
                    }
                    <= indexes
                )
                unique_names = {
                    constraint["name"]
                    for constraint in inspector.get_unique_constraints(
                        "orders"
                    )
                }
                self.assertIn("uq_orders_carrier_tracking", unique_names)
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
                order_columns = {
                    column["name"]
                    for column in inspect(engine).get_columns("orders")
                }
                self.assertNotIn("shipping_carrier", order_columns)
                self.assertNotIn("completed_at", order_columns)
            finally:
                engine.dispose()

    def test_upgrade_rejects_legacy_status_without_shipping_evidence(self):
        with TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "legacy-shipped.db"
            url = f"sqlite:///{path.as_posix()}"
            config = build_config(url)
            command.upgrade(config, PREVIOUS_REVISION)
            engine = create_engine(url)
            try:
                with engine.begin() as connection:
                    insert_order(
                        connection,
                        order_id=1,
                        status="SHIPPED",
                    )
            finally:
                engine.dispose()

            with self.assertRaisesRegex(RuntimeError, "缺少物流证据"):
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

    def test_constraints_reject_partial_or_status_inconsistent_evidence(self):
        with TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "lifecycle-constraints.db"
            url = f"sqlite:///{path.as_posix()}"
            command.upgrade(build_config(url), "head")
            engine = create_engine(url)
            try:
                invalid_rows = (
                    (1, "SHIPPED", "", ""),
                    (
                        2,
                        "FULFILLING",
                        ", shipping_carrier",
                        ", '顺丰速运'",
                    ),
                    (
                        3,
                        "COMPLETED",
                        ", shipping_carrier, tracking_number, shipped_at",
                        ", '顺丰速运', 'SF-3', CURRENT_TIMESTAMP",
                    ),
                    (
                        5,
                        "FULFILLING",
                        ", shipping_carrier, tracking_number, shipped_at",
                        ", '顺丰速运', 'SF-5', CURRENT_TIMESTAMP",
                    ),
                    (
                        6,
                        "COMPLETED",
                        ", shipping_carrier, tracking_number, shipped_at, "
                        "completed_at",
                        ", '顺丰速运', 'SF-6', "
                        "'2026-09-17 12:00:00', '2026-09-17 11:59:59'",
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
                        order_id=4,
                        status="SHIPPED",
                        extra_columns=(
                            ", shipping_carrier, tracking_number, shipped_at"
                        ),
                        values=(
                            ", '顺丰速运', 'SF-4', CURRENT_TIMESTAMP"
                        ),
                    )
            finally:
                engine.dispose()

    def test_downgrade_refuses_to_discard_lifecycle_evidence(self):
        with TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "lifecycle-downgrade.db"
            url = f"sqlite:///{path.as_posix()}"
            config = build_config(url)
            command.upgrade(config, "head")
            engine = create_engine(url)
            try:
                with engine.begin() as connection:
                    insert_order(
                        connection,
                        order_id=1,
                        status="SHIPPED",
                        extra_columns=(
                            ", shipping_carrier, tracking_number, shipped_at"
                        ),
                        values=(
                            ", '顺丰速运', 'SF-DOWNGRADE', CURRENT_TIMESTAMP"
                        ),
                    )
            finally:
                engine.dispose()

            with self.assertRaisesRegex(RuntimeError, "发货或完成证据"):
                command.downgrade(config, PREVIOUS_REVISION)

            engine = create_engine(url)
            try:
                with engine.begin() as connection:
                    connection.execute(text(
                        "UPDATE orders SET status = 'FULFILLING', "
                        "shipping_carrier = NULL, tracking_number = NULL, "
                        "shipped_at = NULL WHERE id = 1"
                    ))
            finally:
                engine.dispose()
            command.downgrade(config, PREVIOUS_REVISION)


if __name__ == "__main__":
    unittest.main()
