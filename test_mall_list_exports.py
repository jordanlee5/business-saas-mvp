"""Virtual HTTP exports: matching scope, permissions, audit, safe XLSX and limits."""
import unittest
from io import BytesIO
from unittest.mock import patch
from openpyxl import load_workbook
from fastapi.testclient import TestClient
from app import main, models as m
from app.mall.list_exports import build_list_export
import test_order_service


class ListExportTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_order_service.OrderServiceTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.fixture.place()
        self.Session = self.fixture.Session
        self.actor = self.fixture.operator
        self.client = TestClient(main.app)
        self.addCleanup(self.client.close)
        for target, value in [("app.main.SessionLocal", self.Session),
                              ("app.main.get_current_user", lambda request: self.actor)]:
            p = patch(target, value); p.start(); self.addCleanup(p.stop)

    def get(self, path):
        res = self.client.get(path, follow_redirects=False)
        self.assertEqual(res.status_code, 200, res.text[:300] if res.status_code != 200 else "")
        self.assertIn("no-store", res.headers["cache-control"])
        self.assertIn(".xlsx", res.headers["content-disposition"])
        return load_workbook(BytesIO(res.content))

    def test_three_exports_match_lists_and_only_audit_changes(self):
        with self.Session() as db:
            before = {t: db.query(t).count() for t in [m.PointsLedgerEntry, m.InventoryMovement, m.Order, m.PointsGrant]}
            logs = db.query(m.AdminActionLog).count()
        for url, rows in [("/mall-orders/export", 1), ("/member-points/export", 1), ("/mall-catalog/export", 2)]:
            book = self.get(url)
            self.assertEqual(book.worksheets[0].max_row, rows + 1)
            self.assertEqual(book.worksheets[0].freeze_panes, "A2")
        with self.Session() as db:
            self.assertEqual(before, {t: db.query(t).count() for t in before})
            self.assertEqual(db.query(m.AdminActionLog).count(), logs + 3)
            for row in db.query(m.AdminActionLog).filter(m.AdminActionLog.action_type.like("%export")):
                self.assertEqual(row.admin_id, self.actor.id)
                self.assertIsNotNone(row.created_at)

    def test_filters_empty_and_invalid_do_not_export_other_records(self):
        for url in ["/mall-orders/export?status=SHIPPED", "/member-points/export?member_status=INACTIVE",
                    "/mall-catalog/export?keyword=not-found", "/mall-orders/export?keyword=not-found"]:
            self.assertEqual(self.get(url).worksheets[0].max_row, 1)
        for url in ["/mall-orders/export?status=invalid", "/member-points/export?member_status=invalid"]:
            self.assertEqual(self.client.get(url).status_code, 400)

    def test_unauthenticated_reviewer_inactive_and_partner_denied(self):
        from types import SimpleNamespace
        for actor in [None, SimpleNamespace(id=1, is_active=True, role="admin", admin_level="primary_reviewer"),
                      SimpleNamespace(id=1, is_active=False, role="admin", admin_level="super_admin"),
                      SimpleNamespace(id=1, is_active=True, role="partner")]:
            with patch("app.main.get_current_user", return_value=actor), patch("app.main.SessionLocal") as db:
                for url in ["/mall-orders/export", "/member-points/export", "/mall-catalog/export"]:
                    self.assertEqual(self.client.get(url, follow_redirects=False).status_code, 302)
                db.assert_not_called()
        with self.Session() as db:
            with self.assertRaises(PermissionError):
                build_list_export(db, actor_id=self.fixture.partner.id, kind="catalog")

    def test_cross_page_catalog_text_and_numbers(self):
        with self.Session.begin() as db:
            product = db.get(m.Product, self.fixture.product.id)
            product.name = '=HYPERLINK("https://example.invalid", "虚拟商品")'
            for i in range(60):
                db.add(m.ProductSku(product_id=product.id, supplier_id=self.fixture.supplier.id,
                    sku_code=f"000{i:03}", name=f"跨页规格{i}", cost_price="1.23", points_price="4.56"))
        book = self.get("/mall-catalog/export?page=2&keyword=跨页")
        sheet = book.worksheets[0]
        self.assertEqual(sheet.max_row, 61)
        self.assertEqual(sheet["A2"].data_type, "s")
        self.assertTrue(sheet["A2"].value.startswith("=HYPERLINK"))
        self.assertEqual(sheet["B2"].value, "000000")
        self.assertEqual(sheet["F2"].value, 1.23)
        self.assertEqual(sheet["G2"].value, 4.56)
        self.assertTrue(all(cell.data_type != "f" for row in sheet for cell in row))
        with patch("app.mall.list_exports.MAX_ROWS", 20):
            res = self.client.get("/mall-catalog/export?keyword=跨页")
            self.assertEqual(res.status_code, 400)
            self.assertIn("缩小筛选", res.text)

    def test_members_and_orders_export_beyond_first_page(self):
        with self.Session.begin() as db:
            for i in range(105):
                db.add(m.Member(member_public_id=f"MEM-EXPORT-{i:03}"))
                db.add(m.Order(order_public_id=f"ORD-EXPORT-{i:03}", member_id=self.fixture.member.id,
                    idempotency_key=f"virtual-export-{i}", status="CANCELLED", total_points=10,
                    total_cost_amount=2, total_quantity=1))
        self.assertEqual(self.get("/member-points/export?keyword=MEM-EXPORT").worksheets[0].max_row, 106)
        self.assertEqual(self.get("/mall-orders/export?status=CANCELLED").worksheets[0].max_row, 106)

    def test_render_operational_workbenches(self):
        for url in ["/mall-delegations", "/mall-qualifications", "/mall-rights"]:
            res = self.client.get(url)
            self.assertEqual(res.status_code, 200)
            self.assertIn("ops-heading", res.text)
            self.assertIn("/static/js/sidebar.js", res.text)


if __name__ == "__main__":
    unittest.main()
