import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from app.migration_baseline import ALEMBIC_CONFIG_PATH
from app.schema_readiness import CURRENT_SCHEMA_REVISION


PREVIOUS_REVISION = "0010_order_refund_recovery"


def build_config(database_url: str) -> Config:
    config = Config(str(ALEMBIC_CONFIG_PATH))
    config.attributes["database_url"] = database_url
    return config


def insert_batch(
    connection,
    *,
    batch_id: int,
    status: str = "PENDING_CONFIRMATION",
    period_start: str = "2026-09-01 00:00:00",
    period_end: str = "2026-09-08 00:00:00",
    generated_at: str = "2026-09-08 01:00:00",
    order_count: int = 1,
    item_count: int = 1,
    total_quantity: int = 1,
    confirmed_by_admin_id=None,
    confirmed_at=None,
) -> None:
    connection.execute(
        text(
            "INSERT INTO supplier_settlement_batches "
            "(id, settlement_public_id, supplier_id, "
            "supplier_public_id_snapshot, supplier_name_snapshot, "
            "period_start, period_end, status, order_count, item_count, "
            "total_quantity, total_cost_amount, generated_by_admin_id, "
            "generated_at, confirmed_by_admin_id, confirmed_at, "
            "created_at, updated_at) VALUES "
            "(:id, :public_id, 1, 'SUP-001', '测试供应商', "
            ":period_start, :period_end, :status, :order_count, "
            ":item_count, :total_quantity, 8.50, 1, :generated_at, "
            ":confirmed_by, :confirmed_at, :generated_at, :generated_at)"
        ),
        {
            "id": batch_id,
            "public_id": f"SETTLEMENT-{batch_id}",
            "period_start": period_start,
            "period_end": period_end,
            "status": status,
            "order_count": order_count,
            "item_count": item_count,
            "total_quantity": total_quantity,
            "generated_at": generated_at,
            "confirmed_by": confirmed_by_admin_id,
            "confirmed_at": confirmed_at,
        },
    )


def insert_item(
    connection,
    *,
    item_id: int,
    order_item_id: int,
    quantity: int = 1,
    line_cost_amount: str = "8.50",
) -> None:
    connection.execute(
        text(
            "INSERT INTO supplier_settlement_items "
            "(id, settlement_batch_id, supplier_id, order_id, "
            "order_item_id, order_public_id_snapshot, order_completed_at, "
            "product_public_id_snapshot, product_name_snapshot, "
            "sku_code_snapshot, sku_name_snapshot, "
            "supplier_public_id_snapshot, supplier_name_snapshot, "
            "supplier_sku_code_snapshot, unit_cost_price, quantity, "
            "line_cost_amount, created_at) VALUES "
            "(:id, 1, 1, 1, :order_item_id, 'ORDER-001', "
            "'2026-09-07 12:00:00', 'PRODUCT-001', '测试商品', "
            "'SKU-001', '默认规格', 'SUP-001', '测试供应商', "
            "'SUP-SKU-001', 8.50, :quantity, :line_cost_amount, "
            "'2026-09-08 01:00:00')"
        ),
        {
            "id": item_id,
            "order_item_id": order_item_id,
            "quantity": quantity,
            "line_cost_amount": line_cost_amount,
        },
    )


class SupplierSettlementMigrationTests(unittest.TestCase):
    def test_upgrade_creates_settlement_tables_and_is_reversible(self):
        with TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "settlement.db"
            url = f"sqlite:///{path.as_posix()}"
            config = build_config(url)
            command.upgrade(config, PREVIOUS_REVISION)
            command.upgrade(config, "head")
            command.check(config)

            engine = create_engine(url)
            try:
                inspector = inspect(engine)
                self.assertTrue(
                    {
                        "supplier_settlement_batches",
                        "supplier_settlement_items",
                    }
                    <= set(inspector.get_table_names())
                )
                batch_columns = {
                    column["name"]
                    for column in inspector.get_columns(
                        "supplier_settlement_batches"
                    )
                }
                self.assertTrue(
                    {
                        "settlement_public_id", "supplier_id", "status",
                        "total_cost_amount", "generated_by_admin_id",
                        "confirmed_by_admin_id", "confirmed_at",
                    }
                    <= batch_columns
                )
                item_indexes = {
                    index["name"]: index
                    for index in inspector.get_indexes(
                        "supplier_settlement_items"
                    )
                }
                self.assertTrue(
                    item_indexes["ix_supplier_settlement_items_order_item_id"][
                        "unique"
                    ]
                )
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
                tables = set(inspect(engine).get_table_names())
                self.assertNotIn("supplier_settlement_batches", tables)
                self.assertNotIn("supplier_settlement_items", tables)
            finally:
                engine.dispose()

    def test_batch_constraints_reject_invalid_state_or_totals(self):
        with TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "batch-constraints.db"
            url = f"sqlite:///{path.as_posix()}"
            command.upgrade(build_config(url), "head")
            engine = create_engine(url)
            try:
                invalid_batches = (
                    {"batch_id": 1, "status": "DRAFT"},
                    {
                        "batch_id": 2,
                        "period_start": "2026-09-08 00:00:00",
                        "period_end": "2026-09-01 00:00:00",
                    },
                    {
                        "batch_id": 3,
                        "order_count": 2,
                        "item_count": 1,
                    },
                    {
                        "batch_id": 4,
                        "status": "CONFIRMED",
                    },
                    {
                        "batch_id": 5,
                        "confirmed_by_admin_id": 1,
                        "confirmed_at": "2026-09-08 02:00:00",
                    },
                )
                for batch in invalid_batches:
                    with self.subTest(batch=batch):
                        with self.assertRaises(Exception):
                            with engine.begin() as connection:
                                insert_batch(connection, **batch)

                with engine.begin() as connection:
                    insert_batch(connection, batch_id=6)
                    insert_batch(
                        connection,
                        batch_id=7,
                        status="CONFIRMED",
                        confirmed_by_admin_id=2,
                        confirmed_at="2026-09-08 02:00:00",
                    )
            finally:
                engine.dispose()

    def test_item_cost_snapshot_is_arithmetic_and_not_repeatable(self):
        with TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "item-constraints.db"
            url = f"sqlite:///{path.as_posix()}"
            command.upgrade(build_config(url), "head")
            engine = create_engine(url)
            try:
                with engine.begin() as connection:
                    insert_batch(connection, batch_id=1)
                    insert_item(connection, item_id=1, order_item_id=10)

                with self.assertRaises(Exception):
                    with engine.begin() as connection:
                        insert_item(
                            connection,
                            item_id=2,
                            order_item_id=11,
                            quantity=2,
                            line_cost_amount="8.50",
                        )

                with self.assertRaises(Exception):
                    with engine.begin() as connection:
                        insert_item(connection, item_id=3, order_item_id=10)
            finally:
                engine.dispose()

    def test_downgrade_refuses_to_discard_settlement_evidence(self):
        with TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "downgrade.db"
            url = f"sqlite:///{path.as_posix()}"
            config = build_config(url)
            command.upgrade(config, "head")
            engine = create_engine(url)
            try:
                with engine.begin() as connection:
                    insert_batch(connection, batch_id=1)
            finally:
                engine.dispose()

            with self.assertRaisesRegex(RuntimeError, "供应商结算"):
                command.downgrade(config, PREVIOUS_REVISION)

            engine = create_engine(url)
            try:
                with engine.begin() as connection:
                    connection.execute(text(
                        "DELETE FROM supplier_settlement_batches"
                    ))
            finally:
                engine.dispose()
            command.downgrade(config, PREVIOUS_REVISION)


if __name__ == "__main__":
    unittest.main()
