import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.admin_permissions import OPERATOR, PRIMARY_REVIEWER
from app.database import Base
from app.mall import (
    CATALOG_PERMISSION_MESSAGE,
    delete_product_media_record,
    save_product_media_record,
    update_product_media_record,
)
from app.models import (
    AdminActionLog,
    Product,
    ProductCategory,
    ProductMedia,
    User,
)


NOW = datetime(2026, 9, 10, 12, 0, 0)


class ProductMediaServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "product-media.db"
        self.engine = create_engine(f"sqlite:///{self.path}")
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(
            bind=self.engine,
            autoflush=False,
            expire_on_commit=False,
        )
        self.db = self.Session()
        self.operator = self.add_user("media-operator", OPERATOR)
        self.reviewer = self.add_user("media-reviewer", PRIMARY_REVIEWER)
        self.category = ProductCategory(
            name="车载用品",
            slug="car-goods",
            is_active=True,
            created_at=NOW,
            updated_at=NOW,
        )
        self.db.add(self.category)
        self.db.flush()
        self.product = Product(
            product_public_id="PRD-ABC234",
            category_id=self.category.id,
            name="车载急救包",
            status="DRAFT",
            created_at=NOW,
            updated_at=NOW,
        )
        self.db.add(self.product)
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()
        self.temp.cleanup()

    def add_user(self, username, admin_level):
        user = User(
            username=username,
            password_hash="test-only",
            role="admin",
            admin_level=admin_level,
            is_active=True,
        )
        self.db.add(user)
        self.db.flush()
        return user

    def image_path(self, filename):
        return f"/uploads/mall_products/PRD-ABC234/{filename}.webp"

    def test_operator_creates_multiple_roles_with_audit(self):
        for role, filename in (
            ("MAIN", "main_a"),
            ("CAROUSEL", "carousel_a"),
            ("CAROUSEL", "carousel_b"),
            ("DETAIL", "detail_a"),
        ):
            save_product_media_record(
                self.db,
                product_id=self.product.id,
                actor_admin_id=self.operator.id,
                media_role=role,
                image_path=self.image_path(filename),
                alt_text="测试图片",
                sort_order=1,
                now=NOW,
            )
        self.db.commit()
        self.assertEqual(self.db.query(ProductMedia).count(), 4)
        self.assertEqual(self.db.query(AdminActionLog).count(), 4)

    def test_new_main_replaces_record_and_returns_previous_path(self):
        first = save_product_media_record(
            self.db,
            product_id=self.product.id,
            actor_admin_id=self.operator.id,
            media_role="MAIN",
            image_path=self.image_path("main_old"),
            now=NOW,
        )
        second = save_product_media_record(
            self.db,
            product_id=self.product.id,
            actor_admin_id=self.operator.id,
            media_role="MAIN",
            image_path=self.image_path("main_new"),
            alt_text="新主图",
            now=NOW,
        )
        self.db.commit()
        self.assertEqual(first.entity.id, second.entity.id)
        self.assertEqual(second.previous_image_path, self.image_path("main_old"))
        self.assertEqual(self.db.query(ProductMedia).count(), 1)
        self.assertEqual(second.entity.alt_text, "新主图")
        self.assertEqual(
            [row.action_type for row in self.db.query(AdminActionLog).order_by(
                AdminActionLog.id
            )],
            ["mall_product_media_create", "mall_product_media_update"],
        )

    def test_update_is_audited_and_noop_is_not(self):
        media = save_product_media_record(
            self.db,
            product_id=self.product.id,
            actor_admin_id=self.operator.id,
            media_role="DETAIL",
            image_path=self.image_path("detail"),
            alt_text="原说明",
            sort_order=3,
            now=NOW,
        ).entity
        updated = update_product_media_record(
            self.db,
            media_id=media.id,
            actor_admin_id=self.operator.id,
            alt_text="新说明",
            sort_order=2,
            is_active=False,
            now=NOW,
        )
        unchanged = update_product_media_record(
            self.db,
            media_id=media.id,
            actor_admin_id=self.operator.id,
            alt_text="新说明",
            sort_order=2,
            is_active=False,
            now=NOW,
        )
        self.assertTrue(updated.changed)
        self.assertFalse(unchanged.changed)
        self.assertEqual(self.db.query(AdminActionLog).count(), 2)

    def test_delete_returns_path_and_audits(self):
        media = save_product_media_record(
            self.db,
            product_id=self.product.id,
            actor_admin_id=self.operator.id,
            media_role="DETAIL",
            image_path=self.image_path("delete_me"),
            now=NOW,
        ).entity
        result = delete_product_media_record(
            self.db,
            media_id=media.id,
            actor_admin_id=self.operator.id,
            now=NOW,
        )
        self.db.commit()
        self.assertEqual(result.previous_image_path, self.image_path("delete_me"))
        self.assertEqual(self.db.query(ProductMedia).count(), 0)
        self.assertEqual(self.db.query(AdminActionLog).count(), 2)

    def test_reviewer_and_cross_product_path_fail_closed(self):
        with self.assertRaisesRegex(PermissionError, CATALOG_PERMISSION_MESSAGE):
            save_product_media_record(
                self.db,
                product_id=self.product.id,
                actor_admin_id=self.reviewer.id,
                media_role="MAIN",
                image_path=self.image_path("blocked"),
                now=NOW,
            )
        with self.assertRaisesRegex(ValueError, "不属于当前商品"):
            save_product_media_record(
                self.db,
                product_id=self.product.id,
                actor_admin_id=self.operator.id,
                media_role="MAIN",
                image_path="/uploads/mall_products/PRD-OTHER/main.webp",
                now=NOW,
            )
        with self.assertRaisesRegex(ValueError, "不属于当前商品"):
            save_product_media_record(
                self.db,
                product_id=self.product.id,
                actor_admin_id=self.operator.id,
                media_role="MAIN",
                image_path=(
                    "/uploads/mall_products/PRD-ABC234/../PRD-OTHER/main.webp"
                ),
                now=NOW,
            )
        with self.assertRaisesRegex(ValueError, "不属于当前商品"):
            save_product_media_record(
                self.db,
                product_id=self.product.id,
                actor_admin_id=self.operator.id,
                media_role="MAIN",
                image_path=(
                    "/uploads/mall_products/PRD-ABC234/"
                    "..\\PRD-OTHER\\main.webp"
                ),
                now=NOW,
            )
        self.assertEqual(self.db.query(ProductMedia).count(), 0)


if __name__ == "__main__":
    unittest.main()
