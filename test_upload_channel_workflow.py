import unittest
from datetime import datetime
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fastapi import Request, UploadFile
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.admin_permissions import OPERATOR
from app.database import Base
from app.main import (
    apply_accepted_batch_filter,
    build_accepted_business_batch_options,
    build_business_record_items,
    compact_filename,
    correct_upload_batch_channel,
    revert_upload_batch,
    upload_excel_submit,
)
from app.mall import (
    BUSINESS_CHANNEL_ALL,
    BusinessChannel,
    BusinessClaimStatus,
)
from app.models import (
    AdminActionLog,
    BusinessRecord,
    MatchReview,
    UploadBatch,
    User,
)
from app.ocr_service import match_ocr_with_records


class UploadChannelWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
        )
        Base.metadata.create_all(bind=self.engine)
        self.Session = sessionmaker(bind=self.engine)
        self.db = self.Session()

        self.partner = User(
            username="channel_partner",
            password_hash="test",
            role="partner",
        )
        self.operator = User(
            username="channel_operator",
            password_hash="test",
            role="admin",
            admin_level=OPERATOR,
        )
        self.db.add_all([self.partner, self.operator])
        self.db.flush()

        self.cash_batch = self._add_batch(
            "cash.xlsx",
            BusinessChannel.CASH_REBATE.value,
        )
        self.mall_batch = self._add_batch(
            "mall.xlsx",
            BusinessChannel.MALL_REDEMPTION.value,
        )
        self.cash_record = self._add_record(
            self.cash_batch,
            BusinessChannel.CASH_REBATE.value,
            None,
            "现金客户",
        )
        self.mall_record = self._add_record(
            self.mall_batch,
            BusinessChannel.MALL_REDEMPTION.value,
            BusinessClaimStatus.PENDING_ACTIVATION.value,
            "商城客户",
        )
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def _add_batch(self, filename, redemption_mode):
        batch = UploadBatch(
            user_id=self.partner.id,
            filename=filename,
            total_rows=1,
            success_rows=1,
            failed_rows=0,
            acceptance_status="已承接",
            redemption_mode=redemption_mode,
        )
        self.db.add(batch)
        self.db.flush()
        return batch

    def _add_record(
        self,
        batch,
        redemption_mode,
        claim_status,
        name,
    ):
        record = BusinessRecord(
            user_id=self.partner.id,
            batch_id=batch.id,
            business_no=f"legacy-{batch.id}",
            name=name,
            phone=f"1380000000{batch.id}",
            plate_number=f"粤A0000{batch.id}",
            points_amount=100.0,
            bank_card=f"622200000000000{batch.id}",
            redemption_mode=redemption_mode,
            claim_status=claim_status,
        )
        self.db.add(record)
        self.db.flush()
        return record

    def test_cash_processing_query_excludes_mall_records(self):
        records = apply_accepted_batch_filter(
            self.db.query(BusinessRecord)
        ).all()

        self.assertEqual(
            [record.id for record in records],
            [self.cash_record.id],
        )

    def test_voucher_batch_options_exclude_mall_batches(self):
        options = build_accepted_business_batch_options(
            self.db,
            self.partner.id,
        )

        self.assertEqual(
            [option["id"] for option in options],
            [self.cash_batch.id],
        )

    def test_ocr_matcher_defensively_ignores_mall_records(self):
        results = match_ocr_with_records(
            "测试客户 6222020000000000 100.00",
            [
                SimpleNamespace(
                    name="测试客户",
                    bank_card="6222020000000000",
                    points_amount=100.0,
                    redemption_mode=(
                        BusinessChannel.MALL_REDEMPTION.value
                    ),
                )
            ],
            voucher_amount=100.0,
        )

        self.assertEqual(results, [])

    def test_business_list_filters_channels_without_crossing(self):
        admin_view = SimpleNamespace(
            id=self.operator.id,
            role="admin",
        )

        cash_items = build_business_record_items(
            db=self.db,
            user=admin_view,
            redemption_mode=(
                BusinessChannel.CASH_REBATE.value
            ),
            use_pagination=False,
        )[0]
        mall_items = build_business_record_items(
            db=self.db,
            user=admin_view,
            redemption_mode=(
                BusinessChannel.MALL_REDEMPTION.value
            ),
            use_pagination=False,
        )[0]
        all_items = build_business_record_items(
            db=self.db,
            user=admin_view,
            redemption_mode=BUSINESS_CHANNEL_ALL,
            use_pagination=False,
        )[0]

        self.assertEqual(
            [item["id"] for item in cash_items],
            [self.cash_record.id],
        )
        self.assertEqual(
            [item["id"] for item in mall_items],
            [self.mall_record.id],
        )
        self.assertEqual(len(all_items), 2)
        self.assertEqual(
            mall_items[0]["latest_review_status"],
            "不适用",
        )
        self.assertIsNone(
            mall_items[0]["business_status"]
        )

    def test_controlled_correction_route_writes_audit_log(self):
        self.mall_batch.acceptance_status = "待承接"
        self.db.commit()
        batch_id = self.mall_batch.id

        with (
            patch(
                "app.main.SessionLocal",
                self.Session,
            ),
            patch(
                "app.main.get_current_user",
                return_value=self.operator,
            ),
        ):
            response = correct_upload_batch_channel(
                SimpleNamespace(),
                batch_id=batch_id,
                redemption_mode=(
                    BusinessChannel.CASH_REBATE.value
                ),
                claim_deadline="",
                return_url="/business-records",
            )

        verification_db = self.Session()
        try:
            corrected_batch = verification_db.get(
                UploadBatch,
                batch_id,
            )
            corrected_record = (
                verification_db.query(BusinessRecord)
                .filter(
                    BusinessRecord.batch_id == batch_id
                )
                .one()
            )
            audit_log = (
                verification_db.query(AdminActionLog)
                .filter(
                    AdminActionLog.target_type
                    == "upload_batch",
                    AdminActionLog.target_id == batch_id,
                )
                .one()
            )

            self.assertEqual(response.status_code, 303)
            self.assertEqual(
                corrected_batch.redemption_mode,
                BusinessChannel.CASH_REBATE.value,
            )
            self.assertIsNone(
                corrected_record.claim_status
            )
            self.assertEqual(
                audit_log.action_type,
                "correct_batch_redemption_mode",
            )
            self.assertIn(
                "商城积分 -> 现金返现",
                audit_log.description,
            )
        finally:
            verification_db.close()

    def test_safe_revert_route_restores_pending_and_writes_audit(self):
        batch_id = self.cash_batch.id

        with (
            patch(
                "app.main.SessionLocal",
                self.Session,
            ),
            patch(
                "app.main.get_current_user",
                return_value=self.operator,
            ),
        ):
            response = revert_upload_batch(
                SimpleNamespace(),
                batch_id=batch_id,
                reason="误承接测试清单",
                return_url="/business-records",
            )

        verification_db = self.Session()
        try:
            reverted_batch = verification_db.get(
                UploadBatch,
                batch_id,
            )
            audit_log = (
                verification_db.query(AdminActionLog)
                .filter(
                    AdminActionLog.target_id == batch_id,
                    AdminActionLog.action_type
                    == "revert_accept_batch",
                )
                .one()
            )

            self.assertEqual(response.status_code, 303)
            self.assertEqual(
                reverted_batch.acceptance_status,
                "待承接",
            )
            self.assertIn("误承接测试清单", audit_log.description)
        finally:
            verification_db.close()

    def test_revert_route_blocks_batch_with_review(self):
        batch_id = self.cash_batch.id
        self.db.add(
            MatchReview(
                business_record_id=self.cash_record.id,
                review_status="待审核",
            )
        )
        self.db.commit()

        with (
            patch(
                "app.main.SessionLocal",
                self.Session,
            ),
            patch(
                "app.main.get_current_user",
                return_value=self.operator,
            ),
        ):
            response = revert_upload_batch(
                SimpleNamespace(),
                batch_id=batch_id,
                reason="尝试撤销",
                return_url="/business-records",
            )

        verification_db = self.Session()
        try:
            protected_batch = verification_db.get(
                UploadBatch,
                batch_id,
            )
            revert_log_count = (
                verification_db.query(AdminActionLog)
                .filter(
                    AdminActionLog.target_id == batch_id,
                    AdminActionLog.action_type
                    == "revert_accept_batch",
                )
                .count()
            )

            self.assertEqual(response.status_code, 303)
            self.assertIn("batch_error", response.headers["location"])
            self.assertEqual(
                protected_batch.acceptance_status,
                "已承接",
            )
            self.assertEqual(revert_log_count, 0)
        finally:
            verification_db.close()

    def test_compact_filename_uses_requested_extreme_abbreviation(self):
        self.assertEqual(
            compact_filename(
                "测试清单-商城积分-广西人保-20230903_1145.xlsx"
            ),
            "测试....1145",
        )


class UploadAtomicWorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def test_any_row_error_prevents_batch_creation(self):
        request = Request({
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/upload-excel",
            "root_path": "",
            "query_string": b"",
            "headers": [],
            "client": ("testclient", 50000),
            "server": ("testserver", 80),
        })
        upload = UploadFile(
            filename="mixed.xlsx",
            file=BytesIO(b"test"),
        )
        user = SimpleNamespace(
            id=999,
            username="atomic_partner",
            role="partner",
            admin_level=None,
        )
        session_factory = MagicMock()
        template_response = MagicMock(
            return_value=SimpleNamespace(status_code=400)
        )

        with (
            patch(
                "app.main.get_current_user",
                return_value=user,
            ),
            patch(
                "app.main.SessionLocal",
                session_factory,
            ),
            patch(
                "app.main.utc8_now",
                return_value=datetime(2026, 9, 3, 12, 0, 0),
            ),
            patch(
                "app.main.parse_business_excel",
                return_value=(
                    [{"name": "合格行"}],
                    ["第 3 行：车牌号为空"],
                ),
            ),
            patch(
                "app.main.templates.TemplateResponse",
                template_response,
            ),
        ):
            response = await upload_excel_submit(
                request,
                file=upload,
                redemption_mode=(
                    BusinessChannel.CASH_REBATE.value
                ),
                claim_deadline="",
            )

        self.assertEqual(response.status_code, 400)
        session_factory.assert_not_called()
        template_context = template_response.call_args.args[1]
        self.assertIn(
            "整份清单未导入，请修正以下问题后重新上传：",
            template_context["errors"],
        )
        self.assertFalse(
            Path(
                "uploads/excel/999_20260903120000_mixed.xlsx"
            ).exists()
        )


if __name__ == "__main__":
    unittest.main()
