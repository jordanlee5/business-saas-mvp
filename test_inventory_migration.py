import unittest
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from app.migration_baseline import ALEMBIC_CONFIG_PATH
from app.schema_readiness import CURRENT_SCHEMA_REVISION


PREVIOUS_REVISION = "0005_product_media"


def build_config(database_url: str) -> Config:
    config = Config(str(ALEMBIC_CONFIG_PATH))
    config.attributes["database_url"] = database_url
    return config


class InventoryMigrationTests(unittest.TestCase):
    def test_upgrade_creates_portable_inventory_schema_and_round_trip(self):
        with TemporaryDirectory() as temporary_directory:
            database_path = Path(temporary_directory) / "inventory.db"
            database_url = f"sqlite:///{database_path.as_posix()}"
            config = build_config(database_url)
            command.upgrade(config, PREVIOUS_REVISION)
            command.upgrade(config, "head")
            command.check(config)

            engine = create_engine(database_url)
            try:
                inspector = inspect(engine)
                tables = set(inspector.get_table_names())
                self.assertTrue(
                    {"inventory_balances", "inventory_movements"}
                    <= tables
                )
                balance_columns = {
                    column["name"]
                    for column in inspector.get_columns("inventory_balances")
                }
                self.assertTrue(
                    {
                        "sku_id",
                        "on_hand_quantity",
                        "reserved_quantity",
                        "version",
                    }
                    <= balance_columns
                )
                movement_columns = {
                    column["name"]
                    for column in inspector.get_columns("inventory_movements")
                }
                self.assertTrue(
                    {
                        "movement_public_id",
                        "sku_id",
                        "movement_type",
                        "quantity_delta",
                        "quantity_before",
                        "quantity_after",
                        "balance_version",
                        "idempotency_key",
                        "reason",
                        "actor_admin_id",
                    }
                    <= movement_columns
                )
                movement_targets = {
                    foreign_key["referred_table"]
                    for foreign_key in inspector.get_foreign_keys(
                        "inventory_movements"
                    )
                }
                self.assertEqual(
                    movement_targets,
                    {"product_skus", "users"},
                )
                balance_indexes = {
                    index["name"]: index
                    for index in inspector.get_indexes("inventory_balances")
                }
                self.assertTrue(
                    balance_indexes["ix_inventory_balances_sku_id"]["unique"]
                )
                with engine.connect() as connection:
                    revision = connection.execute(
                        text("SELECT version_num FROM alembic_version")
                    ).scalar_one()
                self.assertEqual(revision, CURRENT_SCHEMA_REVISION)
            finally:
                engine.dispose()

            command.downgrade(config, PREVIOUS_REVISION)
            engine = create_engine(database_url)
            try:
                tables = set(inspect(engine).get_table_names())
                self.assertNotIn("inventory_balances", tables)
                self.assertNotIn("inventory_movements", tables)
            finally:
                engine.dispose()

    def test_constraints_reject_invalid_balances_and_movements(self):
        with TemporaryDirectory() as temporary_directory:
            database_path = Path(temporary_directory) / "constraints.db"
            database_url = f"sqlite:///{database_path.as_posix()}"
            command.upgrade(build_config(database_url), "head")
            engine = create_engine(database_url)
            now = datetime(2026, 9, 11, 12, 0, 0)
            try:
                with engine.begin() as connection:
                    connection.execute(text(
                        "INSERT INTO users (id, username, password_hash, role, "
                        "is_active, must_change_password, service_rate_mode, "
                        "upstream_cost_rate_mode) VALUES "
                        "(1, 'operator', 'x', 'admin', 1, 0, 'external', 'external')"
                    ))
                    connection.execute(text(
                        "INSERT INTO product_categories "
                        "(id, name, slug, created_at, updated_at) VALUES "
                        "(1, '分类', 'category', :now, :now)"
                    ), {"now": now})
                    connection.execute(text(
                        "INSERT INTO suppliers "
                        "(id, supplier_public_id, name, created_at, updated_at) "
                        "VALUES (1, 'SUP-TEST', '供应商', :now, :now)"
                    ), {"now": now})
                    connection.execute(text(
                        "INSERT INTO products "
                        "(id, product_public_id, category_id, name, status, "
                        "created_at, updated_at) VALUES "
                        "(1, 'PRD-TEST', 1, '商品', 'DRAFT', :now, :now)"
                    ), {"now": now})
                    connection.execute(text(
                        "INSERT INTO product_skus "
                        "(id, product_id, supplier_id, sku_code, name, "
                        "points_price, cost_price, created_at, updated_at) VALUES "
                        "(1, 1, 1, 'SKU-TEST', '规格', 10, 5, :now, :now)"
                    ), {"now": now})
                    connection.execute(text(
                        "INSERT INTO inventory_balances "
                        "(id, sku_id, on_hand_quantity, reserved_quantity, "
                        "version, created_at, updated_at) VALUES "
                        "(1, 1, 5, 0, 1, :now, :now)"
                    ), {"now": now})
                    connection.execute(text(
                        "INSERT INTO inventory_movements "
                        "(id, movement_public_id, sku_id, movement_type, "
                        "quantity_delta, quantity_before, quantity_after, "
                        "balance_version, idempotency_key, reason, "
                        "actor_admin_id, created_at) VALUES "
                        "(1, 'IMV-VALID', 1, 'RECEIPT', 5, 0, 5, 1, "
                        "'valid-key', '首次入库', 1, :now)"
                    ), {"now": now})

                invalid_statements = (
                    (
                        "negative balance",
                        "UPDATE inventory_balances SET on_hand_quantity = -1 "
                        "WHERE id = 1",
                    ),
                    (
                        "reserved exceeds on hand",
                        "UPDATE inventory_balances SET reserved_quantity = 6 "
                        "WHERE id = 1",
                    ),
                    (
                        "zero movement",
                        "INSERT INTO inventory_movements "
                        "(movement_public_id, sku_id, movement_type, "
                        "quantity_delta, quantity_before, quantity_after, "
                        "balance_version, idempotency_key, reason, "
                        "actor_admin_id, created_at) VALUES "
                        "('IMV-ZERO', 1, 'ADJUSTMENT', 0, 5, 5, 2, "
                        "'zero-key', '零调整', 1, :now)",
                    ),
                    (
                        "broken arithmetic",
                        "INSERT INTO inventory_movements "
                        "(movement_public_id, sku_id, movement_type, "
                        "quantity_delta, quantity_before, quantity_after, "
                        "balance_version, idempotency_key, reason, "
                        "actor_admin_id, created_at) VALUES "
                        "('IMV-BROKEN', 1, 'ADJUSTMENT', 2, 5, 9, 2, "
                        "'broken-key', '错误算术', 1, :now)",
                    ),
                )
                for label, statement in invalid_statements:
                    with self.subTest(label=label):
                        with self.assertRaises(IntegrityError):
                            with engine.begin() as connection:
                                connection.execute(text(statement), {"now": now})
            finally:
                engine.dispose()


if __name__ == "__main__":
    unittest.main()
