import tempfile
import unittest
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.admin_permissions import (
    OPERATOR,
    PRIMARY_REVIEWER,
    SUPER_ADMIN,
)
from app.database import Base
from app.mall import (
    CATALOG_PERMISSION_MESSAGE,
    SUPPLIER_PERMISSION_MESSAGE,
    create_product,
    create_product_category,
    create_product_sku,
    create_supplier,
    publish_product,
    unpublish_product,
    update_product,
    update_product_category,
    update_product_sku,
    update_supplier,
)
from app.models import (
    AdminActionLog,
    Product,
    ProductCategory,
    ProductSku,
    Supplier,
    User,
)


NOW = datetime(2026, 9, 8, 20, 0, 0)


class CatalogServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "catalog-service.db"
        self.engine = create_engine(
            f"sqlite:///{self.path}",
            connect_args={"check_same_thread": False},
        )
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(
            bind=self.engine,
            autoflush=False,
            expire_on_commit=False,
        )
        self.db = self.Session()
        self.super_admin = self.add_user(
            "catalog-super", role="admin", admin_level=SUPER_ADMIN
        )
        self.operator = self.add_user(
            "catalog-operator", role="admin", admin_level=OPERATOR
        )
        self.reviewer = self.add_user(
            "catalog-reviewer", role="admin", admin_level=PRIMARY_REVIEWER
        )
        self.inactive_operator = self.add_user(
            "catalog-inactive",
            role="admin",
            admin_level=OPERATOR,
            is_active=False,
        )
        self.partner = self.add_user("catalog-partner", role="partner")
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()
        self.temp.cleanup()

    def add_user(self, username, *, role, admin_level=None, is_active=True):
        user = User(
            username=username,
            password_hash="test-only",
            role=role,
            admin_level=admin_level,
            is_active=is_active,
        )
        self.db.add(user)
        self.db.flush()
        return user

    def create_category(self, **overrides):
        values = {
            "actor_admin_id": self.operator.id,
            "name": "车主服务",
            "slug": "owner-services",
            "description": "商城商品分类",
            "sort_order": 10,
            "now": NOW,
        }
        values.update(overrides)
        return create_product_category(self.db, **values).entity

    def create_supplier(self, **overrides):
        values = {
            "actor_admin_id": self.operator.id,
            "name": "测试供应商",
            "contact_name": "张三",
            "contact_phone": "13800000000",
            "now": NOW,
        }
        values.update(overrides)
        return create_supplier(self.db, **values).entity

    def create_product(self, category, **overrides):
        values = {
            "actor_admin_id": self.operator.id,
            "category_id": category.id,
            "name": "基础保养服务",
            "subtitle": "纯积分兑换",
            "sort_order": 5,
            "now": NOW,
        }
        values.update(overrides)
        return create_product(self.db, **values).entity

    def create_sku(self, product, supplier, **overrides):
        values = {
            "actor_admin_id": self.operator.id,
            "product_id": product.id,
            "supplier_id": supplier.id,
            "sku_code": "svc-basic-001",
            "name": "标准套餐",
            "supplier_sku_code": "vendor-001",
            "points_price": "199.995",
            "cost_price": "88.885",
            "low_stock_threshold": 5,
            "sort_order": 2,
            "now": NOW,
        }
        values.update(overrides)
        return create_product_sku(self.db, **values).entity

    def build_catalog(self):
        category = self.create_category()
        supplier = self.create_supplier()
        product = self.create_product(category)
        sku = self.create_sku(product, supplier)
        return category, supplier, product, sku

    def test_operator_creates_complete_draft_catalog_with_audit(self):
        category, supplier, product, sku = self.build_catalog()
        self.db.commit()

        self.assertEqual(category.slug, "owner-services")
        self.assertRegex(supplier.supplier_public_id, r"^SUP-[A-Z2-9]{12}$")
        self.assertRegex(product.product_public_id, r"^PRD-[A-Z2-9]{12}$")
        self.assertEqual(product.status, "DRAFT")
        self.assertIsNone(product.published_at)
        self.assertEqual(sku.sku_code, "SVC-BASIC-001")
        self.assertEqual(sku.supplier_sku_code, "VENDOR-001")
        self.assertEqual(sku.points_price, Decimal("200.00"))
        self.assertEqual(sku.cost_price, Decimal("88.89"))
        self.assertEqual(
            [row.action_type for row in self.db.query(AdminActionLog).order_by(
                AdminActionLog.id
            )],
            [
                "mall_category_create",
                "mall_supplier_create",
                "mall_product_create",
                "mall_sku_create",
            ],
        )

    def test_super_admin_can_manage_catalog_and_suppliers(self):
        category = self.create_category(actor_admin_id=self.super_admin.id)
        supplier = self.create_supplier(actor_admin_id=self.super_admin.id)
        product = self.create_product(
            category, actor_admin_id=self.super_admin.id
        )
        self.create_sku(
            product, supplier, actor_admin_id=self.super_admin.id
        )
        self.assertEqual(self.db.query(AdminActionLog).count(), 4)

    def test_unauthorized_or_inactive_accounts_fail_closed(self):
        for actor in (
            self.reviewer,
            self.partner,
            self.inactive_operator,
            None,
        ):
            with self.subTest(actor=actor):
                actor_id = 999999 if actor is None else actor.id
                with self.assertRaisesRegex(
                    PermissionError, CATALOG_PERMISSION_MESSAGE
                ):
                    create_product_category(
                        self.db,
                        actor_admin_id=actor_id,
                        name=f"无权分类-{actor_id}",
                        slug=f"blocked-{actor_id}",
                    )
                with self.assertRaisesRegex(
                    PermissionError, SUPPLIER_PERMISSION_MESSAGE
                ):
                    create_supplier(
                        self.db,
                        actor_admin_id=actor_id,
                        name=f"无权供应商-{actor_id}",
                    )
        self.assertEqual(self.db.query(ProductCategory).count(), 0)
        self.assertEqual(self.db.query(Supplier).count(), 0)
        self.assertEqual(self.db.query(AdminActionLog).count(), 0)

    def test_created_product_is_always_draft_and_ids_are_service_owned(self):
        category = self.create_category()
        with self.assertRaises(TypeError):
            create_product(
                self.db,
                actor_admin_id=self.operator.id,
                category_id=category.id,
                name="非法指定状态",
                status="PUBLISHED",
            )
        product = self.create_product(category)
        with self.assertRaises(TypeError):
            update_product(
                self.db,
                product_id=product.id,
                actor_admin_id=self.operator.id,
                product_public_id="PRD-FORGED",
            )
        self.assertEqual(product.status, "DRAFT")

    def test_product_requires_active_existing_category(self):
        category = self.create_category(is_active=False)
        with self.assertRaisesRegex(ValueError, "启用中的分类"):
            self.create_product(category)
        with self.assertRaisesRegex(ValueError, "商品分类编号不存在"):
            create_product(
                self.db,
                actor_admin_id=self.operator.id,
                category_id=999999,
                name="不存在分类",
            )

    def test_publish_requires_active_sellable_sku(self):
        category = self.create_category()
        supplier = self.create_supplier()
        product = self.create_product(category)
        with self.assertRaisesRegex(ValueError, "至少需要一个"):
            publish_product(
                self.db,
                product_id=product.id,
                actor_admin_id=self.operator.id,
                now=NOW,
            )
        self.create_sku(product, supplier, is_active=False)
        with self.assertRaisesRegex(ValueError, "至少需要一个"):
            publish_product(
                self.db,
                product_id=product.id,
                actor_admin_id=self.operator.id,
                now=NOW,
            )

    def test_publish_unpublish_and_republish_record_transitions(self):
        _category, _supplier, product, _sku = self.build_catalog()
        published = publish_product(
            self.db,
            product_id=product.id,
            actor_admin_id=self.operator.id,
            now=NOW,
        )
        self.assertTrue(published.changed)
        self.assertEqual(product.status, "PUBLISHED")
        self.assertEqual(product.published_at, NOW)
        log_count = self.db.query(AdminActionLog).count()
        replay = publish_product(
            self.db,
            product_id=product.id,
            actor_admin_id=self.operator.id,
            now=NOW + timedelta(minutes=1),
        )
        self.assertFalse(replay.changed)
        self.assertEqual(self.db.query(AdminActionLog).count(), log_count)

        unpublished = unpublish_product(
            self.db,
            product_id=product.id,
            actor_admin_id=self.operator.id,
            now=NOW + timedelta(minutes=2),
        )
        self.assertTrue(unpublished.changed)
        self.assertEqual(product.status, "UNPUBLISHED")
        self.assertEqual(product.published_at, NOW)
        republished_at = NOW + timedelta(minutes=3)
        publish_product(
            self.db,
            product_id=product.id,
            actor_admin_id=self.operator.id,
            now=republished_at,
        )
        self.assertEqual(product.published_at, republished_at)
        self.assertEqual(
            [
                row.action_type
                for row in self.db.query(AdminActionLog)
                .filter(AdminActionLog.target_type == "product")
                .order_by(AdminActionLog.id)
            ],
            [
                "mall_product_create",
                "mall_product_publish",
                "mall_product_unpublish",
                "mall_product_publish",
            ],
        )

    def test_draft_product_cannot_be_unpublished(self):
        category = self.create_category()
        product = self.create_product(category)
        with self.assertRaisesRegex(ValueError, "只有已上架商品"):
            unpublish_product(
                self.db,
                product_id=product.id,
                actor_admin_id=self.operator.id,
            )
        self.assertEqual(product.status, "DRAFT")

    def test_published_catalog_cannot_lose_active_category_or_supplier(self):
        category, supplier, product, _sku = self.build_catalog()
        publish_product(
            self.db,
            product_id=product.id,
            actor_admin_id=self.operator.id,
        )
        with self.assertRaisesRegex(ValueError, "分类不能停用"):
            update_product_category(
                self.db,
                category_id=category.id,
                actor_admin_id=self.operator.id,
                is_active=False,
            )
        with self.assertRaisesRegex(ValueError, "供应商不能停用"):
            update_supplier(
                self.db,
                supplier_id=supplier.id,
                actor_admin_id=self.operator.id,
                is_active=False,
            )
        self.assertTrue(category.is_active)
        self.assertTrue(supplier.is_active)

    def test_published_product_cannot_lose_last_active_sku(self):
        _category, supplier, product, sku = self.build_catalog()
        publish_product(
            self.db,
            product_id=product.id,
            actor_admin_id=self.operator.id,
        )
        with self.assertRaisesRegex(ValueError, "最后一个启用 SKU"):
            update_product_sku(
                self.db,
                sku_id=sku.id,
                actor_admin_id=self.operator.id,
                is_active=False,
            )
        second = self.create_sku(
            product,
            supplier,
            sku_code="SVC-BASIC-002",
            name="升级套餐",
            supplier_sku_code="VENDOR-002",
        )
        result = update_product_sku(
            self.db,
            sku_id=sku.id,
            actor_admin_id=self.operator.id,
            is_active=False,
        )
        self.assertTrue(result.changed)
        self.assertFalse(sku.is_active)
        self.assertTrue(second.is_active)

    def test_draft_links_may_be_disabled_but_cannot_be_published(self):
        category, supplier, product, _sku = self.build_catalog()
        update_supplier(
            self.db,
            supplier_id=supplier.id,
            actor_admin_id=self.operator.id,
            is_active=False,
        )
        with self.assertRaisesRegex(ValueError, "至少需要一个"):
            publish_product(
                self.db,
                product_id=product.id,
                actor_admin_id=self.operator.id,
            )
        update_product_category(
            self.db,
            category_id=category.id,
            actor_admin_id=self.operator.id,
            is_active=False,
        )
        with self.assertRaisesRegex(ValueError, "停用分类"):
            publish_product(
                self.db,
                product_id=product.id,
                actor_admin_id=self.operator.id,
            )

    def test_updates_record_only_actual_changed_fields(self):
        category, supplier, product, sku = self.build_catalog()
        category_result = update_product_category(
            self.db,
            category_id=category.id,
            actor_admin_id=self.operator.id,
            description=None,
            sort_order=3,
        )
        supplier_result = update_supplier(
            self.db,
            supplier_id=supplier.id,
            actor_admin_id=self.operator.id,
            contact_name="李四",
            remark="新的结算联系人",
        )
        product_result = update_product(
            self.db,
            product_id=product.id,
            actor_admin_id=self.operator.id,
            name="升级保养服务",
            subtitle=None,
        )
        sku_result = update_product_sku(
            self.db,
            sku_id=sku.id,
            actor_admin_id=self.operator.id,
            points_price="188.886",
            cost_price="80",
            low_stock_threshold=8,
        )
        self.assertEqual(
            category_result.changed_fields, ("description", "sort_order")
        )
        self.assertEqual(
            supplier_result.changed_fields, ("contact_name", "remark")
        )
        self.assertEqual(product_result.changed_fields, ("name", "subtitle"))
        self.assertEqual(
            sku_result.changed_fields,
            ("points_price", "cost_price", "low_stock_threshold"),
        )
        self.assertEqual(sku.points_price, Decimal("188.89"))
        self.assertEqual(sku.cost_price, Decimal("80.00"))
        self.assertIn("points_price", sku_result.action_log.description)

    def test_noop_updates_do_not_add_audit_log(self):
        category, supplier, product, sku = self.build_catalog()
        original_log_count = self.db.query(AdminActionLog).count()
        results = (
            update_product_category(
                self.db,
                category_id=category.id,
                actor_admin_id=self.operator.id,
            ),
            update_supplier(
                self.db,
                supplier_id=supplier.id,
                actor_admin_id=self.operator.id,
            ),
            update_product(
                self.db,
                product_id=product.id,
                actor_admin_id=self.operator.id,
            ),
            update_product_sku(
                self.db,
                sku_id=sku.id,
                actor_admin_id=self.operator.id,
            ),
        )
        self.assertTrue(all(not result.changed for result in results))
        self.assertTrue(all(result.action_log is None for result in results))
        self.assertEqual(self.db.query(AdminActionLog).count(), original_log_count)

    def test_unique_fields_fail_with_controlled_messages(self):
        category, supplier, product, _sku = self.build_catalog()
        with self.assertRaisesRegex(ValueError, "分类名称已存在"):
            self.create_category(slug="another-slug")
        with self.assertRaisesRegex(ValueError, "分类 slug 已存在"):
            self.create_category(name="另一个分类")
        with self.assertRaisesRegex(ValueError, "供应商名称已存在"):
            self.create_supplier()
        with self.assertRaisesRegex(ValueError, "SKU 编码已存在"):
            self.create_sku(
                product,
                supplier,
                name="另一个套餐",
                supplier_sku_code="VENDOR-003",
            )
        with self.assertRaisesRegex(ValueError, "SKU 名称已存在"):
            self.create_sku(
                product,
                supplier,
                sku_code="SVC-BASIC-003",
                supplier_sku_code="VENDOR-004",
            )
        with self.assertRaisesRegex(ValueError, "供应商货号已被使用"):
            self.create_sku(
                product,
                supplier,
                sku_code="SVC-BASIC-004",
                name="另一个套餐",
            )
        self.assertEqual(self.db.query(ProductCategory).count(), 1)
        self.assertEqual(self.db.query(Supplier).count(), 1)
        self.assertEqual(self.db.query(ProductSku).count(), 1)
        self.assertEqual(category.name, "车主服务")

    def test_validation_rejects_invalid_slug_price_cost_and_sort(self):
        for slug in ("包含中文", "two--hyphens", "has space", ""):
            with self.subTest(slug=slug):
                with self.assertRaises(ValueError):
                    self.create_category(name=f"分类-{slug}", slug=slug)
        category = self.create_category()
        supplier = self.create_supplier()
        product = self.create_product(category)
        invalid_skus = (
            {"points_price": "0"},
            {"points_price": "-1"},
            {"cost_price": "-0.01"},
            {"low_stock_threshold": -1},
            {"sort_order": True},
            {"is_active": 1},
        )
        for number, override in enumerate(invalid_skus, start=1):
            with self.subTest(override=override):
                values = {
                    "sku_code": f"INVALID-{number}",
                    "name": f"非法 SKU {number}",
                    "supplier_sku_code": f"INVALID-VENDOR-{number}",
                }
                values.update(override)
                with self.assertRaises(ValueError):
                    self.create_sku(product, supplier, **values)
        self.assertEqual(self.db.query(ProductSku).count(), 0)

    def test_sku_product_and_code_are_immutable_through_update(self):
        _category, _supplier, _product, sku = self.build_catalog()
        with self.assertRaises(TypeError):
            update_product_sku(
                self.db,
                sku_id=sku.id,
                actor_admin_id=self.operator.id,
                sku_code="REPLACED-CODE",
            )
        with self.assertRaises(TypeError):
            update_product_sku(
                self.db,
                sku_id=sku.id,
                actor_admin_id=self.operator.id,
                product_id=999,
            )
        self.assertEqual(sku.sku_code, "SVC-BASIC-001")

    def test_supplier_change_requires_active_supplier(self):
        _category, _supplier, _product, sku = self.build_catalog()
        inactive = self.create_supplier(
            name="停用供应商", is_active=False
        )
        with self.assertRaisesRegex(ValueError, "启用中的供应商"):
            update_product_sku(
                self.db,
                sku_id=sku.id,
                actor_admin_id=self.operator.id,
                supplier_id=inactive.id,
            )

    def test_entity_and_audit_share_callers_transaction(self):
        result = create_product_category(
            self.db,
            actor_admin_id=self.operator.id,
            name="待回滚分类",
            slug="rollback-category",
        )
        self.assertIsNotNone(result.entity.id)
        self.assertIsNotNone(result.action_log.id)
        self.db.rollback()
        self.assertEqual(self.db.query(ProductCategory).count(), 0)
        self.assertEqual(self.db.query(AdminActionLog).count(), 0)

    def test_public_ids_and_sku_code_remain_stable_after_updates(self):
        category, supplier, product, sku = self.build_catalog()
        original = (
            supplier.supplier_public_id,
            product.product_public_id,
            sku.sku_code,
        )
        update_supplier(
            self.db,
            supplier_id=supplier.id,
            actor_admin_id=self.operator.id,
            name="改名供应商",
        )
        update_product(
            self.db,
            product_id=product.id,
            actor_admin_id=self.operator.id,
            name="改名商品",
        )
        update_product_sku(
            self.db,
            sku_id=sku.id,
            actor_admin_id=self.operator.id,
            name="改名套餐",
        )
        self.assertEqual(
            (
                supplier.supplier_public_id,
                product.product_public_id,
                sku.sku_code,
            ),
            original,
        )
        self.assertEqual(category.slug, "owner-services")


if __name__ == "__main__":
    unittest.main()
