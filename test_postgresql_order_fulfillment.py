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
from app.mall import (
    OrderLineRequest,
    execute_order_completion,
    execute_order_fulfillment,
    execute_order_placement,
    execute_order_refund,
    execute_order_shipping,
    receive_inventory,
    record_initial_points_grant,
)
from app.models import (
    AdminActionLog,
    BusinessRecord,
    InventoryBalance,
    InventoryMovement,
    Member,
    Order,
    PointsAccount,
    PointsGrant,
    PointsLedgerEntry,
    Product,
    ProductCategory,
    ProductSku,
    Supplier,
    UploadBatch,
    User,
)
from test_postgresql_migration import (
    POSTGRES_TEST_ALLOW_RESET_ENV_NAME,
    POSTGRES_TEST_DATABASE_URL_ENV_NAME,
    build_alembic_config,
    get_database_state,
    validate_postgresql_test_database_url,
)
from test_postgresql_order_cancellation import (
    clear_order_reservation_movements_for_downgrade,
)


NOW = datetime(2026, 9, 17, 12, 0, 0)
ZERO = Decimal("0.00")


class PostgreSQLOrderFulfillmentIntegrationTests(unittest.TestCase):
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

    def test_place_and_concurrent_lifecycle_on_disposable_database(self):
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
                    username="pg-fulfillment-operator",
                    password_hash="test-only",
                    role="admin",
                    admin_level=OPERATOR,
                    is_active=True,
                )
                partner = User(
                    username="pg-fulfillment-partner",
                    password_hash="test-only",
                    role="partner",
                    is_active=True,
                )
                db.add_all([operator, partner])
                db.flush()
                batch = UploadBatch(
                    user_id=partner.id,
                    filename="pg-fulfillment-points.xlsx",
                    total_rows=1,
                    success_rows=1,
                    failed_rows=0,
                    acceptance_status="已承接",
                    redemption_mode="MALL_REDEMPTION",
                    claim_deadline=NOW + timedelta(days=90),
                )
                member = Member(
                    member_public_id="MEM-PG-FULFILL-001",
                    is_active=True,
                    created_at=NOW,
                    updated_at=NOW,
                )
                db.add_all([batch, member])
                db.flush()
                account = PointsAccount(member_id=member.id)
                db.add(account)
                db.flush()
                business = BusinessRecord(
                    user_id=partner.id,
                    batch_id=batch.id,
                    business_no="BR-PG-FULFILL-001",
                    public_business_no="BR-PG-FULFILL-001",
                    name="PostgreSQL 履约会员",
                    phone="13812345678",
                    plate_number="桂A12345",
                    points_amount=Decimal("100.00"),
                    bank_card="",
                    redemption_mode="MALL_REDEMPTION",
                    claim_status="ACTIVATED",
                )
                db.add(business)
                db.flush()
                grant = PointsGrant(
                    account_id=account.id,
                    business_record_id=business.id,
                    granted_points=Decimal("100.00"),
                    available_points=ZERO,
                    reserved_points=ZERO,
                    activated_at=NOW - timedelta(days=30),
                    expires_at=NOW + timedelta(days=300),
                    status="ACTIVE",
                    created_at=NOW,
                    updated_at=NOW,
                )
                db.add(grant)
                db.flush()
                record_initial_points_grant(
                    db,
                    grant=grant,
                    idempotency_key="pg-fulfillment-fixture-grant",
                    reference_type="BUSINESS_RECORD",
                    reference_id=business.public_business_no,
                    now=NOW - timedelta(days=30),
                )

                category = ProductCategory(
                    name="PostgreSQL 履约分类",
                    slug="pg-fulfillment-test",
                    is_active=True,
                    created_at=NOW,
                    updated_at=NOW,
                )
                supplier = Supplier(
                    supplier_public_id="SUP-PG-FULFILL-001",
                    name="PostgreSQL 履约供应商",
                    is_active=True,
                    created_at=NOW,
                    updated_at=NOW,
                )
                db.add_all([category, supplier])
                db.flush()
                product = Product(
                    product_public_id="PRD-PG-FULFILL-001",
                    category_id=category.id,
                    name="PostgreSQL 履约商品",
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
                    sku_code="PG-FULFILL-SKU-001",
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
                receive_inventory(
                    db,
                    actor_admin_id=operator.id,
                    sku_id=sku.id,
                    quantity=5,
                    reason="PostgreSQL 履约测试入库",
                    idempotency_key="pg-fulfillment-fixture-stock",
                    now=NOW,
                )
                db.commit()
                operator_id = operator.id
                member_id = member.id
                account_id = account.id
                sku_id = sku.id

            placed = execute_order_placement(
                engine,
                member_id=member_id,
                lines=(OrderLineRequest(sku_id, 2),),
                idempotency_key="pg-order-fulfillment-placement",
                now=NOW,
            )
            barrier = Barrier(2)

            def fulfill_once():
                barrier.wait()
                return execute_order_fulfillment(
                    engine,
                    actor_admin_id=operator_id,
                    order_public_id=placed.order_public_id,
                    now=NOW + timedelta(minutes=1),
                )

            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(fulfill_once) for _ in range(2)]
                outcomes = [future.result() for future in futures]

            self.assertEqual(
                sorted(result.replayed for result in outcomes),
                [False, True],
            )
            with Session() as db:
                order = db.get(Order, placed.order_id)
                account = db.get(PointsAccount, account_id)
                balance = db.query(InventoryBalance).filter_by(
                    sku_id=sku_id
                ).one()
                self.assertEqual(order.status, "FULFILLING")
                self.assertEqual(account.available_points, Decimal("20.00"))
                self.assertEqual(account.reserved_points, ZERO)
                self.assertEqual(balance.on_hand_quantity, 3)
                self.assertEqual(balance.reserved_quantity, 0)
                self.assertEqual(
                    db.query(PointsLedgerEntry).filter_by(
                        entry_type="CONSUME"
                    ).count(),
                    1,
                )
                self.assertEqual(
                    db.query(InventoryMovement).filter_by(
                        movement_type="OUTBOUND"
                    ).count(),
                    1,
                )
                self.assertEqual(
                    db.query(AdminActionLog).filter_by(
                        action_type="mall_order_fulfill"
                    ).count(),
                    1,
                )

            shipping_barrier = Barrier(2)

            def ship_once():
                shipping_barrier.wait()
                return execute_order_shipping(
                    engine,
                    actor_admin_id=operator_id,
                    order_public_id=placed.order_public_id,
                    shipping_carrier="顺丰速运",
                    tracking_number="SF-PG-M5-5-001",
                    now=NOW + timedelta(minutes=2),
                )

            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(ship_once) for _ in range(2)]
                shipping_outcomes = [future.result() for future in futures]
            self.assertEqual(
                sorted(result.replayed for result in shipping_outcomes),
                [False, True],
            )

            completion_barrier = Barrier(2)

            def complete_once():
                completion_barrier.wait()
                return execute_order_completion(
                    engine,
                    actor_admin_id=operator_id,
                    order_public_id=placed.order_public_id,
                    now=NOW + timedelta(minutes=3),
                )

            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(complete_once) for _ in range(2)]
                completion_outcomes = [future.result() for future in futures]
            self.assertEqual(
                sorted(result.replayed for result in completion_outcomes),
                [False, True],
            )
            with Session() as db:
                order = db.get(Order, placed.order_id)
                self.assertEqual(order.status, "COMPLETED")
                self.assertEqual(order.shipping_carrier, "顺丰速运")
                self.assertEqual(order.tracking_number, "SF-PG-M5-5-001")
                self.assertIsNotNone(order.shipped_at)
                self.assertIsNotNone(order.completed_at)
                self.assertEqual(
                    db.query(AdminActionLog).filter_by(
                        action_type="mall_order_ship"
                    ).count(),
                    1,
                )
                self.assertEqual(
                    db.query(AdminActionLog).filter_by(
                        action_type="mall_order_complete"
                    ).count(),
                    1,
                )

            refund_barrier = Barrier(2)

            def refund_once():
                refund_barrier.wait()
                return execute_order_refund(
                    engine,
                    actor_admin_id=operator_id,
                    order_public_id=placed.order_public_id,
                    reason="PostgreSQL M5-6 整单退款验收",
                    now=NOW + timedelta(minutes=4),
                )

            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(refund_once) for _ in range(2)]
                refund_outcomes = [future.result() for future in futures]
            self.assertEqual(
                sorted(result.replayed for result in refund_outcomes),
                [False, True],
            )
            with Session() as db:
                order = db.get(Order, placed.order_id)
                account = db.get(PointsAccount, account_id)
                balance = db.query(InventoryBalance).filter_by(
                    sku_id=sku_id
                ).one()
                self.assertEqual(order.status, "REFUNDED")
                self.assertEqual(
                    order.refund_reason,
                    "PostgreSQL M5-6 整单退款验收",
                )
                self.assertIsNotNone(order.refunded_at)
                self.assertEqual(account.available_points, Decimal("100.00"))
                self.assertEqual(account.reserved_points, ZERO)
                self.assertEqual(balance.on_hand_quantity, 5)
                self.assertEqual(balance.reserved_quantity, 0)
                self.assertEqual(
                    db.query(PointsLedgerEntry).filter_by(
                        entry_type="REFUND"
                    ).count(),
                    1,
                )
                self.assertEqual(
                    db.query(InventoryMovement).filter_by(
                        movement_type="RETURN"
                    ).count(),
                    1,
                )
                self.assertEqual(
                    db.query(AdminActionLog).filter_by(
                        action_type="mall_order_refund"
                    ).count(),
                    1,
                )
        finally:
            if engine is not None:
                try:
                    with engine.begin() as connection:
                        connection.execute(
                            Order.__table__.update().values(
                                status="FULFILLING",
                                shipping_carrier=None,
                                tracking_number=None,
                                shipped_at=None,
                                completed_at=None,
                                refund_reason=None,
                                refunded_at=None,
                            )
                        )
                    clear_order_reservation_movements_for_downgrade(engine)
                finally:
                    engine.dispose()
            if upgraded:
                command.downgrade(config, "base")


if __name__ == "__main__":
    unittest.main()
