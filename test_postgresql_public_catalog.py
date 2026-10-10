"""仅针对独立空白 PostgreSQL _test 数据库的公开目录验收。"""

import os
import unittest
from datetime import datetime
from decimal import Decimal

from alembic import command
from sqlalchemy.orm import sessionmaker

from app.database import create_database_engine, resolve_database_url
from app.mall.public_catalog_service import (
    get_public_product_detail, list_public_categories, list_public_products,
)
from app.models import (
    InventoryBalance, Product, ProductCategory, ProductMedia, ProductSku,
    Supplier, User,
)
from test_postgresql_migration import (
    POSTGRES_TEST_ALLOW_RESET_ENV_NAME, POSTGRES_TEST_DATABASE_URL_ENV_NAME,
    build_alembic_config, get_database_state, validate_postgresql_test_database_url,
)


class PostgreSQLPublicCatalogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        configured = os.environ.get(POSTGRES_TEST_DATABASE_URL_ENV_NAME, "").strip()
        if not configured:
            raise unittest.SkipTest("未配置独立 PostgreSQL 测试数据库")
        if os.environ.get(POSTGRES_TEST_ALLOW_RESET_ENV_NAME) != "1":
            raise RuntimeError("必须显式设置 POSTGRES_TEST_ALLOW_RESET=1")
        cls.database_url = validate_postgresql_test_database_url(
            configured, resolve_database_url()
        )

    def test_public_catalog_on_disposable_database(self):
        tables, revision = get_database_state(self.database_url)
        self.assertFalse(tables - {"alembic_version"})
        self.assertIsNone(revision)
        config = build_alembic_config(self.database_url)
        upgraded = False
        engine = None
        try:
            command.upgrade(config, "head")
            upgraded = True
            engine = create_database_engine(self.database_url)
            Session = sessionmaker(bind=engine, autoflush=False)
            with Session() as db:
                user = User(username="public-catalog-test", password_hash="test", role="admin")
                db.add(user)
                category = ProductCategory(name="公开分类", slug="public")
                supplier = Supplier(supplier_public_id="SUP-PUBLIC", name="公开供应商")
                db.add_all([category, supplier])
                db.flush()
                product = Product(
                    product_public_id="PRD-PUBLIC", category_id=category.id,
                    name="公开商品", status="PUBLISHED", published_at=datetime(2026, 10, 9),
                )
                db.add(product)
                db.flush()
                sku = ProductSku(product_id=product.id, supplier_id=supplier.id,
                                 sku_code="SKU-PUBLIC", name="标准",
                                 points_price=Decimal("12.50"), cost_price=Decimal("7.00"),
                                 low_stock_threshold=2)
                db.add(sku)
                db.flush()
                db.add(InventoryBalance(sku_id=sku.id, on_hand_quantity=2,
                                        reserved_quantity=0))
                db.add(ProductMedia(product_id=product.id, media_role="MAIN",
                                    image_path="/uploads/mall_products/PRD-PUBLIC/main.webp",
                                    uploaded_by_id=user.id))
                db.commit()
                self.assertEqual(list_public_categories(db)[0]["slug"], "public")
                page = list_public_products(db, category_slug="public")
                self.assertEqual(page["total"], 1)
                self.assertEqual(page["items"][0]["min_points_price"], "12.50")
                self.assertTrue(page["items"][0]["in_stock"])
                self.assertNotIn("cost_price", repr(page))
                searched = list_public_products(db, search_term="  公开  ")
                self.assertEqual(searched["total"], 1)
                self.assertEqual(list_public_products(db, search_term="%")["total"], 0)
                detail = get_public_product_detail(db, product_public_id="PRD-PUBLIC")
                self.assertEqual(detail["min_points_price"], "12.50")
                self.assertTrue(detail["skus"][0]["in_stock"])
                self.assertEqual(detail["skus"][0]["stock_status"], "LOW_STOCK")
                self.assertEqual(detail["images"][0]["role"], "main")
                self.assertNotIn("cost_price", repr(detail))
                self.assertIsNone(get_public_product_detail(
                    db, product_public_id="PRD-NONE"))
        finally:
            if engine is not None:
                engine.dispose()
            if upgraded:
                command.downgrade(config, "base")


if __name__ == "__main__":
    unittest.main()
