import tempfile
import unittest
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
    create_catalog_category_route,
    create_catalog_product_route,
    create_catalog_sku_route,
    create_catalog_supplier_route,
    mall_catalog_page,
    publish_catalog_product_route,
    unpublish_catalog_product_route,
    update_catalog_category_route,
    update_catalog_product_route,
    update_catalog_sku_route,
    update_catalog_supplier_route,
)
from app.mall import (
    create_product,
    create_product_category,
    get_catalog_admin_snapshot,
)
from app.models import (
    AdminActionLog,
    Product,
    ProductCategory,
    ProductSku,
    Supplier,
    User,
)


def make_request(path="/mall-catalog", method="GET"):
    return Request({
        "type": "http",
        "method": method,
        "path": path,
        "raw_path": path.encode("utf-8"),
        "query_string": b"",
        "headers": [],
        "scheme": "http",
        "server": ("testserver", 80),
        "client": ("127.0.0.1", 12345),
        "root_path": "",
    })


class CatalogRouteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "catalog_routes.db"
        self.engine = create_engine(f"sqlite:///{self.path}")
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(
            bind=self.engine,
            autoflush=False,
            expire_on_commit=False,
        )
        with self.Session() as db:
            super_admin = self.add_user(db, "catalog-super", SUPER_ADMIN)
            operator = self.add_user(db, "catalog-operator", OPERATOR)
            reviewer = self.add_user(
                db, "catalog-reviewer", PRIMARY_REVIEWER
            )
            db.commit()
            self.super_admin = self.user_namespace(super_admin)
            self.operator = self.user_namespace(operator)
            self.reviewer = self.user_namespace(reviewer)

    def tearDown(self):
        self.engine.dispose()
        self.temp.cleanup()

    @staticmethod
    def add_user(db, username, admin_level):
        user = User(
            username=username,
            password_hash="test-only",
            role="admin",
            admin_level=admin_level,
            is_active=True,
        )
        db.add(user)
        db.flush()
        return user

    @staticmethod
    def user_namespace(user):
        return SimpleNamespace(
            id=user.id,
            username=user.username,
            role=user.role,
            admin_level=user.admin_level,
            is_active=True,
        )

    def call_route(self, user, function, *args, **kwargs):
        with (
            patch("app.main.get_current_user", return_value=user),
            patch("app.main.SessionLocal", side_effect=self.Session),
        ):
            return function(*args, **kwargs)

    def test_navigation_and_page_access_follow_catalog_permission(self):
        for user, allowed in (
            (self.super_admin, True),
            (self.operator, True),
            (self.reviewer, False),
            (None, False),
        ):
            with self.subTest(user=user):
                with patch("app.main.get_current_user", return_value=user):
                    context = admin_navigation_context(make_request())
                self.assertEqual(context["can_manage_mall_catalog"], allowed)

        with patch("app.main.get_current_user", return_value=None):
            response = mall_catalog_page(make_request())
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["location"], "/login")

        response = self.call_route(
            self.reviewer,
            mall_catalog_page,
            make_request(),
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["location"], "/dashboard")

    def test_empty_catalog_page_renders_for_operator(self):
        response = self.call_route(
            self.operator,
            mall_catalog_page,
            make_request(),
        )
        body = response.body.decode("utf-8")
        self.assertEqual(response.status_code, 200)
        self.assertIn("商品目录", body)
        self.assertIn("暂无商品", body)
        self.assertIn("商品与 SKU", body)
        self.assertNotIn("库存调整", body)

    def test_invalid_section_falls_back_with_controlled_error(self):
        response = self.call_route(
            self.operator,
            mall_catalog_page,
            make_request(),
            "unknown",
            "",
            "",
        )
        body = response.body.decode("utf-8")
        self.assertEqual(response.status_code, 200)
        self.assertIn("商品目录页面分区无效", body)
        self.assertIn("新建草稿商品", body)

    def test_complete_route_workflow_uses_services_and_writes_audit(self):
        request = make_request(method="POST")
        response = self.call_route(
            self.operator,
            create_catalog_category_route,
            request,
            "车辆服务",
            "car-service",
            "服务类商品",
            2,
            True,
        )
        self.assertEqual(response.status_code, 303)
        response = self.call_route(
            self.operator,
            create_catalog_supplier_route,
            request,
            "测试供应商",
            "测试联系人",
            "13800000000",
            "仅用于路由测试",
            True,
        )
        self.assertEqual(response.status_code, 303)

        with self.Session() as db:
            category = db.query(ProductCategory).one()
            supplier = db.query(Supplier).one()
            category_id = category.id
            supplier_id = supplier.id
            supplier_public_id = supplier.supplier_public_id

        response = self.call_route(
            self.operator,
            create_catalog_product_route,
            request,
            category_id,
            "基础洗车服务",
            "适用于普通乘用车",
            "到店核销",
            3,
        )
        self.assertEqual(response.status_code, 303)
        with self.Session() as db:
            product_id = db.query(Product.id).scalar()

        response = self.call_route(
            self.operator,
            create_catalog_sku_route,
            request,
            product_id,
            supplier_id,
            "wash-basic",
            "标准洗车",
            "vendor-wash-1",
            "88.888",
            "35.555",
            5,
            True,
            4,
        )
        self.assertEqual(response.status_code, 303)
        with self.Session() as db:
            sku_id = db.query(ProductSku.id).scalar()

        self.call_route(
            self.operator,
            update_catalog_category_route,
            request,
            category_id,
            "车辆养护",
            "car-care",
            "更新后的说明",
            1,
            True,
        )
        self.call_route(
            self.operator,
            update_catalog_supplier_route,
            request,
            supplier_id,
            "测试供应商（更新）",
            "新联系人",
            "13900000000",
            "更新备注",
            True,
        )
        self.call_route(
            self.operator,
            update_catalog_product_route,
            request,
            product_id,
            category_id,
            "精细洗车服务",
            "更新副标题",
            "更新商品说明",
            1,
        )
        self.call_route(
            self.operator,
            update_catalog_sku_route,
            request,
            sku_id,
            supplier_id,
            "精细洗车",
            "vendor-wash-2",
            "99.90",
            "40.10",
            6,
            True,
            2,
        )
        response = self.call_route(
            self.operator,
            publish_catalog_product_route,
            request,
            product_id,
        )
        self.assertIn("商品已上架", unquote_plus(response.headers["location"]))
        response = self.call_route(
            self.operator,
            unpublish_catalog_product_route,
            request,
            product_id,
        )
        self.assertIn("商品已下架", unquote_plus(response.headers["location"]))

        with self.Session() as db:
            category = db.get(ProductCategory, category_id)
            supplier = db.get(Supplier, supplier_id)
            product = db.get(Product, product_id)
            sku = db.get(ProductSku, sku_id)
            self.assertEqual(category.slug, "car-care")
            self.assertEqual(supplier.supplier_public_id, supplier_public_id)
            self.assertEqual(product.status, "UNPUBLISHED")
            self.assertEqual(product.product_public_id[:4], "PRD-")
            self.assertEqual(sku.sku_code, "WASH-BASIC")
            self.assertEqual(str(sku.points_price), "99.90")
            self.assertEqual(db.query(AdminActionLog).count(), 10)

        response = self.call_route(
            self.operator,
            mall_catalog_page,
            make_request(),
        )
        body = response.body.decode("utf-8")
        self.assertIn("精细洗车服务", body)
        self.assertIn("WASH-BASIC", body)
        self.assertIn("已下架", body)

    def test_failed_publish_rolls_back_and_surfaces_service_message(self):
        with self.Session() as db:
            category = create_product_category(
                db,
                actor_admin_id=self.operator.id,
                name="无 SKU 分类",
                slug="no-sku",
            ).entity
            product = create_product(
                db,
                actor_admin_id=self.operator.id,
                category_id=category.id,
                name="无 SKU 商品",
            ).entity
            product_id = product.id
            initial_log_count = db.query(AdminActionLog).count()
            db.commit()

        response = self.call_route(
            self.operator,
            publish_catalog_product_route,
            make_request(method="POST"),
            product_id,
        )
        self.assertEqual(response.status_code, 303)
        self.assertIn(
            "至少需要一个启用且供应商有效的 SKU",
            unquote_plus(response.headers["location"]),
        )
        with self.Session() as db:
            self.assertEqual(db.get(Product, product_id).status, "DRAFT")
            self.assertEqual(
                db.query(AdminActionLog).count(), initial_log_count
            )

    def test_unauthorized_write_does_not_open_transaction_or_mutate(self):
        with (
            patch("app.main.get_current_user", return_value=self.reviewer),
            patch("app.main.SessionLocal") as session_local,
        ):
            response = create_catalog_category_route(
                make_request(method="POST"),
                "越权分类",
                "forbidden",
                "",
                0,
                True,
            )
        self.assertEqual(response.headers["location"], "/dashboard")
        session_local.assert_not_called()
        with self.Session() as db:
            self.assertEqual(db.query(ProductCategory).count(), 0)

    def test_snapshot_is_ordered_and_read_only(self):
        with self.Session() as db:
            db.add_all([
                ProductCategory(
                    name="后分类",
                    slug="later",
                    sort_order=9,
                    is_active=False,
                ),
                ProductCategory(
                    name="前分类",
                    slug="first",
                    sort_order=1,
                    is_active=True,
                ),
            ])
            db.commit()
            snapshot = get_catalog_admin_snapshot(db)
            self.assertEqual(
                [item.slug for item in snapshot.categories],
                ["first", "later"],
            )
            self.assertEqual(
                [item.slug for item in snapshot.active_categories],
                ["first"],
            )
            self.assertFalse(db.new)
            self.assertFalse(db.dirty)

    def test_unexpected_failure_is_rolled_back_and_reraised(self):
        def fail_after_pending_write(db, **_kwargs):
            db.add(ProductCategory(name="临时分类", slug="temporary"))
            raise RuntimeError("unexpected route failure")

        with (
            patch("app.main.get_current_user", return_value=self.operator),
            patch("app.main.SessionLocal", side_effect=self.Session),
            patch(
                "app.main.create_product_category",
                side_effect=fail_after_pending_write,
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "unexpected"):
                create_catalog_category_route(
                    make_request(method="POST"),
                    "任意分类",
                    "any",
                    "",
                    0,
                    True,
                )
        with self.Session() as db:
            self.assertEqual(db.query(ProductCategory).count(), 0)


if __name__ == "__main__":
    unittest.main()
