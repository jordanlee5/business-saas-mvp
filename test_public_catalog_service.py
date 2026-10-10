import tempfile
import unittest
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from fastapi import HTTPException

from app.api.miniprogram_v1 import (
    PublicProductDetail, get_public_product, get_public_products,
)
from app.database import Base
from app.mall.public_catalog_service import (
    get_public_product_detail, list_public_categories, list_public_products,
)
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

    def test_search_respects_visibility_category_pagination_and_literal_wildcards(self):
        car = self.category("汽车", "car")
        home = self.category("家居", "home")
        hidden = self.category("停用", "hidden", active=False)
        supplier = Supplier(supplier_public_id="SUP-A", name="供应商A")
        inactive_supplier = Supplier(supplier_public_id="SUP-B", name="供应商B",
                                     is_active=False)
        self.db.add_all((supplier, inactive_supplier))
        self.db.flush()
        first = self.product(car, "FIRST", order=1)
        first.name = "Basic Wash"
        second = self.product(car, "SECOND", order=2)
        second.name = "Other"
        second.subtitle = "BASIC wash"
        literal = self.product(home, "LITERAL")
        literal.name = "10%_off / pack"
        for product, number in ((first, "F"), (second, "S"), (literal, "L")):
            self.sku(product, supplier, number, stock=1)
        draft = self.product(car, "DRAFT", status="DRAFT")
        draft.name = "Basic draft"
        self.sku(draft, supplier, "D")
        hidden_product = self.product(hidden, "HIDDEN")
        hidden_product.name = "Basic hidden"
        self.sku(hidden_product, supplier, "H")
        no_sku = self.product(car, "NO-SKU")
        no_sku.name = "Basic no sku"
        self.sku(no_sku, inactive_supplier, "N")
        self.db.commit()

        page = list_public_products(self.db, search_term="  bAsIc  ",
                                    category_slug="car", page_size=1)
        self.assertEqual(page["total"], 2)
        self.assertEqual([item["product_public_id"] for item in page["items"]],
                         ["PRD-FIRST"])
        second_page = get_public_products(category="car", q="BASIC", page=2,
                                          page_size=1, db=self.db)
        self.assertEqual(second_page["total"], 2)
        self.assertEqual(second_page["items"][0]["product_public_id"], "PRD-SECOND")
        for query in ("%_", "/", "10%_OFF / PACK"):
            with self.subTest(query=query):
                result = list_public_products(self.db, search_term=query)
                self.assertEqual(result["total"], 1)
                self.assertEqual(result["items"][0]["product_public_id"],
                                 "PRD-LITERAL")
        self.assertEqual(list_public_products(self.db, search_term="%")["total"], 1)
        self.assertEqual(list_public_products(self.db, search_term="missing")["total"], 0)
        self.assertEqual(list_public_products(self.db, search_term="Basic",
                                              category_slug="home")["items"], [])
        self.assertEqual(list_public_products(self.db)["total"], 3)
        self.assertFalse(self.db.new or self.db.dirty or self.db.deleted)
        for query in ("", "  ", "x" * 81, 1):
            with self.subTest(invalid=query), self.assertRaises(ValueError):
                list_public_products(self.db, search_term=query)

    def test_detail_filters_visibility_and_uses_only_public_fields(self):
        category = self.category("在售", "active")
        hidden_category = self.category("停用", "hidden", active=False)
        supplier = Supplier(supplier_public_id="SUP-A", name="供应商A")
        hidden_supplier = Supplier(supplier_public_id="SUP-B", name="供应商B",
                                   is_active=False)
        self.db.add_all((supplier, hidden_supplier))
        self.db.flush()
        product = self.product(category, "DETAIL")
        product.subtitle = "副标题"
        product.description = "商品说明"
        self.sku(product, supplier, "B", price="23.00", stock=0)
        self.sku(product, supplier, "A", price="12.50", stock=5)
        self.sku(product, hidden_supplier, "H", price="1.00", stock=9)
        self.sku(product, supplier, "I", active=False, price="2.00", stock=9)
        self.db.add_all([
            ProductMedia(product_id=product.id, media_role="MAIN",
                         image_path="/uploads/mall_products/PRD-DETAIL/main.webp",
                         uploaded_by_id=self.user.id),
            ProductMedia(product_id=product.id, media_role="CAROUSEL",
                         image_path="/uploads/mall_products/PRD-DETAIL/slide.webp",
                         uploaded_by_id=self.user.id),
            ProductMedia(product_id=product.id, media_role="DETAIL",
                         image_path="/uploads/mall_products/PRD-DETAIL/hidden.webp",
                         is_active=False, uploaded_by_id=self.user.id),
            ProductMedia(product_id=product.id, media_role="DETAIL",
                         image_path="/uploads/mall_products/OTHER/wrong.webp",
                         uploaded_by_id=self.user.id),
        ])
        draft = self.product(category, "DRAFT", status="DRAFT")
        self.sku(draft, supplier, "D", stock=2)
        hidden = self.product(hidden_category, "HIDDEN")
        self.sku(hidden, supplier, "HC", stock=2)
        no_sku = self.product(category, "NO-SKU")
        self.sku(no_sku, hidden_supplier, "NS", stock=2)
        self.db.commit()

        detail = get_public_product_detail(self.db, product_public_id="PRD-DETAIL")
        self.assertEqual(detail["category_slug"], "active")
        self.assertEqual(detail["description"], "商品说明")
        self.assertEqual(detail["min_points_price"], "12.50")
        self.assertTrue(detail["in_stock"])
        self.assertEqual([sku["sku_code"] for sku in detail["skus"]],
                         ["SKU-B", "SKU-A"])
        self.assertEqual([sku["in_stock"] for sku in detail["skus"]], [False, True])
        self.assertEqual([image["role"] for image in detail["images"]],
                         ["main", "carousel"])
        self.assertEqual(detail["main_image_url"], detail["images"][0]["url"])
        self.assertNotIn("cost_price", repr(detail))
        self.assertNotIn("supplier", repr(detail))
        self.assertNotIn("quantity", repr(detail))
        self.assertFalse(self.db.new or self.db.dirty or self.db.deleted)
        for public_id in ("PRD-DRAFT", "PRD-HIDDEN", "PRD-NO-SKU", "PRD-NONE"):
            with self.subTest(public_id=public_id):
                self.assertIsNone(get_public_product_detail(
                    self.db, product_public_id=public_id))
        with self.assertRaises(ValueError):
            get_public_product_detail(self.db, product_public_id="../PRD-DETAIL")

        routed = get_public_product("PRD-DETAIL", db=self.db)
        self.assertEqual(PublicProductDetail.model_validate(routed).model_dump(), detail)
        for public_id in ("PRD-DRAFT", "PRD-HIDDEN", "PRD-NO-SKU", "PRD-NONE"):
            with self.subTest(public_id=public_id), self.assertRaises(HTTPException) as error:
                get_public_product(public_id, db=self.db)
            self.assertEqual(error.exception.status_code, 404)


if __name__ == "__main__":
    unittest.main()
