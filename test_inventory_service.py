import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.admin_permissions import OPERATOR, PRIMARY_REVIEWER
from app.database import Base
from app.mall import (
    INVENTORY_BALANCE_MISMATCH_MESSAGE,
    INVENTORY_PERMISSION_MESSAGE,
    InventoryStockStatus,
    adjust_inventory,
    assert_inventory_balance_consistent,
    audit_inventory_balance,
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


NOW = datetime(2026, 9, 11, 12, 0, 0)


class InventoryServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "inventory.db"
        self.engine = create_engine(f"sqlite:///{self.path}")
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(
            bind=self.engine,
            autoflush=False,
            expire_on_commit=False,
        )
        self.db = self.Session()
        self.operator = self.add_user("inventory-operator", OPERATOR)
        self.other_operator = self.add_user("other-operator", OPERATOR)
        self.reviewer = self.add_user(
            "inventory-reviewer",
            PRIMARY_REVIEWER,
        )
        self.category = ProductCategory(
            name="车载用品",
            slug="inventory-category",
            is_active=True,
            created_at=NOW,
            updated_at=NOW,
        )
        self.supplier = Supplier(
            supplier_public_id="SUP-INVENTORY01",
            name="库存测试供应商",
            is_active=True,
            created_at=NOW,
            updated_at=NOW,
        )
        self.db.add_all([self.category, self.supplier])
        self.db.flush()
        self.product = Product(
            product_public_id="PRD-INVENTORY01",
            category_id=self.category.id,
            name="应急工具箱",
            status="DRAFT",
            created_at=NOW,
            updated_at=NOW,
        )
        self.db.add(self.product)
        self.db.flush()
        self.sku = ProductSku(
            product_id=self.product.id,
            supplier_id=self.supplier.id,
            sku_code="INVENTORY-SKU-001",
            name="标准款",
            points_price="100.00",
            cost_price="50.00",
            low_stock_threshold=5,
            is_active=True,
            created_at=NOW,
            updated_at=NOW,
        )
        self.db.add(self.sku)
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()
        self.temp.cleanup()

    def add_user(self, username, admin_level, *, is_active=True):
        user = User(
            username=username,
            password_hash="test-only",
            role="admin",
            admin_level=admin_level,
            is_active=is_active,
        )
        self.db.add(user)
        self.db.flush()
        return user

    def test_receipt_and_adjustment_append_auditable_movements(self):
        received = receive_inventory(
            self.db,
            actor_admin_id=self.operator.id,
            sku_id=self.sku.id,
            quantity=10,
            reason="首批入库",
            idempotency_key="receipt-001",
            now=NOW,
        )
        adjusted = adjust_inventory(
            self.db,
            actor_admin_id=self.operator.id,
            sku_id=self.sku.id,
            quantity_delta=-3,
            reason="盘点减少",
            idempotency_key="adjustment-001",
            now=NOW,
        )
        self.db.commit()

        self.assertFalse(received.replayed)
        self.assertFalse(adjusted.replayed)
        self.assertEqual(adjusted.snapshot.on_hand_quantity, 7)
        self.assertEqual(adjusted.snapshot.available_quantity, 7)
        self.assertEqual(adjusted.snapshot.balance_version, 2)
        self.assertIs(
            adjusted.snapshot.stock_status,
            InventoryStockStatus.IN_STOCK,
        )
        movements = self.db.query(InventoryMovement).order_by(
            InventoryMovement.balance_version
        ).all()
        self.assertEqual(
            [row.movement_type for row in movements],
            ["RECEIPT", "ADJUSTMENT"],
        )
        self.assertEqual(
            [
                (
                    row.quantity_delta,
                    row.quantity_before,
                    row.quantity_after,
                    row.balance_version,
                )
                for row in movements
            ],
            [(10, 0, 10, 1), (-3, 10, 7, 2)],
        )
        self.assertEqual(
            [row.action_type for row in self.db.query(AdminActionLog).order_by(
                AdminActionLog.id
            )],
            ["mall_inventory_receive", "mall_inventory_adjust"],
        )
        audit = assert_inventory_balance_consistent(
            self.db,
            sku_id=self.sku.id,
        )
        self.assertTrue(audit.is_consistent)
        self.assertEqual(audit.ledger_quantity, 7)
        self.assertEqual(audit.ledger_version, 2)

    def test_idempotent_replay_does_not_duplicate_stock_or_audit(self):
        first = receive_inventory(
            self.db,
            actor_admin_id=self.operator.id,
            sku_id=self.sku.id,
            quantity=8,
            reason="采购入库",
            idempotency_key="same-request",
            now=NOW,
        )
        replay = receive_inventory(
            self.db,
            actor_admin_id=self.operator.id,
            sku_id=self.sku.id,
            quantity=8,
            reason="采购入库",
            idempotency_key="same-request",
            now=NOW,
        )
        self.db.commit()

        self.assertFalse(first.replayed)
        self.assertTrue(replay.replayed)
        self.assertEqual(first.movement.id, replay.movement.id)
        self.assertEqual(replay.snapshot.on_hand_quantity, 8)
        self.assertEqual(self.db.query(InventoryMovement).count(), 1)
        self.assertEqual(self.db.query(AdminActionLog).count(), 1)

    def test_idempotency_key_conflict_fails_closed(self):
        receive_inventory(
            self.db,
            actor_admin_id=self.operator.id,
            sku_id=self.sku.id,
            quantity=8,
            reason="采购入库",
            idempotency_key="conflict-key",
            now=NOW,
        )
        with self.assertRaisesRegex(ValueError, "幂等键已用于其他操作"):
            receive_inventory(
                self.db,
                actor_admin_id=self.operator.id,
                sku_id=self.sku.id,
                quantity=9,
                reason="采购入库",
                idempotency_key="conflict-key",
                now=NOW,
            )
        with self.assertRaisesRegex(ValueError, "幂等键已用于其他操作"):
            receive_inventory(
                self.db,
                actor_admin_id=self.other_operator.id,
                sku_id=self.sku.id,
                quantity=8,
                reason="采购入库",
                idempotency_key="conflict-key",
                now=NOW,
            )
        self.assertEqual(self.db.query(InventoryMovement).count(), 1)

    def test_invalid_or_negative_final_quantity_creates_no_movement(self):
        for invalid_quantity in (0, -1, True, "3"):
            with self.subTest(invalid_quantity=invalid_quantity):
                with self.assertRaises(ValueError):
                    receive_inventory(
                        self.db,
                        actor_admin_id=self.operator.id,
                        sku_id=self.sku.id,
                        quantity=invalid_quantity,
                        reason="非法入库",
                        idempotency_key=f"invalid-{invalid_quantity}",
                    )

        receive_inventory(
            self.db,
            actor_admin_id=self.operator.id,
            sku_id=self.sku.id,
            quantity=2,
            reason="有效入库",
            idempotency_key="valid-receipt",
            now=NOW,
        )
        self.db.commit()
        with self.assertRaisesRegex(ValueError, "不能低于已预占数量"):
            adjust_inventory(
                self.db,
                actor_admin_id=self.operator.id,
                sku_id=self.sku.id,
                quantity_delta=-3,
                reason="非法扣减",
                idempotency_key="invalid-adjustment",
            )
        self.assertEqual(self.db.query(InventoryMovement).count(), 1)
        self.assertEqual(
            self.db.query(InventoryBalance).one().on_hand_quantity,
            2,
        )

    def test_low_and_out_of_stock_states_use_available_quantity(self):
        empty = audit_inventory_balance(self.db, sku_id=self.sku.id)
        self.assertTrue(empty.is_consistent)
        self.assertIs(
            empty.snapshot.stock_status,
            InventoryStockStatus.OUT_OF_STOCK,
        )
        receive_inventory(
            self.db,
            actor_admin_id=self.operator.id,
            sku_id=self.sku.id,
            quantity=5,
            reason="低库存入库",
            idempotency_key="low-stock",
            now=NOW,
        )
        low = audit_inventory_balance(self.db, sku_id=self.sku.id)
        self.assertIs(low.snapshot.stock_status, InventoryStockStatus.LOW_STOCK)
        adjust_inventory(
            self.db,
            actor_admin_id=self.operator.id,
            sku_id=self.sku.id,
            quantity_delta=-5,
            reason="清空库存",
            idempotency_key="out-of-stock",
            now=NOW,
        )
        out = audit_inventory_balance(self.db, sku_id=self.sku.id)
        self.assertIs(out.snapshot.stock_status, InventoryStockStatus.OUT_OF_STOCK)

    def test_unauthorized_and_inactive_accounts_fail_closed(self):
        inactive = self.add_user("inactive-operator", OPERATOR, is_active=False)
        self.db.commit()
        for actor_id in (self.reviewer.id, inactive.id, 999999):
            with self.subTest(actor_id=actor_id):
                with self.assertRaisesRegex(
                    PermissionError,
                    INVENTORY_PERMISSION_MESSAGE,
                ):
                    receive_inventory(
                        self.db,
                        actor_admin_id=actor_id,
                        sku_id=self.sku.id,
                        quantity=1,
                        reason="无权操作",
                        idempotency_key=f"blocked-{actor_id}",
                    )
        self.assertEqual(self.db.query(InventoryMovement).count(), 0)
        self.assertEqual(self.db.query(InventoryBalance).count(), 0)

    def test_balance_tampering_is_detected(self):
        receive_inventory(
            self.db,
            actor_admin_id=self.operator.id,
            sku_id=self.sku.id,
            quantity=4,
            reason="测试入库",
            idempotency_key="tamper-test",
            now=NOW,
        )
        self.db.commit()
        balance = self.db.query(InventoryBalance).one()
        balance.on_hand_quantity = 99
        self.db.flush()

        result = audit_inventory_balance(self.db, sku_id=self.sku.id)
        self.assertFalse(result.is_consistent)
        with self.assertRaisesRegex(
            RuntimeError,
            INVENTORY_BALANCE_MISMATCH_MESSAGE,
        ):
            assert_inventory_balance_consistent(self.db, sku_id=self.sku.id)
        with self.assertRaisesRegex(
            RuntimeError,
            INVENTORY_BALANCE_MISMATCH_MESSAGE,
        ):
            adjust_inventory(
                self.db,
                actor_admin_id=self.operator.id,
                sku_id=self.sku.id,
                quantity_delta=1,
                reason="不应写入",
                idempotency_key="blocked-by-drift",
                now=NOW,
            )
        self.assertEqual(self.db.query(InventoryMovement).count(), 1)


if __name__ == "__main__":
    unittest.main()
