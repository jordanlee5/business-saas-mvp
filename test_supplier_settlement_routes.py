"""供应商结算后台入口的角色、只读呈现与受控下载测试。"""

import asyncio
import unittest
from datetime import datetime
from http.cookies import SimpleCookie
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from starlette.requests import Request

from app.admin_permissions import OPERATOR, PRIMARY_REVIEWER, SUPER_ADMIN
from app.main import (
    admin_navigation_context,
    export_mall_settlement,
    generate_mall_settlement_route,
    mall_settlement_detail_page,
    mall_settlements_page,
)
from app.mall import (
    SupplierSettlementDetail,
    SupplierSettlementDetailItem,
    SupplierSettlementExportResult,
    SupplierSettlementPage,
)


NOW = datetime(2026, 9, 23, 10, 0, 0)


def request(path="/mall-settlements", *, method="GET", cookie=""):
    return Request({
        "type": "http", "http_version": "1.1", "method": method,
        "scheme": "http", "path": path, "root_path": "",
        "query_string": b"", "headers": (
            [(b"cookie", cookie.encode())] if cookie else []
        ),
        "client": ("testclient", 50000), "server": ("testserver", 80),
    })


def actor(level, *, active=True):
    return SimpleNamespace(
        id=10, username="admin", role="admin",
        admin_level=level, is_active=active,
    )


def detail(status="CONFIRMED"):
    return SupplierSettlementDetail(
        batch_id=31, settlement_public_id="SETTLEMENT-001",
        supplier_id=12, supplier_public_id_snapshot="SUP-001",
        supplier_name_snapshot="供应商甲", status=status,
        status_label="已确认" if status == "CONFIRMED" else "待确认",
        period_start=NOW, period_end=NOW, order_count=1,
        item_count=1, total_quantity=2,
        total_cost_amount=Decimal("25.00"),
        generated_by_admin_id=10, generated_by_username="admin",
        generated_at=NOW,
        confirmed_by_admin_id=10 if status == "CONFIRMED" else None,
        confirmed_by_username="admin" if status == "CONFIRMED" else None,
        confirmed_at=NOW if status == "CONFIRMED" else None,
        items=(SupplierSettlementDetailItem(
            settlement_item_id=2, order_id=21, order_item_id=32,
            order_public_id_snapshot="ORD-001", order_completed_at=NOW,
            product_public_id_snapshot="PRD-001",
            product_name_snapshot="商品甲", sku_code_snapshot="SKU-001",
            sku_name_snapshot="规格甲", supplier_sku_code_snapshot=None,
            unit_cost_price=Decimal("12.50"), quantity=2,
            line_cost_amount=Decimal("25.00"),
        ),),
    )


class SupplierSettlementRouteTests(unittest.TestCase):
    def test_navigation_allows_only_active_operations_admins(self):
        for user, allowed in (
            (actor(SUPER_ADMIN), True), (actor(OPERATOR), True),
            (actor(PRIMARY_REVIEWER), False),
            (actor(OPERATOR, active=False), False), (None, False),
        ):
            with self.subTest(user=user):
                with patch("app.main.get_current_user", return_value=user):
                    context = admin_navigation_context(request())
                self.assertEqual(
                    context["can_view_mall_supplier_settlements"], allowed
                )

    def test_guards_run_before_reading_or_exporting(self):
        cases = (
            (mall_settlements_page, (request(), "", "ALL", 1, 20, "")),
            (mall_settlement_detail_page, (request(), "SETTLEMENT-001")),
            (export_mall_settlement, (request(), "SETTLEMENT-001")),
        )
        for handler, arguments in cases:
            for user, destination in (
                (None, "/login"),
                (actor(PRIMARY_REVIEWER), "/dashboard"),
                (actor(OPERATOR, active=False), "/dashboard"),
            ):
                with self.subTest(handler=handler.__name__, user=user):
                    with (
                        patch("app.main.get_current_user", return_value=user),
                        patch("app.main.SessionLocal") as session,
                        patch("app.main.execute_supplier_settlement_export")
                        as export,
                    ):
                        response = handler(*arguments)
                    self.assertEqual(response.headers["location"], destination)
                    session.assert_not_called()
                    export.assert_not_called()

    def test_list_renders_empty_state_and_rejects_bad_filters(self):
        empty = SupplierSettlementPage(
            items=(), keyword="", status="ALL", page=1,
            page_size=20, total=0, total_pages=1,
        )
        with (
            patch("app.main.get_current_user", return_value=actor(OPERATOR)),
            patch("app.main.SessionLocal"),
            patch("app.main.list_supplier_settlements", return_value=empty)
            as listing,
        ):
            response = mall_settlements_page(request(), "", "ALL", 1, 20, "")
            self.assertEqual(response.status_code, 200)
            self.assertIn("暂无符合条件".encode(), response.body)
            listing.side_effect = [ValueError("供应商结算状态无效"), empty]
            response = mall_settlements_page(request(), "", "UNKNOWN", 1, 20, "")
        self.assertIn("供应商结算状态无效".encode(), response.body)

    def test_generation_form_requires_operations_role_and_issues_csrf(self):
        empty = SupplierSettlementPage(
            items=(), keyword="", status="ALL", page=1,
            page_size=20, total=0, total_pages=1,
        )
        for level, permitted in ((OPERATOR, True), (SUPER_ADMIN, True),
                                 (PRIMARY_REVIEWER, False)):
            with (
                self.subTest(level=level),
                patch("app.main.get_current_user", return_value=actor(level)),
                patch("app.main.SessionLocal") as session,
                patch("app.main.list_supplier_settlements", return_value=empty),
            ):
                response = mall_settlements_page(
                    request(), "", "ALL", 1, 20, ""
                )
                if permitted:
                    self.assertIn(b'/mall-settlements/generate', response.body)
                    self.assertIn("mall_settlement_csrf=", response.headers["set-cookie"])
                    session.return_value.query.assert_called_once()
                else:
                    self.assertEqual(response.headers["location"], "/dashboard")
                    session.assert_not_called()

    def test_generate_route_guards_and_delegates_to_atomic_service(self):
        token = "a" * 43
        incoming = request(
            "/mall-settlements/generate", method="POST",
            cookie=f"mall_settlement_csrf={token}",
        )
        fields = (12, "2026-09-01T00:00", "2026-09-08T00:00")
        with patch("app.main.execute_supplier_settlement_generation") as generate:
            for user, destination in (
                (None, "/login"),
                (actor(PRIMARY_REVIEWER), "/dashboard"),
                (actor(OPERATOR, active=False), "/dashboard"),
            ):
                with patch("app.main.get_current_user", return_value=user):
                    response = generate_mall_settlement_route(
                        incoming, *fields, token
                    )
                self.assertEqual(response.headers["location"], destination)
                generate.assert_not_called()
            with patch("app.main.get_current_user", return_value=actor(OPERATOR)):
                for bad_token in ("", "b" * 43):
                    response = generate_mall_settlement_route(
                        incoming, *fields, bad_token
                    )
                    self.assertIn("error=", response.headers["location"])
                response = generate_mall_settlement_route(
                    incoming, 12, "2026-09-01T00:00", "bad", token
                )
                self.assertIn("error=", response.headers["location"])
                generate.assert_not_called()
                generate.return_value.settlement_public_id = "SETTLEMENT-NEW"
                response = generate_mall_settlement_route(
                    incoming, *fields, token
                )
                self.assertEqual(response.status_code, 303)
                self.assertEqual(
                    response.headers["location"],
                    "/mall-settlements/SETTLEMENT-NEW",
                )
                generate.assert_called_once()
                self.assertEqual(generate.call_args.kwargs["actor_admin_id"], 10)
                self.assertEqual(generate.call_args.kwargs["supplier_id"], 12)
                self.assertEqual(
                    generate.call_args.kwargs["period_end"],
                    datetime(2026, 9, 8),
                )
                generate.side_effect = ValueError("没有新增合格订单项")
                response = generate_mall_settlement_route(
                    incoming, *fields, token
                )
                self.assertIn("error=", response.headers["location"])

    def test_real_generation_from_page_writes_one_batch_and_one_audit(self):
        from datetime import timedelta
        from app.models import AdminActionLog, SupplierSettlementBatch
        from test_supplier_settlement_service import (
            SupplierSettlementServiceTests, PERIOD_START,
        )

        fixture = SupplierSettlementServiceTests(
            "test_generates_only_unsettled_completed_items_in_half_open_period"
        )
        fixture.setUp()
        try:
            fixture.add_order(
                suffix="ROUTE-GENERATE",
                completed_at=PERIOD_START + timedelta(days=1),
                lines=((fixture.sku_one, 2),),
            )
            fixture.db.commit()
            with (
                patch("app.main.get_current_user", return_value=fixture.operator),
                patch("app.main.SessionLocal", side_effect=fixture.Session),
                patch("app.main.engine", fixture.engine),
            ):
                page = mall_settlements_page(
                    request(), "", "ALL", 1, 20, ""
                )
                cookie = SimpleCookie()
                cookie.load(page.headers["set-cookie"])
                token = cookie["mall_settlement_csrf"].value
                incoming = request(
                    "/mall-settlements/generate", method="POST",
                    cookie=f"mall_settlement_csrf={token}",
                )
                arguments = (
                    incoming, fixture.supplier.id,
                    "2026-09-01T00:00", "2026-09-08T00:00", token,
                )
                response = generate_mall_settlement_route(*arguments)
                self.assertEqual(response.status_code, 303)
                self.assertIn("/mall-settlements/", response.headers["location"])
                repeat = generate_mall_settlement_route(*arguments)
                self.assertIn("error=", repeat.headers["location"])
            with fixture.Session() as db:
                self.assertEqual(db.query(SupplierSettlementBatch).count(), 1)
                self.assertEqual(db.query(AdminActionLog).filter_by(
                    action_type="mall_supplier_settlement_generate"
                ).count(), 1)
        finally:
            fixture.tearDown()

    def test_detail_shows_snapshot_and_only_confirmed_download(self):
        with (
            patch("app.main.get_current_user", return_value=actor(OPERATOR)),
            patch("app.main.SessionLocal"),
            patch("app.main.get_supplier_settlement_detail", return_value=detail())
            as lookup,
        ):
            response = mall_settlement_detail_page(request(), "SETTLEMENT-001")
            self.assertIn(b"ORD-001", response.body)
            self.assertIn(b"/mall-settlements/SETTLEMENT-001/export", response.body)
            lookup.return_value = detail("PENDING_CONFIRMATION")
            response = mall_settlement_detail_page(request(), "SETTLEMENT-001")
            self.assertNotIn(b"/export", response.body)
            lookup.side_effect = ValueError("证据不一致")
            response = mall_settlement_detail_page(request(), "SETTLEMENT-001")
            self.assertEqual(response.status_code, 302)
            self.assertIn("error=", response.headers["location"])

    def test_export_delegates_atomic_audit_and_returns_workbook(self):
        exported = SupplierSettlementExportResult(
            batch_id=31, settlement_public_id="SETTLEMENT-001",
            supplier_public_id_snapshot="SUP-001", status="CONFIRMED",
            total_cost_amount=Decimal("25.00"), exported_by_admin_id=10,
            exported_at=NOW, action_log_id=20,
            filename="supplier_settlement_SETTLEMENT-001.xlsx",
            content=b"workbook bytes",
        )
        with (
            patch("app.main.get_current_user", return_value=actor(OPERATOR)),
            patch("app.main.execute_supplier_settlement_export",
                  return_value=exported) as export,
        ):
            response = export_mall_settlement(request(), "SETTLEMENT-001")
            self.assertEqual(response.status_code, 200)
            self.assertIn("supplier_settlement_31_20260923100000.xlsx",
                          response.headers["content-disposition"])
            chunks = asyncio.run(self._content(response))
            self.assertEqual(chunks, b"workbook bytes")
            export.assert_called_once()
            self.assertEqual(export.call_args.kwargs["actor_admin_id"], 10)
            export.side_effect = ValueError("仅已确认的供应商结算可以导出")
            response = export_mall_settlement(request(), "SETTLEMENT-001")
            self.assertEqual(response.status_code, 302)
            self.assertIn("error=", response.headers["location"])

    def test_real_confirmed_batch_download_is_audited_and_tampering_fails(self):
        from datetime import timedelta

        from app.models import AdminActionLog, SupplierSettlementBatch
        from test_supplier_settlement_service import (
            SupplierSettlementServiceTests,
            PERIOD_START,
        )

        fixture = SupplierSettlementServiceTests(
            "test_lists_settlements_with_keyword_status_and_pagination"
        )
        fixture.setUp()
        try:
            fixture.add_order(
                suffix="ROUTE-EXPORT",
                completed_at=PERIOD_START + timedelta(days=1),
                lines=((fixture.sku_one, 2),),
            )
            fixture.db.commit()
            generated = fixture.generate()
            fixture.confirm(generated.settlement_public_id)

            with (
                patch("app.main.get_current_user",
                      return_value=fixture.operator),
                patch("app.main.SessionLocal", side_effect=fixture.Session),
                patch("app.main.engine", fixture.engine),
            ):
                response = mall_settlements_page(
                    request(), "", "ALL", 1, 20, ""
                )
                self.assertIn(
                    generated.settlement_public_id.encode(), response.body
                )
                response = mall_settlement_detail_page(
                    request(), generated.settlement_public_id
                )
                self.assertIn(b"ORD-SETTLEMENT-ROUTE-EXPORT", response.body)
                response = export_mall_settlement(
                    request(), generated.settlement_public_id
                )
                self.assertTrue(asyncio.run(self._content(response)).startswith(
                    b"PK"
                ))
                with fixture.Session() as db:
                    audit_count = db.query(AdminActionLog).filter_by(
                        action_type="mall_supplier_settlement_export"
                    ).count()
                    self.assertEqual(audit_count, 1)
                    batch = db.get(SupplierSettlementBatch, generated.batch_id)
                    batch.total_cost_amount = Decimal("999.00")
                    db.commit()
                with self.assertRaises(RuntimeError):
                    export_mall_settlement(
                        request(), generated.settlement_public_id
                    )
                with fixture.Session() as db:
                    self.assertEqual(db.query(AdminActionLog).filter_by(
                        action_type="mall_supplier_settlement_export"
                    ).count(), 1)
        finally:
            fixture.tearDown()

    @staticmethod
    async def _content(response):
        return b"".join([chunk async for chunk in response.body_iterator])


if __name__ == "__main__":
    unittest.main()
