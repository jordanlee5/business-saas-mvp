import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from threading import Barrier

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.admin_permissions import OPERATOR
from app.database import Base
from app.mall import (
    OrderLineRequest,
    execute_order_cancellation,
    execute_order_placement,
    expire_points_grant,
    receive_inventory,
    record_initial_points_grant,
)
from app.models import (
    BusinessRecord,
    InventoryBalance,
    InventoryMovement,
    Member,
    Order,
    OrderItem,
    OrderPointsGrantAllocation,
    PointsAccount,
    PointsGrant,
    PointsLedgerEntry,
    Product,
    ProductCategory,
    ProductMedia,
    ProductSku,
    Supplier,
    UploadBatch,
    User,
)


NOW = datetime(2026, 9, 15, 12, 0, 0)
ZERO = Decimal("0.00")


class OrderServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "orders.db"
        self.engine = create_engine(f"sqlite:///{self.path}")
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(
            bind=self.engine,
            autoflush=False,
            expire_on_commit=False,
        )
        self.db = self.Session()

        self.operator = User(
            username="order-stock-operator",
            password_hash="test-only",
            role="admin",
            admin_level=OPERATOR,
            is_active=True,
        )
        self.partner = User(
            username="order-source-partner",
            password_hash="test-only",
            role="partner",
            is_active=True,
        )
        self.db.add_all([self.operator, self.partner])
        self.db.flush()
        self.batch = UploadBatch(
            user_id=self.partner.id,
            filename="order-points.xlsx",
            total_rows=2,
            success_rows=2,
            failed_rows=0,
            acceptance_status="已承接",
            redemption_mode="MALL_REDEMPTION",
            claim_deadline=NOW + timedelta(days=90),
        )
        self.member = Member(
            member_public_id="MEM-ORDER-001",
            is_active=True,
            created_at=NOW,
            updated_at=NOW,
        )
        self.db.add_all([self.batch, self.member])
        self.db.flush()
        self.account = PointsAccount(member_id=self.member.id)
        self.db.add(self.account)
        self.db.flush()
        self.early_grant = self.add_grant(
            public_no="BR-ORDER-EARLY",
            points="70.00",
            expires_at=NOW + timedelta(days=10),
        )
        self.later_grant = self.add_grant(
            public_no="BR-ORDER-LATER",
            points="100.00",
            expires_at=NOW + timedelta(days=20),
        )

        self.category = ProductCategory(
            name="订单测试分类",
            slug="order-test",
            is_active=True,
            created_at=NOW,
            updated_at=NOW,
        )
        self.supplier = Supplier(
            supplier_public_id="SUP-ORDER-001",
            name="订单测试供应商",
            is_active=True,
            created_at=NOW,
            updated_at=NOW,
        )
        self.db.add_all([self.category, self.supplier])
        self.db.flush()
        self.product = Product(
            product_public_id="PRD-ORDER-001",
            category_id=self.category.id,
            name="订单测试商品",
            status="PUBLISHED",
            published_at=NOW,
            created_at=NOW,
            updated_at=NOW,
        )
        self.db.add(self.product)
        self.db.flush()
        self.db.add(ProductMedia(
            product_id=self.product.id,
            media_role="MAIN",
            image_path="uploads/mall_products/PRD-ORDER-001/main.webp",
            alt_text="订单测试主图",
            sort_order=0,
            is_active=True,
            uploaded_by_id=self.operator.id,
            created_at=NOW,
            updated_at=NOW,
        ))
        self.sku_one = self.add_sku(
            code="ORDER-SKU-001",
            name="标准款",
            points="40.00",
            cost="12.00",
        )
        self.sku_two = self.add_sku(
            code="ORDER-SKU-002",
            name="轻量款",
            points="25.00",
            cost="5.00",
        )
        receive_inventory(
            self.db,
            actor_admin_id=self.operator.id,
            sku_id=self.sku_one.id,
            quantity=5,
            reason="订单测试入库",
            idempotency_key="order-fixture-stock-1",
            now=NOW,
        )
        receive_inventory(
            self.db,
            actor_admin_id=self.operator.id,
            sku_id=self.sku_two.id,
            quantity=4,
            reason="订单测试入库",
            idempotency_key="order-fixture-stock-2",
            now=NOW,
        )
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()
        self.temp.cleanup()

    def add_grant(self, *, public_no, points, expires_at):
        business = BusinessRecord(
            user_id=self.partner.id,
            batch_id=self.batch.id,
            business_no=public_no,
            public_business_no=public_no,
            name="订单会员",
            phone="13812345678",
            plate_number="桂A12345",
            points_amount=Decimal(points),
            bank_card="",
            redemption_mode="MALL_REDEMPTION",
            claim_status="ACTIVATED",
        )
        self.db.add(business)
        self.db.flush()
        grant = PointsGrant(
            account_id=self.account.id,
            business_record_id=business.id,
            granted_points=Decimal(points),
            available_points=ZERO,
            reserved_points=ZERO,
            activated_at=NOW - timedelta(days=30),
            expires_at=expires_at,
            status="ACTIVE",
            created_at=NOW,
            updated_at=NOW,
        )
        self.db.add(grant)
        self.db.flush()
        record_initial_points_grant(
            self.db,
            grant=grant,
            idempotency_key=f"order-fixture-grant:{grant.id}",
            reference_type="BUSINESS_RECORD",
            reference_id=public_no,
            now=NOW - timedelta(days=30),
        )
        return grant

    def add_sku(self, *, code, name, points, cost):
        sku = ProductSku(
            product_id=self.product.id,
            supplier_id=self.supplier.id,
            sku_code=code,
            name=name,
            points_price=Decimal(points),
            cost_price=Decimal(cost),
            low_stock_threshold=1,
            is_active=True,
            created_at=NOW,
            updated_at=NOW,
        )
        self.db.add(sku)
        self.db.flush()
        return sku

    def place(self, *, key="order-request-001", lines=None):
        return execute_order_placement(
            self.engine,
            member_id=self.member.id,
            lines=lines or (
                OrderLineRequest(self.sku_one.id, 2),
                OrderLineRequest(self.sku_two.id, 1),
            ),
            idempotency_key=key,
            now=NOW,
        )

    def cancel(self, order_public_id, *, member_id=None, now=NOW):
        return execute_order_cancellation(
            self.engine,
            member_id=self.member.id if member_id is None else member_id,
            order_public_id=order_public_id,
            now=now,
        )

    def test_places_order_with_snapshots_fefo_points_and_stock(self):
        result = self.place()

        self.assertFalse(result.replayed)
        self.assertEqual(result.status, "CREATED")
        self.assertEqual(result.total_points, Decimal("105.00"))
        self.assertEqual(result.total_cost_amount, Decimal("29.00"))
        self.assertEqual(result.total_quantity, 3)
        self.assertEqual(result.item_count, 2)
        self.assertEqual(
            [item.grant_id for item in result.points_reservations],
            [self.early_grant.id, self.later_grant.id],
        )
        self.assertEqual(
            [item.allocated_points for item in result.points_reservations],
            [Decimal("70.00"), Decimal("35.00")],
        )

        self.db.expire_all()
        account = self.db.get(PointsAccount, self.account.id)
        self.assertEqual(account.available_points, Decimal("65.00"))
        self.assertEqual(account.reserved_points, Decimal("105.00"))
        self.assertEqual(account.version, 3)
        grants = self.db.query(PointsGrant).order_by(
            PointsGrant.expires_at
        ).all()
        self.assertEqual(
            [(row.available_points, row.reserved_points) for row in grants],
            [
                (Decimal("0.00"), Decimal("70.00")),
                (Decimal("65.00"), Decimal("35.00")),
            ],
        )
        reserve_entries = self.db.query(PointsLedgerEntry).filter(
            PointsLedgerEntry.entry_type == "RESERVE"
        ).order_by(PointsLedgerEntry.id).all()
        self.assertEqual(len(reserve_entries), 2)
        self.assertEqual(
            [row.available_points_delta for row in reserve_entries],
            [Decimal("-70.00"), Decimal("-35.00")],
        )

        balances = self.db.query(InventoryBalance).order_by(
            InventoryBalance.sku_id
        ).all()
        self.assertEqual(
            [(row.on_hand_quantity, row.reserved_quantity) for row in balances],
            [(5, 2), (4, 1)],
        )
        reserve_movements = self.db.query(InventoryMovement).filter(
            InventoryMovement.movement_type == "RESERVE"
        ).order_by(InventoryMovement.sku_id).all()
        self.assertEqual(
            [row.reserved_quantity_delta for row in reserve_movements],
            [2, 1],
        )
        self.assertTrue(all(row.quantity_delta == 0 for row in reserve_movements))

        items = self.db.query(OrderItem).order_by(OrderItem.sku_id).all()
        self.product.name = "目录后改名称"
        self.sku_one.points_price = Decimal("999.00")
        self.db.flush()
        self.assertEqual(items[0].product_name_snapshot, "订单测试商品")
        self.assertEqual(
            items[0].product_image_path_snapshot,
            "uploads/mall_products/PRD-ORDER-001/main.webp",
        )
        self.assertEqual(items[0].unit_points_price, Decimal("40.00"))

    def test_exact_replay_returns_same_order_without_duplicate_writes(self):
        first = self.place()
        replay = self.place()

        self.assertFalse(first.replayed)
        self.assertTrue(replay.replayed)
        self.assertEqual(replay.order_id, first.order_id)
        self.assertEqual(self.db.query(Order).count(), 1)
        self.assertEqual(self.db.query(OrderItem).count(), 2)
        self.assertEqual(self.db.query(OrderPointsGrantAllocation).count(), 2)
        self.assertEqual(
            self.db.query(PointsLedgerEntry).filter(
                PointsLedgerEntry.entry_type == "RESERVE"
            ).count(),
            2,
        )
        self.assertEqual(
            self.db.query(InventoryMovement).filter(
                InventoryMovement.movement_type == "RESERVE"
            ).count(),
            2,
        )

    def test_reused_idempotency_key_with_other_request_fails_closed(self):
        self.place()
        with self.assertRaisesRegex(ValueError, "幂等键已用于其他请求"):
            self.place(
                lines=(OrderLineRequest(self.sku_one.id, 1),),
            )
        self.assertEqual(self.db.query(Order).count(), 1)

    def test_insufficient_inventory_rolls_back_order_and_points(self):
        with self.assertRaisesRegex(ValueError, "商品库存不足"):
            self.place(
                key="no-stock",
                lines=(OrderLineRequest(self.sku_one.id, 6),),
            )
        self.db.expire_all()
        self.assertEqual(self.db.query(Order).count(), 0)
        self.assertEqual(self.db.query(OrderItem).count(), 0)
        self.assertEqual(self.db.get(InventoryBalance, 1).reserved_quantity, 0)
        account = self.db.get(PointsAccount, self.account.id)
        self.assertEqual(account.available_points, Decimal("170.00"))
        self.assertEqual(account.reserved_points, ZERO)

    def test_insufficient_points_rolls_back_prior_inventory_reservation(self):
        with self.assertRaisesRegex(ValueError, "可用积分不足"):
            self.place(
                key="no-points",
                lines=(OrderLineRequest(self.sku_one.id, 5),),
            )
        self.db.expire_all()
        self.assertEqual(self.db.query(Order).count(), 0)
        balance = self.db.query(InventoryBalance).filter(
            InventoryBalance.sku_id == self.sku_one.id
        ).one()
        self.assertEqual(balance.reserved_quantity, 0)
        self.assertEqual(
            self.db.query(InventoryMovement).filter(
                InventoryMovement.movement_type == "RESERVE"
            ).count(),
            0,
        )

    def test_expired_grant_is_excluded_from_fefo_allocation(self):
        self.early_grant.expires_at = NOW
        self.db.commit()
        result = self.place(
            key="expired-fefo",
            lines=(OrderLineRequest(self.sku_one.id, 2),),
        )
        self.assertEqual(
            [item.grant_id for item in result.points_reservations],
            [self.later_grant.id],
        )
        self.assertEqual(
            result.points_reservations[0].allocated_points,
            Decimal("80.00"),
        )

    def test_invalid_member_catalog_and_duplicate_lines_are_rejected(self):
        self.member.is_active = False
        self.db.commit()
        with self.assertRaisesRegex(ValueError, "会员不存在或已停用"):
            self.place(key="inactive-member")
        self.member.is_active = True
        self.product.status = "UNPUBLISHED"
        self.db.commit()
        with self.assertRaisesRegex(ValueError, "不可兑换"):
            self.place(key="unpublished")
        with self.assertRaisesRegex(ValueError, "不能重复提交"):
            self.place(
                key="duplicate-lines",
                lines=(
                    OrderLineRequest(self.sku_one.id, 1),
                    OrderLineRequest(self.sku_one.id, 1),
                ),
            )
        self.assertEqual(self.db.query(Order).count(), 0)

    def test_cancels_created_order_and_releases_points_and_stock(self):
        placed = self.place()
        result = self.cancel(placed.order_public_id)

        self.assertFalse(result.replayed)
        self.assertEqual(result.status, "CANCELLED")
        self.assertEqual(result.released_points, Decimal("105.00"))
        self.assertEqual(len(result.points_release_entry_ids), 2)
        self.assertEqual(len(result.inventory_release_movement_ids), 2)

        self.db.expire_all()
        order = self.db.get(Order, placed.order_id)
        self.assertEqual(order.status, "CANCELLED")
        account = self.db.get(PointsAccount, self.account.id)
        self.assertEqual(account.available_points, Decimal("170.00"))
        self.assertEqual(account.reserved_points, ZERO)
        self.assertEqual(account.version, 4)
        grants = self.db.query(PointsGrant).order_by(
            PointsGrant.expires_at
        ).all()
        self.assertEqual(
            [(row.available_points, row.reserved_points) for row in grants],
            [
                (Decimal("70.00"), ZERO),
                (Decimal("100.00"), ZERO),
            ],
        )
        release_entries = self.db.query(PointsLedgerEntry).filter(
            PointsLedgerEntry.entry_type == "RELEASE"
        ).order_by(PointsLedgerEntry.id).all()
        self.assertEqual(len(release_entries), 2)
        self.assertEqual(
            [row.reserved_points_delta for row in release_entries],
            [Decimal("-70.00"), Decimal("-35.00")],
        )

        balances = self.db.query(InventoryBalance).order_by(
            InventoryBalance.sku_id
        ).all()
        self.assertEqual(
            [(row.on_hand_quantity, row.reserved_quantity) for row in balances],
            [(5, 0), (4, 0)],
        )
        releases = self.db.query(InventoryMovement).filter(
            InventoryMovement.movement_type == "RELEASE"
        ).order_by(InventoryMovement.sku_id).all()
        self.assertEqual(
            [row.reserved_quantity_delta for row in releases],
            [-2, -1],
        )
        self.assertTrue(all(row.quantity_delta == 0 for row in releases))

    def test_cancel_replay_does_not_duplicate_release_evidence(self):
        placed = self.place()
        first = self.cancel(placed.order_public_id)
        replay = self.cancel(placed.order_public_id)

        self.assertFalse(first.replayed)
        self.assertTrue(replay.replayed)
        self.assertEqual(
            replay.points_release_entry_ids,
            first.points_release_entry_ids,
        )
        self.assertEqual(
            replay.inventory_release_movement_ids,
            first.inventory_release_movement_ids,
        )
        self.assertEqual(
            self.db.query(PointsLedgerEntry).filter_by(
                entry_type="RELEASE"
            ).count(),
            2,
        )
        self.assertEqual(
            self.db.query(InventoryMovement).filter_by(
                movement_type="RELEASE"
            ).count(),
            2,
        )

    def test_concurrent_cancel_creates_one_release_set(self):
        placed = self.place()
        barrier = Barrier(2)

        def cancel_once():
            barrier.wait()
            return self.cancel(placed.order_public_id)

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = [pool.submit(cancel_once) for _ in range(2)]
            outcomes = [future.result() for future in results]

        self.assertEqual(
            sorted(result.replayed for result in outcomes),
            [False, True],
        )
        self.assertEqual(
            self.db.query(PointsLedgerEntry).filter_by(
                entry_type="RELEASE"
            ).count(),
            2,
        )
        self.assertEqual(
            self.db.query(InventoryMovement).filter_by(
                movement_type="RELEASE"
            ).count(),
            2,
        )

    def test_cancel_rejects_other_member_and_non_created_status(self):
        placed = self.place()
        other = Member(
            member_public_id="MEM-ORDER-OTHER",
            is_active=True,
            created_at=NOW,
            updated_at=NOW,
        )
        self.db.add(other)
        self.db.commit()
        with self.assertRaisesRegex(ValueError, "订单不存在"):
            self.cancel(placed.order_public_id, member_id=other.id)

        order = self.db.get(Order, placed.order_id)
        order.status = "FULFILLING"
        self.db.commit()
        with self.assertRaisesRegex(ValueError, "状态不允许取消"):
            self.cancel(placed.order_public_id)
        self.assertEqual(
            self.db.query(InventoryMovement).filter_by(
                movement_type="RELEASE"
            ).count(),
            0,
        )

    def test_cancel_rolls_back_stock_release_when_points_cannot_release(self):
        placed = self.place()
        self.db.expire_all()
        early = self.db.get(PointsGrant, self.early_grant.id)
        early.status = "EXPIRED"
        self.db.commit()

        with self.assertRaisesRegex(RuntimeError, "批次状态不允许释放"):
            self.cancel(placed.order_public_id)
        self.db.expire_all()
        self.assertEqual(self.db.get(Order, placed.order_id).status, "CREATED")
        self.assertEqual(
            self.db.query(InventoryMovement).filter_by(
                movement_type="RELEASE"
            ).count(),
            0,
        )
        self.assertEqual(
            [row.reserved_quantity for row in self.db.query(
                InventoryBalance
            ).order_by(InventoryBalance.sku_id)],
            [2, 1],
        )

    def test_cancel_fails_closed_when_reservation_evidence_is_tampered(self):
        placed = self.place()
        self.db.expire_all()
        reserve = self.db.query(InventoryMovement).filter_by(
            movement_type="RESERVE",
            sku_id=self.sku_one.id,
        ).one()
        reserve.idempotency_key = "tampered-order-reservation"
        self.db.commit()

        with self.assertRaisesRegex(RuntimeError, "预占证据不完整"):
            self.cancel(placed.order_public_id)
        self.assertEqual(
            self.db.query(InventoryMovement).filter_by(
                movement_type="RELEASE"
            ).count(),
            0,
        )

    def test_cancel_after_grant_expiry_releases_without_extending_expiry(self):
        self.early_grant.expires_at = NOW + timedelta(days=1)
        self.db.commit()
        placed = self.place()
        cancel_time = NOW + timedelta(days=2)
        self.cancel(placed.order_public_id, now=cancel_time)

        self.db.expire_all()
        early = self.db.get(PointsGrant, self.early_grant.id)
        self.assertEqual(early.expires_at, NOW + timedelta(days=1))
        self.assertEqual(early.available_points, Decimal("70.00"))
        self.assertEqual(early.reserved_points, ZERO)
        expired = expire_points_grant(
            self.db,
            grant_id=early.id,
            now=cancel_time,
        )
        self.db.commit()
        self.assertEqual(expired.expired_points, Decimal("70.00"))
        self.assertEqual(early.status, "EXPIRED")
        self.assertEqual(early.available_points, ZERO)

    def test_cancelled_order_with_tampered_release_fails_replay_closed(self):
        placed = self.place()
        self.cancel(placed.order_public_id)
        self.db.expire_all()
        release = self.db.query(PointsLedgerEntry).filter_by(
            entry_type="RELEASE"
        ).first()
        release.reason = "被篡改"
        self.db.commit()

        with self.assertRaisesRegex(RuntimeError, "积分释放证据不完整"):
            self.cancel(placed.order_public_id)


if __name__ == "__main__":
    unittest.main()
