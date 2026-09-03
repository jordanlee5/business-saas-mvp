import unittest
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory

import pandas as pd
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.excel_service import parse_business_excel
from app.mall import (
    ACCEPTED_BATCH_STATUS,
    BUSINESS_CHANNEL_ALL,
    PENDING_BATCH_STATUS,
    REJECTED_BATCH_STATUS,
    BusinessChannel,
    BusinessClaimStatus,
    batch_revert_block_reason,
    build_upload_channel_snapshot,
    business_channel_label,
    correct_pending_batch_channel,
    decide_pending_batch,
    normalize_business_channel_filter,
    revert_batch_decision,
)
from app.models import (
    BusinessRecord,
    MatchReview,
    PointsGrant,
    UploadBatch,
    User,
)


class UploadExcelFieldValidationTests(unittest.TestCase):
    def write_excel(self, rows):
        temp_dir = TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        path = Path(temp_dir.name) / "upload.xlsx"
        pd.DataFrame(
            rows,
            columns=[
                "姓名",
                "手机号",
                "车牌号",
                "积分金额",
                "银行卡号",
            ],
        ).to_excel(path, index=False)
        return path

    def test_mall_allows_blank_bank_card(self):
        path = self.write_excel([
            {
                "姓名": "商城客户",
                "手机号": "13800000001",
                "车牌号": "桂A10001",
                "积分金额": 100,
                "银行卡号": "",
            }
        ])

        records, errors = parse_business_excel(
            str(path),
            bank_card_required=False,
        )

        self.assertEqual(errors, [])
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["bank_card"], "")

    def test_cash_rejects_blank_bank_card(self):
        path = self.write_excel([
            {
                "姓名": "现金客户",
                "手机号": "13800000002",
                "车牌号": "桂A10002",
                "积分金额": 100,
                "银行卡号": "",
            }
        ])

        records, errors = parse_business_excel(str(path))

        self.assertEqual(records, [])
        self.assertIn("第 2 行：银行卡号为空", errors)

    def test_every_channel_rejects_blank_plate_number(self):
        path = self.write_excel([
            {
                "姓名": "缺少车牌",
                "手机号": "13800000003",
                "车牌号": "",
                "积分金额": 100,
                "银行卡号": "6222000000000003",
            }
        ])

        for bank_card_required in (True, False):
            with self.subTest(
                bank_card_required=bank_card_required
            ):
                records, errors = parse_business_excel(
                    str(path),
                    bank_card_required=bank_card_required,
                )
                self.assertEqual(records, [])
                self.assertIn("第 2 行：车牌号为空", errors)

    def test_every_channel_rejects_blank_points_amount(self):
        path = self.write_excel([
            {
                "姓名": "缺少积分",
                "手机号": "13800000004",
                "车牌号": "桂A10004",
                "积分金额": None,
                "银行卡号": "6222000000000004",
            }
        ])

        for bank_card_required in (True, False):
            with self.subTest(
                bank_card_required=bank_card_required
            ):
                records, errors = parse_business_excel(
                    str(path),
                    bank_card_required=bank_card_required,
                )
                self.assertEqual(records, [])
                self.assertIn(
                    "第 2 行：积分金额不是数字",
                    errors,
                )

    def test_empty_sheet_is_rejected(self):
        path = self.write_excel([])

        records, errors = parse_business_excel(str(path))

        self.assertEqual(records, [])
        self.assertEqual(errors, ["Excel 中没有可导入的数据行"])


class UploadChannelValidationTests(unittest.TestCase):
    def test_cash_snapshot_preserves_legacy_defaults(self):
        snapshot = build_upload_channel_snapshot(
            BusinessChannel.CASH_REBATE.value,
            "",
        )

        self.assertEqual(
            snapshot.redemption_mode,
            BusinessChannel.CASH_REBATE.value,
        )
        self.assertIsNone(snapshot.claim_deadline)
        self.assertIsNone(snapshot.claim_status)

    def test_cash_snapshot_rejects_mall_deadline(self):
        with self.assertRaisesRegex(
            ValueError,
            "现金返现渠道不能设置商城领取截止日",
        ):
            build_upload_channel_snapshot(
                BusinessChannel.CASH_REBATE.value,
                "2026-12-31",
            )

    def test_mall_snapshot_requires_deadline(self):
        with self.assertRaisesRegex(
            ValueError,
            "商城积分渠道必须设置领取截止日",
        ):
            build_upload_channel_snapshot(
                BusinessChannel.MALL_REDEMPTION.value,
                "",
            )

    def test_mall_snapshot_uses_utc8_end_of_day(self):
        snapshot = build_upload_channel_snapshot(
            BusinessChannel.MALL_REDEMPTION.value,
            "2026-09-30",
            now=datetime(2026, 9, 3, 9, 0, 0),
        )

        self.assertEqual(
            snapshot.claim_deadline,
            datetime(2026, 9, 30, 23, 59, 59, 999999),
        )
        self.assertEqual(
            snapshot.claim_status,
            BusinessClaimStatus.PENDING_ACTIVATION.value,
        )

    def test_mall_snapshot_rejects_past_or_invalid_date(self):
        for value, message in (
            ("2026-09-02", "不能早于今天"),
            ("2026-02-30", "必须是有效日期"),
        ):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, message):
                    build_upload_channel_snapshot(
                        BusinessChannel.MALL_REDEMPTION.value,
                        value,
                        now=datetime(2026, 9, 3, 9, 0, 0),
                    )

    def test_channel_filter_and_labels_fail_closed(self):
        self.assertEqual(
            normalize_business_channel_filter("unknown"),
            BUSINESS_CHANNEL_ALL,
        )
        self.assertEqual(
            normalize_business_channel_filter(
                BusinessChannel.MALL_REDEMPTION.value
            ),
            BusinessChannel.MALL_REDEMPTION.value,
        )
        self.assertEqual(
            business_channel_label(
                BusinessChannel.CASH_REBATE.value
            ),
            "现金返现",
        )
        self.assertEqual(
            business_channel_label("unknown"),
            "未知渠道",
        )


class PendingBatchChannelCorrectionTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
        )
        Base.metadata.create_all(bind=self.engine)
        self.Session = sessionmaker(bind=self.engine)
        self.db = self.Session()

        uploader = User(
            username="partner_channel_test",
            password_hash="test",
            role="partner",
        )
        self.db.add(uploader)
        self.db.flush()

        self.batch = UploadBatch(
            user_id=uploader.id,
            filename="channel.xlsx",
            total_rows=2,
            success_rows=2,
            failed_rows=0,
            acceptance_status="待承接",
            redemption_mode=(
                BusinessChannel.CASH_REBATE.value
            ),
        )
        self.db.add(self.batch)
        self.db.flush()

        for index in range(2):
            self.db.add(
                BusinessRecord(
                    user_id=uploader.id,
                    batch_id=self.batch.id,
                    business_no=f"legacy-{index}",
                    name=f"客户{index}",
                    phone=f"1380000000{index}",
                    plate_number=f"沪A0000{index}",
                    points_amount=100.0,
                    bank_card=f"622200000000000{index}",
                    redemption_mode=(
                        BusinessChannel.CASH_REBATE.value
                    ),
                    claim_status=None,
                )
            )
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def test_correction_updates_batch_and_every_record_snapshot(self):
        snapshot, count = correct_pending_batch_channel(
            self.db,
            batch=self.batch,
            redemption_mode=(
                BusinessChannel.MALL_REDEMPTION.value
            ),
            claim_deadline_text="2026-12-31",
            now=datetime(2026, 9, 3, 9, 0, 0),
        )
        self.db.commit()

        records = (
            self.db.query(BusinessRecord)
            .filter(BusinessRecord.batch_id == self.batch.id)
            .all()
        )

        self.assertEqual(count, 2)
        self.assertEqual(
            self.batch.redemption_mode,
            snapshot.redemption_mode,
        )
        self.assertEqual(
            self.batch.claim_deadline,
            snapshot.claim_deadline,
        )
        self.assertTrue(
            all(
                record.redemption_mode
                == BusinessChannel.MALL_REDEMPTION.value
                and record.claim_status
                == BusinessClaimStatus.PENDING_ACTIVATION.value
                for record in records
            )
        )

    def test_correction_back_to_cash_clears_mall_fields(self):
        correct_pending_batch_channel(
            self.db,
            batch=self.batch,
            redemption_mode=(
                BusinessChannel.MALL_REDEMPTION.value
            ),
            claim_deadline_text="2026-12-31",
            now=datetime(2026, 9, 3, 9, 0, 0),
        )
        snapshot, count = correct_pending_batch_channel(
            self.db,
            batch=self.batch,
            redemption_mode=(
                BusinessChannel.CASH_REBATE.value
            ),
            claim_deadline_text="",
            now=datetime(2026, 9, 3, 9, 0, 0),
        )

        self.assertEqual(count, 2)
        self.assertIsNone(snapshot.claim_deadline)
        self.assertTrue(
            all(
                record.claim_status is None
                for record in self.db.query(BusinessRecord).all()
            )
        )

    def test_correction_rejects_processed_or_same_channel(self):
        with self.assertRaisesRegex(ValueError, "必须与当前渠道不同"):
            correct_pending_batch_channel(
                self.db,
                batch=self.batch,
                redemption_mode=(
                    BusinessChannel.CASH_REBATE.value
                ),
            )

        self.batch.acceptance_status = "已承接"

        with self.assertRaisesRegex(ValueError, "只有待承接批次"):
            correct_pending_batch_channel(
                self.db,
                batch=self.batch,
                redemption_mode=(
                    BusinessChannel.MALL_REDEMPTION.value
                ),
                claim_deadline_text="2026-12-31",
                now=datetime(2026, 9, 3, 9, 0, 0),
            )

    def test_decision_only_allows_pending_batch(self):
        decide_pending_batch(
            self.batch,
            ACCEPTED_BATCH_STATUS,
        )
        self.assertEqual(
            self.batch.acceptance_status,
            ACCEPTED_BATCH_STATUS,
        )

        with self.assertRaisesRegex(ValueError, "只有待承接批次"):
            decide_pending_batch(
                self.batch,
                REJECTED_BATCH_STATUS,
            )

    def test_revert_rejected_or_safe_accepted_batch(self):
        self.batch.acceptance_status = REJECTED_BATCH_STATUS
        previous_status = revert_batch_decision(
            self.db,
            batch=self.batch,
        )
        self.assertEqual(previous_status, REJECTED_BATCH_STATUS)
        self.assertEqual(
            self.batch.acceptance_status,
            PENDING_BATCH_STATUS,
        )

        self.batch.acceptance_status = ACCEPTED_BATCH_STATUS
        previous_status = revert_batch_decision(
            self.db,
            batch=self.batch,
        )
        self.assertEqual(previous_status, ACCEPTED_BATCH_STATUS)
        self.assertEqual(
            self.batch.acceptance_status,
            PENDING_BATCH_STATUS,
        )

    def test_revert_accepted_batch_fails_closed_after_review(self):
        record = self.db.query(BusinessRecord).first()
        self.db.add(
            MatchReview(
                business_record_id=record.id,
                review_status="待审核",
            )
        )
        self.batch.acceptance_status = ACCEPTED_BATCH_STATUS
        self.db.commit()

        reason = batch_revert_block_reason(
            self.db,
            batch=self.batch,
        )
        self.assertIn("审核记录", reason)

        with self.assertRaisesRegex(ValueError, "审核记录"):
            revert_batch_decision(
                self.db,
                batch=self.batch,
            )

        self.assertEqual(
            self.batch.acceptance_status,
            ACCEPTED_BATCH_STATUS,
        )

    def test_revert_mall_batch_fails_closed_after_claim_progress(self):
        records = self.db.query(BusinessRecord).all()
        self.batch.redemption_mode = (
            BusinessChannel.MALL_REDEMPTION.value
        )
        self.batch.acceptance_status = ACCEPTED_BATCH_STATUS
        for record in records:
            record.redemption_mode = (
                BusinessChannel.MALL_REDEMPTION.value
            )
            record.claim_status = (
                BusinessClaimStatus.PENDING_ACTIVATION.value
            )
        records[0].claim_status = BusinessClaimStatus.ACTIVATED.value
        self.db.commit()

        with self.assertRaisesRegex(ValueError, "进入领取处理"):
            revert_batch_decision(self.db, batch=self.batch)

    def test_revert_mall_batch_fails_closed_after_points_grant(self):
        records = self.db.query(BusinessRecord).all()
        self.batch.redemption_mode = (
            BusinessChannel.MALL_REDEMPTION.value
        )
        self.batch.acceptance_status = ACCEPTED_BATCH_STATUS
        for record in records:
            record.redemption_mode = (
                BusinessChannel.MALL_REDEMPTION.value
            )
            record.claim_status = (
                BusinessClaimStatus.PENDING_ACTIVATION.value
            )
        self.db.add(
            PointsGrant(
                account_id=1,
                business_record_id=records[0].id,
                granted_points=100,
                available_points=100,
                reserved_points=0,
                activated_at=datetime(2026, 9, 3, 9, 0, 0),
                expires_at=datetime(2027, 9, 3, 9, 0, 0),
                status="ACTIVE",
            )
        )
        self.db.commit()

        with self.assertRaisesRegex(ValueError, "已经产生积分权益"):
            revert_batch_decision(self.db, batch=self.batch)


if __name__ == "__main__":
    unittest.main()
