import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from app.migration_baseline import ALEMBIC_CONFIG_PATH
from app.schema_readiness import CURRENT_SCHEMA_REVISION


PREVIOUS_REVISION = "0007_order_foundation"


def build_config(database_url: str) -> Config:
    config = Config(str(ALEMBIC_CONFIG_PATH))
    config.attributes["database_url"] = database_url
    return config


class OrderReservationMigrationTests(unittest.TestCase):
    def test_upgrade_adds_idempotency_and_reserved_stock_evidence(self):
        with TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "order-reservation.db"
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
                movement_columns = {
                    column["name"]
                    for column in inspector.get_columns("inventory_movements")
                }
                self.assertIn("idempotency_key", order_columns)
                item_columns = {
                    column["name"]
                    for column in inspector.get_columns("order_items")
                }
                self.assertIn("product_image_path_snapshot", item_columns)
                self.assertTrue(
                    {
                        "reserved_quantity_delta",
                        "reserved_quantity_before",
                        "reserved_quantity_after",
                        "actor_member_id",
                        "reference_type",
                        "reference_id",
                    }
                    <= movement_columns
                )
                with engine.connect() as connection:
                    revision = connection.execute(
                        text("SELECT version_num FROM alembic_version")
                    ).scalar_one()
                self.assertEqual(revision, CURRENT_SCHEMA_REVISION)
            finally:
                engine.dispose()

            command.downgrade(config, PREVIOUS_REVISION)
            engine = create_engine(url)
            try:
                inspector = inspect(engine)
                order_columns = {
                    column["name"]
                    for column in inspector.get_columns("orders")
                }
                movement_columns = {
                    column["name"]
                    for column in inspector.get_columns("inventory_movements")
                }
                self.assertNotIn("idempotency_key", order_columns)
                self.assertNotIn("reserved_quantity_delta", movement_columns)
            finally:
                engine.dispose()

    def test_existing_order_gets_stable_legacy_idempotency_key(self):
        with TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "legacy-order.db"
            url = f"sqlite:///{path.as_posix()}"
            config = build_config(url)
            command.upgrade(config, PREVIOUS_REVISION)
            engine = create_engine(url)
            try:
                with engine.begin() as connection:
                    connection.execute(text(
                        "INSERT INTO orders "
                        "(id, order_public_id, member_id, status, total_points, "
                        "total_cost_amount, total_quantity, created_at, updated_at) "
                        "VALUES (41, 'ORD-LEGACY-41', 1, 'CREATED', 10, 2, 1, "
                        "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                    ))
            finally:
                engine.dispose()

            command.upgrade(config, "head")
            engine = create_engine(url)
            try:
                with engine.connect() as connection:
                    key = connection.execute(text(
                        "SELECT idempotency_key FROM orders WHERE id = 41"
                    )).scalar_one()
                self.assertEqual(key, "legacy-order:41")
            finally:
                engine.dispose()

    def test_existing_physical_inventory_movement_is_preserved(self):
        with TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "legacy-inventory.db"
            url = f"sqlite:///{path.as_posix()}"
            config = build_config(url)
            command.upgrade(config, PREVIOUS_REVISION)
            engine = create_engine(url)
            try:
                with engine.begin() as connection:
                    connection.execute(text(
                        "INSERT INTO inventory_balances "
                        "(id, sku_id, on_hand_quantity, reserved_quantity, "
                        "version, created_at, updated_at) VALUES "
                        "(1, 1, 8, 0, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                    ))
                    connection.execute(text(
                        "INSERT INTO inventory_movements "
                        "(id, movement_public_id, sku_id, movement_type, "
                        "quantity_delta, quantity_before, quantity_after, "
                        "balance_version, idempotency_key, reason, "
                        "actor_admin_id, created_at) VALUES "
                        "(1, 'IMV-LEGACY-PHYSICAL', 1, 'RECEIPT', 8, 0, 8, "
                        "1, 'legacy-receipt', '历史入库', 1, CURRENT_TIMESTAMP)"
                    ))
            finally:
                engine.dispose()

            command.upgrade(config, "head")
            engine = create_engine(url)
            try:
                with engine.connect() as connection:
                    row = connection.execute(text(
                        "SELECT movement_type, quantity_delta, quantity_before, "
                        "quantity_after, reserved_quantity_delta, "
                        "reserved_quantity_before, reserved_quantity_after, "
                        "actor_admin_id, actor_member_id "
                        "FROM inventory_movements WHERE id = 1"
                    )).one()
                self.assertEqual(
                    tuple(row),
                    ("RECEIPT", 8, 0, 8, 0, 0, 0, 1, None),
                )
            finally:
                engine.dispose()

            command.downgrade(config, PREVIOUS_REVISION)
            engine = create_engine(url)
            try:
                with engine.connect() as connection:
                    row = connection.execute(text(
                        "SELECT movement_type, quantity_delta, quantity_before, "
                        "quantity_after, actor_admin_id "
                        "FROM inventory_movements WHERE id = 1"
                    )).one()
                self.assertEqual(tuple(row), ("RECEIPT", 8, 0, 8, 1))
            finally:
                engine.dispose()

    def test_constraints_reject_incomplete_reservation_movement(self):
        with TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "reservation-constraints.db"
            url = f"sqlite:///{path.as_posix()}"
            command.upgrade(build_config(url), "head")
            engine = create_engine(url)
            try:
                with self.assertRaises(Exception):
                    with engine.begin() as connection:
                        connection.execute(text(
                            "INSERT INTO inventory_movements "
                            "(movement_public_id, sku_id, movement_type, "
                            "quantity_delta, quantity_before, quantity_after, "
                            "reserved_quantity_delta, reserved_quantity_before, "
                            "reserved_quantity_after, balance_version, "
                            "idempotency_key, reason, actor_member_id, created_at) "
                            "VALUES ('IMV-BROKEN', 1, 'RESERVE', 0, 5, 5, "
                            "2, 0, 2, 1, 'broken', 'broken', 1, CURRENT_TIMESTAMP)"
                        ))
            finally:
                engine.dispose()


if __name__ == "__main__":
    unittest.main()
