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
    execute_order_completion,
    execute_order_fulfillment,
    execute_order_placement,
    execute_order_refund,
    execute_order_shipping,
    execute_supplier_settlement_generation,
    expire_points_grant,
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

    def fulfill(self, order_public_id, *, actor_admin_id=None, now=NOW):
        return execute_order_fulfillment(
            self.engine,
            actor_admin_id=(
                self.operator.id
                if actor_admin_id is None
                else actor_admin_id
            ),
            order_public_id=order_public_id,
            now=now,
        )

    def ship(
        self,
        order_public_id,
        *,
        actor_admin_id=None,
        carrier="顺丰速运",
        tracking_number="SF-M5-5-0001",
        now=NOW + timedelta(minutes=1),
    ):
        return execute_order_shipping(
            self.engine,
            actor_admin_id=(
                self.operator.id
                if actor_admin_id is None
                else actor_admin_id
            ),
            order_public_id=order_public_id,
            shipping_carrier=carrier,
            tracking_number=tracking_number,
            now=now,
        )

    def complete(
        self,
        order_public_id,
        *,
        actor_admin_id=None,
        now=NOW + timedelta(minutes=2),
    ):
        return execute_order_completion(
            self.engine,
            actor_admin_id=(
                self.operator.id
                if actor_admin_id is None
                else actor_admin_id
            ),
            order_public_id=order_public_id,
            now=now,
        )

    def refund(
        self,
        order_public_id,
        *,
        actor_admin_id=None,
        reason="客户确认整单退货",
        now=NOW + timedelta(minutes=3),
    ):
        return execute_order_refund(
            self.engine,
            actor_admin_id=(
                self.operator.id
                if actor_admin_id is None
                else actor_admin_id
            ),
            order_public_id=order_public_id,
            reason=reason,
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

    def test_fulfills_created_order_consumes_points_and_outbounds_stock(self):
        placed = self.place()
        result = self.fulfill(placed.order_public_id)

        self.assertFalse(result.replayed)
        self.assertEqual(result.status, "FULFILLING")
        self.assertEqual(result.consumed_points, Decimal("105.00"))
        self.assertEqual(len(result.points_consume_entry_ids), 2)
        self.assertEqual(len(result.inventory_outbound_movement_ids), 2)

        self.db.expire_all()
        order = self.db.get(Order, placed.order_id)
        self.assertEqual(order.status, "FULFILLING")
        account = self.db.get(PointsAccount, self.account.id)
        self.assertEqual(account.available_points, Decimal("65.00"))
        self.assertEqual(account.reserved_points, ZERO)
        self.assertEqual(account.version, 4)
        grants = self.db.query(PointsGrant).order_by(
            PointsGrant.expires_at
        ).all()
        self.assertEqual(
            [
                (row.available_points, row.reserved_points, row.status)
                for row in grants
            ],
            [
                (ZERO, ZERO, "EXHAUSTED"),
                (Decimal("65.00"), ZERO, "ACTIVE"),
            ],
        )
        consume_entries = self.db.query(PointsLedgerEntry).filter_by(
            entry_type="CONSUME"
        ).order_by(PointsLedgerEntry.id).all()
        self.assertEqual(len(consume_entries), 2)
        self.assertEqual(
            [row.available_points_delta for row in consume_entries],
            [ZERO, ZERO],
        )
        self.assertEqual(
            [row.reserved_points_delta for row in consume_entries],
            [Decimal("-70.00"), Decimal("-35.00")],
        )
        self.assertTrue(
            all(row.actor_admin_id == self.operator.id for row in consume_entries)
        )

        balances = self.db.query(InventoryBalance).order_by(
            InventoryBalance.sku_id
        ).all()
        self.assertEqual(
            [(row.on_hand_quantity, row.reserved_quantity) for row in balances],
            [(3, 0), (3, 0)],
        )
        outbounds = self.db.query(InventoryMovement).filter_by(
            movement_type="OUTBOUND"
        ).order_by(InventoryMovement.sku_id).all()
        self.assertEqual(
            [row.quantity_delta for row in outbounds],
            [-2, -1],
        )
        self.assertEqual(
            [row.reserved_quantity_delta for row in outbounds],
            [-2, -1],
        )
        self.assertTrue(
            all(row.actor_admin_id == self.operator.id for row in outbounds)
        )
        action_log = self.db.get(AdminActionLog, result.action_log_id)
        self.assertEqual(action_log.action_type, "mall_order_fulfill")
        self.assertEqual(action_log.target_type, "mall_order")
        self.assertEqual(action_log.target_id, placed.order_id)

    def test_fulfillment_replay_does_not_duplicate_evidence(self):
        placed = self.place()
        first = self.fulfill(placed.order_public_id)
        replay = self.fulfill(placed.order_public_id)

        self.assertFalse(first.replayed)
        self.assertTrue(replay.replayed)
        self.assertEqual(replay.action_log_id, first.action_log_id)
        self.assertEqual(
            replay.points_consume_entry_ids,
            first.points_consume_entry_ids,
        )
        self.assertEqual(
            replay.inventory_outbound_movement_ids,
            first.inventory_outbound_movement_ids,
        )
        self.assertEqual(
            self.db.query(PointsLedgerEntry).filter_by(
                entry_type="CONSUME"
            ).count(),
            2,
        )
        self.assertEqual(
            self.db.query(InventoryMovement).filter_by(
                movement_type="OUTBOUND"
            ).count(),
            2,
        )
        self.assertEqual(
            self.db.query(AdminActionLog).filter_by(
                action_type="mall_order_fulfill"
            ).count(),
            1,
        )

    def test_concurrent_fulfillment_creates_one_evidence_set(self):
        placed = self.place()
        barrier = Barrier(2)

        def fulfill_once():
            barrier.wait()
            return self.fulfill(placed.order_public_id)

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(fulfill_once) for _ in range(2)]
            outcomes = [future.result() for future in futures]

        self.assertEqual(
            sorted(result.replayed for result in outcomes),
            [False, True],
        )
        self.assertEqual(
            self.db.query(PointsLedgerEntry).filter_by(
                entry_type="CONSUME"
            ).count(),
            2,
        )
        self.assertEqual(
            self.db.query(InventoryMovement).filter_by(
                movement_type="OUTBOUND"
            ).count(),
            2,
        )

    def test_fulfillment_rejects_unauthorized_actor_and_cancelled_order(self):
        placed = self.place()
        with self.assertRaisesRegex(PermissionError, "无权确认商城订单履约"):
            self.fulfill(
                placed.order_public_id,
                actor_admin_id=self.partner.id,
            )

        self.cancel(placed.order_public_id)
        with self.assertRaisesRegex(ValueError, "状态不允许确认履约"):
            self.fulfill(placed.order_public_id)
        self.assertEqual(
            self.db.query(InventoryMovement).filter_by(
                movement_type="OUTBOUND"
            ).count(),
            0,
        )

    def test_fulfillment_rolls_back_outbound_when_points_are_frozen(self):
        placed = self.place()
        self.db.expire_all()
        early = self.db.get(PointsGrant, self.early_grant.id)
        early.status = "FROZEN"
        self.db.commit()

        with self.assertRaisesRegex(RuntimeError, "批次状态不允许消费"):
            self.fulfill(placed.order_public_id)
        self.db.expire_all()
        self.assertEqual(self.db.get(Order, placed.order_id).status, "CREATED")
        self.assertEqual(
            self.db.query(InventoryMovement).filter_by(
                movement_type="OUTBOUND"
            ).count(),
            0,
        )
        self.assertEqual(
            self.db.query(PointsLedgerEntry).filter_by(
                entry_type="CONSUME"
            ).count(),
            0,
        )
        self.assertEqual(
            [
                row.reserved_quantity
                for row in self.db.query(InventoryBalance).order_by(
                    InventoryBalance.sku_id
                )
            ],
            [2, 1],
        )

    def test_fulfillment_fails_closed_for_tampered_reservation(self):
        placed = self.place()
        self.db.expire_all()
        reserve = self.db.query(PointsLedgerEntry).filter_by(
            entry_type="RESERVE"
        ).first()
        reserve.reason = "被篡改"
        self.db.commit()

        with self.assertRaisesRegex(RuntimeError, "积分预占证据不完整"):
            self.fulfill(placed.order_public_id)
        self.assertEqual(
            self.db.query(InventoryMovement).filter_by(
                movement_type="OUTBOUND"
            ).count(),
            0,
        )

    def test_fulfillment_consumes_reservation_after_original_expiry_time(self):
        self.early_grant.expires_at = NOW + timedelta(days=1)
        self.db.commit()
        placed = self.place()
        result = self.fulfill(
            placed.order_public_id,
            now=NOW + timedelta(days=2),
        )

        self.assertEqual(result.status, "FULFILLING")
        self.db.expire_all()
        early = self.db.get(PointsGrant, self.early_grant.id)
        self.assertEqual(early.expires_at, NOW + timedelta(days=1))
        self.assertEqual(early.available_points, ZERO)
        self.assertEqual(early.reserved_points, ZERO)
        self.assertEqual(early.status, "EXHAUSTED")

    def test_fulfilled_order_with_tampered_evidence_fails_replay_closed(self):
        placed = self.place()
        self.fulfill(placed.order_public_id)
        self.db.expire_all()
        outbound = self.db.query(InventoryMovement).filter_by(
            movement_type="OUTBOUND"
        ).first()
        outbound.reason = "被篡改"
        self.db.commit()

        with self.assertRaisesRegex(RuntimeError, "库存出库证据不完整"):
            self.fulfill(placed.order_public_id)

    def test_ships_fulfilling_order_with_manual_logistics_and_audit(self):
        placed = self.place()
        self.fulfill(placed.order_public_id)
        result = self.ship(placed.order_public_id)

        self.assertFalse(result.replayed)
        self.assertEqual(result.status, "SHIPPED")
        self.assertEqual(result.shipping_carrier, "顺丰速运")
        self.assertEqual(result.tracking_number, "SF-M5-5-0001")
        self.assertEqual(result.shipped_at, NOW + timedelta(minutes=1))
        self.db.expire_all()
        order = self.db.get(Order, placed.order_id)
        self.assertEqual(order.status, "SHIPPED")
        self.assertEqual(order.shipping_carrier, "顺丰速运")
        self.assertEqual(order.tracking_number, "SF-M5-5-0001")
        self.assertIsNone(order.completed_at)
        action_log = self.db.get(AdminActionLog, result.action_log_id)
        self.assertEqual(action_log.action_type, "mall_order_ship")
        self.assertEqual(action_log.admin_id, self.operator.id)
        self.assertIn("顺丰速运", action_log.description)
        self.assertIn("SF-M5-5-0001", action_log.description)

    def test_shipping_exact_replay_is_stable_and_other_details_conflict(self):
        placed = self.place()
        self.fulfill(placed.order_public_id)
        first = self.ship(placed.order_public_id)
        replay = self.ship(placed.order_public_id)

        self.assertFalse(first.replayed)
        self.assertTrue(replay.replayed)
        self.assertEqual(replay.action_log_id, first.action_log_id)
        self.assertEqual(
            self.db.query(AdminActionLog).filter_by(
                action_type="mall_order_ship"
            ).count(),
            1,
        )
        with self.assertRaisesRegex(ValueError, "其他物流信息"):
            self.ship(
                placed.order_public_id,
                tracking_number="SF-M5-5-OTHER",
            )

    def test_concurrent_shipping_creates_one_audit_evidence(self):
        placed = self.place()
        self.fulfill(placed.order_public_id)
        barrier = Barrier(2)

        def ship_once():
            barrier.wait()
            return self.ship(placed.order_public_id)

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(ship_once) for _ in range(2)]
            outcomes = [future.result() for future in futures]

        self.assertEqual(
            sorted(result.replayed for result in outcomes),
            [False, True],
        )
        self.assertEqual(
            self.db.query(AdminActionLog).filter_by(
                action_type="mall_order_ship"
            ).count(),
            1,
        )

    def test_shipping_rejects_unauthorized_wrong_state_and_bad_time(self):
        placed = self.place()
        with self.assertRaisesRegex(ValueError, "状态不允许发货"):
            self.ship(placed.order_public_id)
        self.fulfill(placed.order_public_id)
        with self.assertRaisesRegex(PermissionError, "无权执行商城订单发货"):
            self.ship(
                placed.order_public_id,
                actor_admin_id=self.partner.id,
            )
        with self.assertRaisesRegex(ValueError, "不能早于"):
            self.ship(
                placed.order_public_id,
                now=NOW - timedelta(seconds=1),
            )
        self.db.expire_all()
        order = self.db.get(Order, placed.order_id)
        self.assertEqual(order.status, "FULFILLING")
        self.assertIsNone(order.shipping_carrier)

    def test_shipping_fails_closed_for_tampered_fulfillment_evidence(self):
        placed = self.place()
        self.fulfill(placed.order_public_id)
        self.db.expire_all()
        consume = self.db.query(PointsLedgerEntry).filter_by(
            entry_type="CONSUME"
        ).first()
        consume.reason = "被篡改"
        self.db.commit()

        with self.assertRaisesRegex(RuntimeError, "积分消费证据不完整"):
            self.ship(placed.order_public_id)
        self.db.expire_all()
        order = self.db.get(Order, placed.order_id)
        self.assertEqual(order.status, "FULFILLING")
        self.assertIsNone(order.shipped_at)

    def test_completes_shipped_order_and_replays_without_duplicate_audit(self):
        placed = self.place()
        self.fulfill(placed.order_public_id)
        shipping = self.ship(placed.order_public_id)
        first = self.complete(placed.order_public_id)
        replay = self.complete(placed.order_public_id)

        self.assertFalse(first.replayed)
        self.assertTrue(replay.replayed)
        self.assertEqual(first.status, "COMPLETED")
        self.assertEqual(first.shipping_action_log_id, shipping.action_log_id)
        self.assertEqual(
            replay.completion_action_log_id,
            first.completion_action_log_id,
        )
        self.assertEqual(first.completed_at, NOW + timedelta(minutes=2))
        self.db.expire_all()
        order = self.db.get(Order, placed.order_id)
        self.assertEqual(order.status, "COMPLETED")
        self.assertEqual(order.completed_at, NOW + timedelta(minutes=2))
        self.assertEqual(
            self.db.query(AdminActionLog).filter_by(
                action_type="mall_order_complete"
            ).count(),
            1,
        )

    def test_concurrent_completion_creates_one_audit_evidence(self):
        placed = self.place()
        self.fulfill(placed.order_public_id)
        self.ship(placed.order_public_id)
        barrier = Barrier(2)

        def complete_once():
            barrier.wait()
            return self.complete(placed.order_public_id)

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(complete_once) for _ in range(2)]
            outcomes = [future.result() for future in futures]

        self.assertEqual(
            sorted(result.replayed for result in outcomes),
            [False, True],
        )
        self.assertEqual(
            self.db.query(AdminActionLog).filter_by(
                action_type="mall_order_complete"
            ).count(),
            1,
        )

    def test_completion_rejects_wrong_state_unauthorized_and_bad_time(self):
        placed = self.place()
        self.fulfill(placed.order_public_id)
        with self.assertRaisesRegex(ValueError, "状态不允许确认完成"):
            self.complete(placed.order_public_id)
        self.ship(placed.order_public_id)
        with self.assertRaisesRegex(PermissionError, "无权确认商城订单完成"):
            self.complete(
                placed.order_public_id,
                actor_admin_id=self.partner.id,
            )
        with self.assertRaisesRegex(ValueError, "不能早于发货时间"):
            self.complete(
                placed.order_public_id,
                now=NOW + timedelta(seconds=30),
            )
        self.db.expire_all()
        self.assertEqual(self.db.get(Order, placed.order_id).status, "SHIPPED")

    def test_tampered_shipping_evidence_blocks_completion_and_replay(self):
        placed = self.place()
        self.fulfill(placed.order_public_id)
        self.ship(placed.order_public_id)
        self.db.expire_all()
        shipping_log = self.db.query(AdminActionLog).filter_by(
            action_type="mall_order_ship"
        ).one()
        shipping_log.description = "被篡改"
        self.db.commit()

        with self.assertRaisesRegex(RuntimeError, "发货审计证据不完整"):
            self.complete(placed.order_public_id)
        self.db.expire_all()
        order = self.db.get(Order, placed.order_id)
        self.assertEqual(order.status, "SHIPPED")
        self.assertIsNone(order.completed_at)

    def test_refunds_completed_order_and_restores_points_and_inventory(self):
        placed = self.place()
        self.fulfill(placed.order_public_id)
        self.ship(placed.order_public_id)
        self.complete(placed.order_public_id)

        result = self.refund(placed.order_public_id)

        self.assertFalse(result.replayed)
        self.assertEqual(result.status, "REFUNDED")
        self.assertEqual(result.refunded_points, Decimal("105.00"))
        self.assertEqual(result.refund_reason, "客户确认整单退货")
        self.assertEqual(result.refunded_at, NOW + timedelta(minutes=3))
        self.assertEqual(len(result.points_refund_entry_ids), 2)
        self.assertEqual(len(result.inventory_return_movement_ids), 2)

        self.db.expire_all()
        order = self.db.get(Order, placed.order_id)
        account = self.db.get(PointsAccount, self.account.id)
        grants = self.db.query(PointsGrant).order_by(
            PointsGrant.expires_at
        ).all()
        balances = self.db.query(InventoryBalance).order_by(
            InventoryBalance.sku_id
        ).all()
        self.assertEqual(order.status, "REFUNDED")
        self.assertEqual(order.refund_reason, "客户确认整单退货")
        self.assertEqual(account.available_points, Decimal("170.00"))
        self.assertEqual(account.reserved_points, ZERO)
        self.assertEqual(
            [grant.available_points for grant in grants],
            [Decimal("70.00"), Decimal("100.00")],
        )
        self.assertTrue(all(grant.status == "ACTIVE" for grant in grants))
        self.assertEqual(
            [(row.on_hand_quantity, row.reserved_quantity) for row in balances],
            [(5, 0), (4, 0)],
        )
        self.assertEqual(
            self.db.query(PointsLedgerEntry).filter_by(
                entry_type="REFUND"
            ).count(),
            2,
        )
        self.assertEqual(
            self.db.query(InventoryMovement).filter_by(
                movement_type="RETURN"
            ).count(),
            2,
        )
        self.assertEqual(
            self.db.query(AdminActionLog).filter_by(
                action_type="mall_order_refund"
            ).count(),
            1,
        )

    def test_order_in_supplier_settlement_cannot_be_automatically_refunded(self):
        placed = self.place(key="settled-order-refund-guard")
        self.fulfill(placed.order_public_id)
        self.ship(placed.order_public_id)
        self.complete(placed.order_public_id)
        execute_supplier_settlement_generation(
            self.engine,
            actor_admin_id=self.operator.id,
            supplier_id=self.supplier.id,
            period_start=NOW,
            period_end=NOW + timedelta(days=1),
            now=NOW + timedelta(days=2),
        )

        with self.assertRaisesRegex(ValueError, "已进入供应商结算"):
            self.refund(placed.order_public_id)

        self.db.expire_all()
        order = self.db.get(Order, placed.order_id)
        self.assertEqual(order.status, "COMPLETED")
        self.assertIsNone(order.refund_reason)
        self.assertEqual(
            self.db.query(PointsLedgerEntry).filter_by(
                entry_type="REFUND"
            ).count(),
            0,
        )
        self.assertEqual(
            self.db.query(InventoryMovement).filter_by(
                movement_type="RETURN"
            ).count(),
            0,
        )

    def test_refund_exact_replay_is_stable_and_other_reason_conflicts(self):
        placed = self.place()
        self.fulfill(placed.order_public_id)
        self.ship(placed.order_public_id)
        self.complete(placed.order_public_id)
        first = self.refund(placed.order_public_id)
        replay = self.refund(placed.order_public_id)

        self.assertFalse(first.replayed)
        self.assertTrue(replay.replayed)
        self.assertEqual(replay.action_log_id, first.action_log_id)
        self.assertEqual(
            replay.points_refund_entry_ids,
            first.points_refund_entry_ids,
        )
        self.assertEqual(
            replay.inventory_return_movement_ids,
            first.inventory_return_movement_ids,
        )
        with self.assertRaisesRegex(ValueError, "其他退款原因"):
            self.refund(placed.order_public_id, reason="其他原因")
        self.assertEqual(
            self.db.query(PointsLedgerEntry).filter_by(
                entry_type="REFUND"
            ).count(),
            2,
        )
        self.assertEqual(
            self.db.query(InventoryMovement).filter_by(
                movement_type="RETURN"
            ).count(),
            2,
        )

    def test_concurrent_refund_creates_one_resource_recovery_set(self):
        placed = self.place()
        self.fulfill(placed.order_public_id)
        self.ship(placed.order_public_id)
        self.complete(placed.order_public_id)
        barrier = Barrier(2)

        def refund_once():
            barrier.wait()
            return self.refund(placed.order_public_id)

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(refund_once) for _ in range(2)]
            outcomes = [future.result() for future in futures]

        self.assertEqual(
            sorted(result.replayed for result in outcomes),
            [False, True],
        )
        self.assertEqual(
            self.db.query(PointsLedgerEntry).filter_by(
                entry_type="REFUND"
            ).count(),
            2,
        )
        self.assertEqual(
            self.db.query(InventoryMovement).filter_by(
                movement_type="RETURN"
            ).count(),
            2,
        )
        self.assertEqual(
            self.db.query(AdminActionLog).filter_by(
                action_type="mall_order_refund"
            ).count(),
            1,
        )

    def test_refund_rejects_wrong_state_unauthorized_bad_time_and_reason(self):
        placed = self.place()
        with self.assertRaisesRegex(ValueError, "状态不允许退款"):
            self.refund(placed.order_public_id)
        self.fulfill(placed.order_public_id)
        self.ship(placed.order_public_id)
        self.complete(placed.order_public_id)
        with self.assertRaisesRegex(PermissionError, "无权执行商城订单退款"):
            self.refund(
                placed.order_public_id,
                actor_admin_id=self.partner.id,
            )
        with self.assertRaisesRegex(ValueError, "不能早于订单完成时间"):
            self.refund(
                placed.order_public_id,
                now=NOW + timedelta(minutes=1),
            )
        with self.assertRaisesRegex(ValueError, "退款原因不能为空"):
            self.refund(placed.order_public_id, reason="  ")
        self.db.expire_all()
        order = self.db.get(Order, placed.order_id)
        self.assertEqual(order.status, "COMPLETED")
        self.assertIsNone(order.refunded_at)

    def test_expired_original_grant_blocks_automatic_refund(self):
        placed = self.place()
        self.fulfill(placed.order_public_id)
        self.ship(placed.order_public_id)
        self.complete(placed.order_public_id)
        self.db.expire_all()
        grant = self.db.get(PointsGrant, self.early_grant.id)
        grant.expires_at = NOW + timedelta(minutes=2, seconds=30)
        self.db.commit()

        with self.assertRaisesRegex(ValueError, "已到期，不能自动退款"):
            self.refund(placed.order_public_id)

        self.db.expire_all()
        order = self.db.get(Order, placed.order_id)
        self.assertEqual(order.status, "COMPLETED")
        self.assertEqual(
            self.db.query(PointsLedgerEntry).filter_by(
                entry_type="REFUND"
            ).count(),
            0,
        )
        self.assertEqual(
            self.db.query(InventoryMovement).filter_by(
                movement_type="RETURN"
            ).count(),
            0,
        )

    def test_tampered_consumption_evidence_blocks_refund(self):
        placed = self.place()
        self.fulfill(placed.order_public_id)
        self.ship(placed.order_public_id)
        self.complete(placed.order_public_id)
        self.db.expire_all()
        consume = self.db.query(PointsLedgerEntry).filter_by(
            entry_type="CONSUME"
        ).first()
        consume.reason = "被篡改"
        self.db.commit()

        with self.assertRaisesRegex(RuntimeError, "积分消费证据不完整"):
            self.refund(placed.order_public_id)
        self.db.expire_all()
        self.assertEqual(self.db.get(Order, placed.order_id).status, "COMPLETED")
        self.assertEqual(
            self.db.query(InventoryMovement).filter_by(
                movement_type="RETURN"
            ).count(),
            0,
        )


if __name__ == "__main__":
    unittest.main()
