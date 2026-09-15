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


PREVIOUS_REVISION = "0006_inventory_foundation"


def build_config(database_url: str) -> Config:
    config = Config(str(ALEMBIC_CONFIG_PATH))
    config.attributes["database_url"] = database_url
    return config


class OrderFoundationMigrationTests(unittest.TestCase):
    def test_upgrade_creates_snapshot_schema_and_round_trip(self):
        with TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "orders.db"
            url = f"sqlite:///{path.as_posix()}"
            config = build_config(url)
            command.upgrade(config, PREVIOUS_REVISION)
            command.upgrade(config, "head")
            command.check(config)

            engine = create_engine(url)
            try:
                inspector = inspect(engine)
                self.assertTrue(
                    {"orders", "order_items", "order_points_grant_allocations"}
                    <= set(inspector.get_table_names())
                )
                item_columns = {
                    column["name"]
                    for column in inspector.get_columns("order_items")
                }
                self.assertTrue(
                    {
                        "product_public_id_snapshot",
                        "product_name_snapshot",
                        "sku_code_snapshot",
                        "sku_name_snapshot",
                        "supplier_public_id_snapshot",
                        "supplier_name_snapshot",
                        "supplier_sku_code_snapshot",
                        "unit_points_price",
                        "unit_cost_price",
                        "quantity",
                        "line_points",
                        "line_cost_amount",
                    }
                    <= item_columns
                )
                targets = {
                    foreign_key["referred_table"]
                    for foreign_key in inspector.get_foreign_keys("order_items")
                }
                self.assertEqual(
                    targets, {"orders", "products", "product_skus", "suppliers"}
                )
                allocation_targets = {
                    foreign_key["referred_table"]
                    for foreign_key in inspector.get_foreign_keys(
                        "order_points_grant_allocations"
                    )
                }
                self.assertEqual(allocation_targets, {"orders", "points_grants"})
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
                tables = set(inspect(engine).get_table_names())
                self.assertFalse(
                    {"orders", "order_items", "order_points_grant_allocations"}
                    & tables
                )
            finally:
                engine.dispose()

    def test_constraints_protect_totals_snapshots_and_allocations(self):
        with TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "constraints.db"
            url = f"sqlite:///{path.as_posix()}"
            command.upgrade(build_config(url), "head")
            engine = create_engine(url)
            now = datetime(2026, 9, 14, 12, 0, 0)
            invalid_statements = (
                "INSERT INTO orders (order_public_id, member_id, status, total_points, total_cost_amount, total_quantity, created_at, updated_at) VALUES ('ORD-X', 1, 'CREATED', 0, 1, 1, :now, :now)",
                "INSERT INTO order_items (order_id, product_id, sku_id, supplier_id, product_public_id_snapshot, product_name_snapshot, sku_code_snapshot, sku_name_snapshot, supplier_public_id_snapshot, supplier_name_snapshot, unit_points_price, unit_cost_price, quantity, line_points, line_cost_amount, created_at) VALUES (1, 1, 1, 1, 'PRD-X', '商品', 'SKU-X', '规格', 'SUP-X', '供应商', 10, 2, 2, 19, 4, :now)",
                "INSERT INTO order_points_grant_allocations (order_id, points_grant_id, allocated_points, created_at) VALUES (1, 1, 0, :now)",
            )
            try:
                for statement in invalid_statements:
                    with self.subTest(statement=statement):
                        with self.assertRaises(IntegrityError):
                            with engine.begin() as connection:
                                connection.execute(text(statement), {"now": now})
            finally:
                engine.dispose()


if __name__ == "__main__":
    unittest.main()
