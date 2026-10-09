import tempfile
import unittest
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.api.miniprogram_v1 import get_public_products
from app.database import Base
from app.mall.public_catalog_service import list_public_categories, list_public_products
from app.models import (
    InventoryBalance, Product, ProductCategory, ProductMedia, ProductSku,
    Supplier, User,
)


NOW = datetime(2026, 10, 9, 12, 0, 0)


class PublicCatalogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.engine = create_engine(
            f"sqlite:///{Path(self.temp.name) / 'catalog.db'}",
            connect_args={"check_same_thread": False},
        )
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine, autoflush=False)()
        self.user = User(username="u", password_hash="test", role="admin")
        self.db.add(self.user)
        self.db.flush()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()
        self.temp.cleanup()

    def category(self, name, slug, *, active=True, order=0):
        row = ProductCategory(name=name, slug=slug, is_active=active, sort_order=order)
        self.db.add(row)
        self.db.flush()
        return row

    def product(self, category, number, *, status="PUBLISHED", order=0):
        row = Product(product_public_id=f"PRD-{number}", category_id=category.id,
                      name=f"商品{number}", subtitle=None, status=status,
                      sort_order=order, published_at=NOW if status == "PUBLISHED" else None)
        self.db.add(row)
        self.db.flush()
        return row

    def sku(self, product, supplier, number, *, active=True, price="10.50", stock=0):
        row = ProductSku(product_id=product.id, supplier_id=supplier.id,
                         sku_code=f"SKU-{number}", name=f"规格{number}",
                         points_price=Decimal(price), cost_price=Decimal("5.00"),
                         is_active=active)
        self.db.add(row)
        self.db.flush()
        self.db.add(InventoryBalance(sku_id=row.id, on_hand_quantity=stock,
                                     reserved_quantity=0))
        return row

    def test_public_visibility_prices_stock_pagination_and_no_mutation(self):
        visible = self.category("有效分类", "active", order=2)
        hidden = self.category("停用分类", "hidden", active=False)
        supplier = Supplier(supplier_public_id="SUP-A", name="供应商A", is_active=True)
        inactive_supplier = Supplier(supplier_public_id="SUP-B", name="供应商B", is_active=False)
        self.db.add_all((supplier, inactive_supplier))
        self.db.flush()

        first = self.product(visible, "A", order=1)
        second = self.product(visible, "B", order=2)
        self.sku(first, supplier, "A", stock=0, price="10.50")
        self.sku(first, supplier, "A2", stock=3, price="20.00")
        self.sku(second, supplier, "B", stock=0, price="33.33")
        self.sku(self.product(visible, "DRAFT", status="DRAFT"), supplier, "D", stock=4)
        self.sku(self.product(hidden, "HIDDEN"), supplier, "H", stock=4)
        self.sku(self.product(visible, "INACTIVE-SKU"), supplier, "I", active=False)
        self.sku(self.product(visible, "INACTIVE-SUP"), inactive_supplier, "S")
        self.db.add(ProductMedia(product_id=first.id, media_role="MAIN",
                                 image_path="/uploads/mall_products/a.png",
                                 is_active=True, uploaded_by_id=self.user.id))
        self.db.commit()

        self.assertEqual(list_public_categories(self.db),
                         [{"name": "有效分类", "slug": "active"}])
        page = list_public_products(self.db, page_size=1)
        self.assertEqual(page["total"], 2)
        self.assertEqual([x["product_public_id"] for x in page["items"]], ["PRD-A"])
        self.assertEqual(page["items"][0]["min_points_price"], "10.50")
        self.assertTrue(page["items"][0]["in_stock"])
        self.assertEqual(page["items"][0]["main_image_url"],
                         "/uploads/mall_products/a.png")
        second_page = list_public_products(self.db, category_slug="active", page=2, page_size=1)
        self.assertEqual(second_page["items"][0]["product_public_id"], "PRD-B")
        self.assertFalse(second_page["items"][0]["in_stock"])
        self.assertEqual(second_page["items"][0]["min_points_price"], "33.33")
        self.assertNotIn("cost_price", repr(page))
        self.assertNotIn("supplier", repr(page))
        self.assertFalse(self.db.new or self.db.dirty or self.db.deleted)

    def test_empty_and_invalid_page(self):
        self.db.commit()
        self.assertEqual(list_public_products(self.db, category_slug="none")["items"], [])
        self.assertEqual(list_public_categories(self.db), [])
        for kwargs in ({"page": 0}, {"page_size": 51}, {"category_slug": ""}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                list_public_products(self.db, **kwargs)


if __name__ == "__main__":
    unittest.main()
