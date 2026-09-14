import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import unquote_plus

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from starlette.requests import Request

from app.admin_permissions import OPERATOR, PRIMARY_REVIEWER, SUPER_ADMIN
from app.database import Base
from app.main import (
    admin_navigation_context,
    adjust_inventory_route,
    mall_inventory_page,
    receive_inventory_route,
)
from app.mall import (
    InventoryStockStatus,
    list_inventory_admin,
    receive_inventory,
)
from app.models import (
    AdminActionLog,
    InventoryBalance,
    InventoryMovement,
    Product,
    ProductCategory,
    ProductSku,
    Supplier,
    User,
)


NOW = datetime(2026, 9, 14, 10, 0, 0)


def make_request(path="/mall-inventory", method="GET"):
    return Request({
        "type": "http",
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "root_path": "",
        "query_string": b"",
        "headers": [],
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
    })


class InventoryRouteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "inventory-routes.db"
        self.engine = create_engine(f"sqlite:///{self.path}")
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(
            bind=self.engine,
            autoflush=False,
            expire_on_commit=False,
        )
        with self.Session() as db:
            self.super_admin = self.add_user(
                db, "inventory-super", SUPER_ADMIN
            )
            self.operator = self.add_user(
                db, "inventory-operator", OPERATOR
            )
            self.reviewer = self.add_user(
                db, "inventory-reviewer", PRIMARY_REVIEWER
            )
            partner = User(
                username="inventory-partner",
                password_hash="test-only",
                role="partner",
                is_active=True,
            )
            db.add(partner)
            db.flush()
            self.partner = self.user_namespace(partner)
            category = ProductCategory(
                name="车载用品",
                slug="inventory-routes",
                is_active=True,
                created_at=NOW,
                updated_at=NOW,
            )
            supplier = Supplier(
                supplier_public_id="SUP-INVENTORY-ROUTE",
                name="库存路由供应商",
                is_active=True,
                created_at=NOW,
                updated_at=NOW,
            )
            db.add_all([category, supplier])
            db.flush()
            product = Product(
                product_public_id="PRD-INVENTORY-ROUTE",
                category_id=category.id,
                name="应急工具箱",
                status="DRAFT",
                created_at=NOW,
                updated_at=NOW,
            )
            db.add(product)
            db.flush()
            skus = [
                ProductSku(
                    product_id=product.id,
                    supplier_id=supplier.id,
                    sku_code="INVENTORY-ROUTE-A",
                    name="标准款",
                    points_price="100.00",
                    cost_price="50.00",
                    low_stock_threshold=5,
                    is_active=True,
                    sort_order=1,
                    created_at=NOW,
                    updated_at=NOW,
                ),
                ProductSku(
                    product_id=product.id,
                    supplier_id=supplier.id,
                    sku_code="INVENTORY-ROUTE-B",
                    name="升级款",
                    points_price="150.00",
                    cost_price="75.00",
                    low_stock_threshold=5,
                    is_active=True,
                    sort_order=2,
                    created_at=NOW,
                    updated_at=NOW,
                ),
                ProductSku(
                    product_id=product.id,
                    supplier_id=supplier.id,
                    sku_code="INVENTORY-ROUTE-C",
                    name="豪华款",
                    points_price="200.00",
                    cost_price="100.00",
                    low_stock_threshold=0,
                    is_active=True,
                    sort_order=3,
                    created_at=NOW,
                    updated_at=NOW,
                ),
            ]
            db.add_all(skus)
            db.flush()
            self.sku_ids = tuple(sku.id for sku in skus)
            db.commit()

    def tearDown(self):
        self.engine.dispose()
        self.temp.cleanup()

    @staticmethod
    def user_namespace(user):
        return SimpleNamespace(
            id=user.id,
            username=user.username,
            role=user.role,
            admin_level=user.admin_level,
            is_active=user.is_active,
        )

    def add_user(self, db, username, admin_level):
        user = User(
            username=username,
            password_hash="test-only",
            role="admin",
            admin_level=admin_level,
            is_active=True,
        )
        db.add(user)
        db.flush()
        return self.user_namespace(user)

    def call_page(
        self,
        user,
        *,
        keyword="",
        stock_status="ALL",
        page=1,
        page_size=10,
        movement_sku_id=0,
        movement_page=1,
        message="",
        error="",
    ):
        with (
            patch("app.main.get_current_user", return_value=user),
            patch("app.main.SessionLocal", side_effect=self.Session),
        ):
            return mall_inventory_page(
                make_request(),
                keyword,
                stock_status,
                page,
                page_size,
                movement_sku_id,
                movement_page,
                message,
                error,
            )

    def call_mutation(self, user, function, *args):
        with (
            patch("app.main.get_current_user", return_value=user),
            patch("app.main.SessionLocal", side_effect=self.Session),
        ):
            return function(make_request(method="POST"), *args)

    def test_navigation_and_page_access_follow_inventory_permission(self):
        for user, allowed in (
            (self.super_admin, True),
            (self.operator, True),
            (self.reviewer, False),
            (self.partner, False),
            (None, False),
        ):
            with self.subTest(user=user):
                with patch("app.main.get_current_user", return_value=user):
                    context = admin_navigation_context(make_request())
                self.assertEqual(
                    context["can_manage_mall_inventory"], allowed
                )

        with (
            patch("app.main.get_current_user", return_value=None),
            patch("app.main.SessionLocal") as session_local,
        ):
            response = mall_inventory_page(
                make_request(), "", "ALL", 1, 10, 0, 1, "", ""
            )
        self.assertEqual(response.headers["location"], "/login")
        session_local.assert_not_called()

        with (
            patch("app.main.get_current_user", return_value=self.reviewer),
            patch("app.main.SessionLocal") as session_local,
        ):
            response = mall_inventory_page(
                make_request(), "", "ALL", 1, 10, 0, 1, "", ""
            )
        self.assertEqual(response.headers["location"], "/dashboard")
        session_local.assert_not_called()

    def test_read_model_reports_statuses_filters_pages_and_is_read_only(self):
        with self.Session() as db:
            receive_inventory(
                db,
                actor_admin_id=self.operator.id,
                sku_id=self.sku_ids[0],
                quantity=10,
                reason="正常库存",
                idempotency_key="snapshot-in-stock",
                now=NOW,
            )
            receive_inventory(
                db,
                actor_admin_id=self.operator.id,
                sku_id=self.sku_ids[1],
                quantity=3,
                reason="低库存",
                idempotency_key="snapshot-low-stock",
                now=NOW,
            )
            db.commit()
            result = list_inventory_admin(
                db,
                stock_status=InventoryStockStatus.LOW_STOCK.value,
                page=9,
                page_size=1,
                movement_sku_id=self.sku_ids[1],
            )
            self.assertEqual(result.summary.sku_count, 3)
            self.assertEqual(result.summary.in_stock_count, 1)
            self.assertEqual(result.summary.low_stock_count, 1)
            self.assertEqual(result.summary.out_of_stock_count, 1)
            self.assertEqual(result.summary.movement_count, 2)
            self.assertEqual(result.total, 1)
            self.assertEqual(result.page, 1)
            self.assertEqual(result.items[0].sku_code, "INVENTORY-ROUTE-B")
            self.assertEqual(result.movements.total, 1)
            self.assertEqual(
                result.movements.items[0].movement_type_label, "入库"
            )
            self.assertFalse(db.new)
            self.assertFalse(db.dirty)

    def test_operator_page_renders_controls_without_client_actor_field(self):
        response = self.call_page(self.operator)
        body = response.body.decode("utf-8")
        self.assertEqual(response.status_code, 200)
        self.assertIn("库存管理", body)
        self.assertIn("INVENTORY-ROUTE-A", body)
        self.assertIn("无库存", body)
        self.assertIn('name="idempotency_key"', body)
        self.assertNotIn('name="actor_admin_id"', body)
        self.assertIn("暂无库存流水", body)

    def test_invalid_filter_falls_back_with_controlled_error(self):
        response = self.call_page(
            self.operator,
            stock_status="INVALID",
        )
        body = response.body.decode("utf-8")
        self.assertEqual(response.status_code, 200)
        self.assertIn("库存状态筛选无效", body)
        self.assertIn("INVENTORY-ROUTE-A", body)

    def test_receive_replay_and_adjustment_use_domain_services(self):
        sku_id = self.sku_ids[0]
        response = self.call_mutation(
            self.operator,
            receive_inventory_route,
            sku_id,
            10,
            "采购到货",
            "route-receipt-001",
        )
        self.assertEqual(response.status_code, 303)
        self.assertIn(
            "库存入库已完成",
            unquote_plus(response.headers["location"]),
        )
        response = self.call_mutation(
            self.operator,
            receive_inventory_route,
            sku_id,
            10,
            "采购到货",
            "route-receipt-001",
        )
        self.assertIn(
            "本次未重复写入",
            unquote_plus(response.headers["location"]),
        )
        response = self.call_mutation(
            self.operator,
            adjust_inventory_route,
            sku_id,
            -6,
            "盘点减少",
            "route-adjust-001",
        )
        self.assertIn(
            "库存人工调整已完成",
            unquote_plus(response.headers["location"]),
        )

        with self.Session() as db:
            balance = db.query(InventoryBalance).filter(
                InventoryBalance.sku_id == sku_id
            ).one()
            self.assertEqual(balance.on_hand_quantity, 4)
            self.assertEqual(balance.version, 2)
            self.assertEqual(db.query(InventoryMovement).count(), 2)
            self.assertEqual(db.query(AdminActionLog).count(), 2)

        response = self.call_page(
            self.operator,
            stock_status="LOW_STOCK",
            movement_sku_id=sku_id,
        )
        body = response.body.decode("utf-8")
        self.assertIn("低库存", body)
        self.assertIn("采购到货", body)
        self.assertIn("盘点减少", body)
        self.assertIn("10 → 4", body)

    def test_unauthorized_write_does_not_open_transaction(self):
        with (
            patch("app.main.get_current_user", return_value=self.reviewer),
            patch("app.main.SessionLocal") as session_local,
        ):
            response = receive_inventory_route(
                make_request(method="POST"),
                self.sku_ids[0],
                1,
                "越权入库",
                "forbidden-receipt",
            )
        self.assertEqual(response.headers["location"], "/dashboard")
        session_local.assert_not_called()
        with self.Session() as db:
            self.assertEqual(db.query(InventoryMovement).count(), 0)

    def test_failed_adjustment_rolls_back_with_service_message(self):
        response = self.call_mutation(
            self.operator,
            adjust_inventory_route,
            self.sku_ids[0],
            -1,
            "不能出现负库存",
            "invalid-adjustment",
        )
        location = unquote_plus(response.headers["location"])
        self.assertIn("库存调整后不能低于已预占数量", location)
        with self.Session() as db:
            self.assertEqual(db.query(InventoryBalance).count(), 0)
            self.assertEqual(db.query(InventoryMovement).count(), 0)
            self.assertEqual(db.query(AdminActionLog).count(), 0)

    def test_balance_mismatch_is_visible_and_blocks_route_writes(self):
        sku_id = self.sku_ids[0]
        with self.Session() as db:
            receive_inventory(
                db,
                actor_admin_id=self.operator.id,
                sku_id=sku_id,
                quantity=4,
                reason="建立测试余额",
                idempotency_key="tamper-source",
                now=NOW,
            )
            db.commit()
            balance = db.query(InventoryBalance).filter(
                InventoryBalance.sku_id == sku_id
            ).one()
            balance.on_hand_quantity = 99
            db.commit()

        response = self.call_page(self.operator)
        body = response.body.decode("utf-8")
        self.assertIn("账实异常 1", body)
        self.assertIn("已停止写入", body)
        response = self.call_mutation(
            self.operator,
            adjust_inventory_route,
            sku_id,
            1,
            "不应写入",
            "blocked-by-mismatch",
        )
        self.assertIn(
            "SKU 库存余额与流水不一致",
            unquote_plus(response.headers["location"]),
        )
        with self.Session() as db:
            self.assertEqual(db.query(InventoryMovement).count(), 1)
            self.assertEqual(db.query(AdminActionLog).count(), 1)

    def test_unexpected_failure_rolls_back_and_is_reraised(self):
        def fail_after_pending_write(db, **_kwargs):
            db.add(InventoryBalance(
                sku_id=self.sku_ids[0],
                on_hand_quantity=1,
                reserved_quantity=0,
                version=1,
                created_at=NOW,
                updated_at=NOW,
            ))
            raise RuntimeError("unexpected inventory failure")

        with (
            patch("app.main.get_current_user", return_value=self.operator),
            patch("app.main.SessionLocal", side_effect=self.Session),
            patch(
                "app.main.receive_inventory",
                side_effect=fail_after_pending_write,
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "unexpected"):
                receive_inventory_route(
                    make_request(method="POST"),
                    self.sku_ids[0],
                    1,
                    "触发异常",
                    "unexpected-request",
                )
        with self.Session() as db:
            self.assertEqual(db.query(InventoryBalance).count(), 0)


if __name__ == "__main__":
    unittest.main()
