import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from decimal import Decimal
from threading import Barrier

from alembic import command
from sqlalchemy.orm import sessionmaker

from app.admin_permissions import OPERATOR
from app.database import create_database_engine, resolve_database_url
from app.mall import execute_supplier_settlement_generation
from app.models import (
    AdminActionLog,
    Member,
    Order,
    OrderItem,
    Product,
    ProductCategory,
    ProductSku,
    Supplier,
    SupplierSettlementBatch,
    SupplierSettlementItem,
    User,
)
from test_postgresql_migration import (
    POSTGRES_TEST_ALLOW_RESET_ENV_NAME,
    POSTGRES_TEST_DATABASE_URL_ENV_NAME,
    build_alembic_config,
    get_database_state,
    validate_postgresql_test_database_url,
)


NOW = datetime(2026, 9, 21, 12, 0, 0)
PERIOD_START = datetime(2026, 9, 1, 0, 0, 0)
PERIOD_END = datetime(2026, 9, 8, 0, 0, 0)


class PostgreSQLSupplierSettlementIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        configured_url = os.environ.get(
            POSTGRES_TEST_DATABASE_URL_ENV_NAME,
            "",
        ).strip()
        if not configured_url:
            raise unittest.SkipTest("未配置独立 PostgreSQL 测试数据库")
        if os.environ.get(POSTGRES_TEST_ALLOW_RESET_ENV_NAME, "") != "1":
            raise RuntimeError(
                "运行 PostgreSQL 集成测试前必须显式设置 "
                "POSTGRES_TEST_ALLOW_RESET=1"
            )
        cls.database_url = validate_postgresql_test_database_url(
            configured_url,
            resolve_database_url(),
        )

    def test_concurrent_generation_keeps_one_batch_and_one_item(self):
        tables_before, revision_before = get_database_state(self.database_url)
        self.assertFalse(
            tables_before - {"alembic_version"},
            "PostgreSQL 集成测试只允许使用空测试数据库",
        )
        self.assertIsNone(
            revision_before,
            "PostgreSQL 集成测试库不得已有版本标记",
        )

        config = build_alembic_config(self.database_url)
        upgraded = False
        engine = None
        try:
            command.upgrade(config, "head")
            upgraded = True
            engine = create_database_engine(self.database_url)
            Session = sessionmaker(
                bind=engine,
                autoflush=False,
                expire_on_commit=False,
            )
            with Session() as db:
                operator = User(
                    username="pg-settlement-operator",
                    password_hash="test-only",
                    role="admin",
                    admin_level=OPERATOR,
                    is_active=True,
                )
                member = Member(
                    member_public_id="MEM-PG-SETTLEMENT-001",
                    is_active=True,
                    created_at=NOW,
                    updated_at=NOW,
                )
                category = ProductCategory(
                    name="PostgreSQL 结算分类",
                    slug="pg-settlement-test",
                    is_active=True,
                    created_at=NOW,
                    updated_at=NOW,
                )
                supplier = Supplier(
                    supplier_public_id="SUP-PG-SETTLEMENT-001",
                    name="PostgreSQL 结算供应商",
                    is_active=True,
                    created_at=NOW,
                    updated_at=NOW,
                )
                db.add_all([operator, member, category, supplier])
                db.flush()
                product = Product(
                    product_public_id="PRD-PG-SETTLEMENT-001",
                    category_id=category.id,
                    name="PostgreSQL 结算商品",
                    status="PUBLISHED",
                    published_at=NOW,
                    created_at=NOW,
                    updated_at=NOW,
                )
                db.add(product)
                db.flush()
                sku = ProductSku(
                    product_id=product.id,
                    supplier_id=supplier.id,
                    sku_code="PG-SETTLEMENT-SKU-001",
                    name="标准款",
                    points_price=Decimal("40.00"),
                    cost_price=Decimal("12.00"),
                    low_stock_threshold=1,
                    is_active=True,
                    created_at=NOW,
                    updated_at=NOW,
                )
                db.add(sku)
                db.flush()
                completed_at = PERIOD_START + timedelta(days=2)
                order = Order(
                    order_public_id="ORD-PG-SETTLEMENT-001",
                    idempotency_key="pg-settlement-order-001",
                    member_id=member.id,
                    status="COMPLETED",
                    total_points=Decimal("80.00"),
                    total_cost_amount=Decimal("24.00"),
                    total_quantity=2,
                    shipping_carrier="顺丰速运",
                    tracking_number="SF-PG-SETTLEMENT-001",
                    shipped_at=completed_at - timedelta(hours=1),
                    completed_at=completed_at,
                    created_at=PERIOD_START,
                    updated_at=completed_at,
                )
                db.add(order)
                db.flush()
                order_item = OrderItem(
                    order_id=order.id,
                    product_id=product.id,
                    sku_id=sku.id,
                    supplier_id=supplier.id,
                    product_public_id_snapshot=product.product_public_id,
                    product_name_snapshot=product.name,
                    sku_code_snapshot=sku.sku_code,
                    sku_name_snapshot=sku.name,
                    supplier_public_id_snapshot=supplier.supplier_public_id,
                    supplier_name_snapshot=supplier.name,
                    supplier_sku_code_snapshot="SUP-PG-SKU-001",
                    product_image_path_snapshot=None,
                    unit_points_price=Decimal("40.00"),
                    unit_cost_price=Decimal("12.00"),
                    quantity=2,
                    line_points=Decimal("80.00"),
                    line_cost_amount=Decimal("24.00"),
                    created_at=PERIOD_START,
                )
                db.add(order_item)
                db.commit()
                operator_id = operator.id
                supplier_id = supplier.id
                order_item_id = order_item.id

            barrier = Barrier(2)

            def generate_once():
                barrier.wait()
                try:
                    result = execute_supplier_settlement_generation(
                        engine,
                        actor_admin_id=operator_id,
                        supplier_id=supplier_id,
                        period_start=PERIOD_START,
                        period_end=PERIOD_END,
                        now=NOW,
                    )
                    return "success", result
                except Exception as exc:
                    return "rejected", exc

            with ThreadPoolExecutor(max_workers=2) as pool:
                outcomes = [
                    future.result()
                    for future in (
                        pool.submit(generate_once),
                        pool.submit(generate_once),
                    )
                ]

            self.assertEqual(
                sorted(outcome[0] for outcome in outcomes),
                ["rejected", "success"],
            )
            rejected = next(
                value for status, value in outcomes if status == "rejected"
            )
            self.assertIsInstance(rejected, ValueError)
            self.assertIn("没有未结算", str(rejected))

            with Session() as db:
                batch = db.query(SupplierSettlementBatch).one()
                item = db.query(SupplierSettlementItem).one()
                self.assertEqual(batch.status, "PENDING_CONFIRMATION")
                self.assertEqual(batch.order_count, 1)
                self.assertEqual(batch.item_count, 1)
                self.assertEqual(batch.total_quantity, 2)
                self.assertEqual(batch.total_cost_amount, Decimal("24.00"))
                self.assertEqual(item.order_item_id, order_item_id)
                self.assertEqual(
                    db.query(AdminActionLog).filter_by(
                        action_type="mall_supplier_settlement_generate"
                    ).count(),
                    1,
                )
        finally:
            if engine is not None:
                try:
                    with engine.begin() as connection:
                        connection.execute(
                            AdminActionLog.__table__.delete().where(
                                AdminActionLog.action_type
                                == "mall_supplier_settlement_generate"
                            )
                        )
                        connection.execute(
                            SupplierSettlementItem.__table__.delete()
                        )
                        connection.execute(
                            SupplierSettlementBatch.__table__.delete()
                        )
                        connection.execute(OrderItem.__table__.delete())
                        connection.execute(Order.__table__.delete())
                finally:
                    engine.dispose()
            if upgraded:
                command.downgrade(config, "base")

        tables_final, revision_final = get_database_state(self.database_url)
        self.assertFalse(tables_final - {"alembic_version"})
        self.assertIsNone(revision_final)


if __name__ == "__main__":
    unittest.main()
