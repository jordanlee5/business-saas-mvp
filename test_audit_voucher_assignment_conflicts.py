import csv
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from audit_voucher_assignment_conflicts import (
    HISTORICAL_DUPLICATE_ACTION,
    MANUAL_DISPOSITION_COLUMNS,
    ManualDispositionCsvValidationError,
    MULTIPLE_SAFE_CANDIDATE_ACTION,
    NO_SAFE_CANDIDATE_ACTION,
    UNIQUE_SAFE_CANDIDATE_ACTION,
    HISTORICAL_CASE_TYPE,
    HISTORICAL_KEEP_DECISION,
    HISTORICAL_REVOKE_DECISION,
    UNRESOLVED_CASE_TYPE,
    UNRESOLVED_CONFIRM_DECISION,
    UNRESOLVED_EXCLUDE_DECISION,
    allocation_state,
    amount_safe_business_ids,
    amount_safe_group_category,
    build_historical_manual_disposition_rows,
    build_manual_disposition_row,
    build_manual_disposition_rows,
    build_unresolved_manual_disposition_rows,
    candidate_allocation_state,
    continuable_business_ids,
    export_manual_disposition_csv,
    group_category,
    remaining_amount,
    validate_manual_disposition_csv,
    validate_manual_disposition_groups,
    unresolved_group_action,
)


def make_business(
    business_amount,
    approved_amount,
    missing_amount_count=0,
    business_id=1,
    business_no="PUBLIC-001",
):
    return {
        "business_id": business_id,
        "business_no": business_no,
        "business_amount": business_amount,
        "approved_amount": approved_amount,
        "missing_amount_count": missing_amount_count,
    }


def make_review(
    *,
    review_id,
    voucher_id,
    business_record_id,
    review_status,
    allocation_amount,
    match_status,
    name_match,
    bank_match,
    amount_match,
    score,
    created_at,
    primary_reviewer_id=None,
    primary_review_result=None,
    primary_reviewed_at=None,
    secondary_reviewer_id=None,
    secondary_review_result=None,
    secondary_reviewed_at=None,
):
    return SimpleNamespace(
        id=review_id,
        voucher_id=voucher_id,
        business_record_id=business_record_id,
        review_status=review_status,
        allocation_amount=allocation_amount,
        match_status=match_status,
        name_match=name_match,
        bank_match=bank_match,
        amount_match=amount_match,
        score=score,
        created_at=created_at,
        primary_reviewer_id=primary_reviewer_id,
        primary_review_result=primary_review_result,
        primary_reviewed_at=primary_reviewed_at,
        secondary_reviewer_id=secondary_reviewer_id,
        secondary_review_result=secondary_review_result,
        secondary_reviewed_at=secondary_reviewed_at,
    )


def make_group(*business_ids):
    return SimpleNamespace(
        pending_business_record_ids=tuple(business_ids),
    )


def make_audit_group(
    voucher_id,
    *,
    review_ids=(),
    unresolved_review_ids=(),
    pending_business_record_ids=(),
):
    return SimpleNamespace(
        voucher_id=voucher_id,
        review_ids=tuple(review_ids),
        unresolved_review_ids=tuple(
            unresolved_review_ids
        ),
        pending_business_record_ids=tuple(
            pending_business_record_ids
        ),
    )


def make_manual_disposition_csv_row():
    row = dict.fromkeys(
        MANUAL_DISPOSITION_COLUMNS,
        "",
    )
    row.update(
        {
            "案例编号": "D1",
            "案例类型": "未决冲突",
            "凭证ID": 6,
            "凭证文件名": "voucher-006.png",
            "凭证金额": Decimal("60.00"),
            "审核记录ID": None,
            "业务ID": 8,
            "公开业务单号": "PUBLIC-0008",
            "业务金额": Decimal("100.00"),
            "已核销金额": Decimal("120.00"),
            "剩余金额": Decimal("-20.00"),
        }
    )

    return row


def make_semantic_disposition_row(
    *,
    case_no="D1",
    case_type=UNRESOLVED_CASE_TYPE,
    voucher_id=6,
    review_id=31,
    business_id=8,
    amount_safety="金额安全候选",
    decision="",
    confirmed_by="",
    confirmed_at="",
):
    row = make_manual_disposition_csv_row()
    row.update(
        {
            "案例编号": case_no,
            "案例类型": case_type,
            "凭证ID": voucher_id,
            "凭证文件名": (
                f"voucher-{voucher_id:03d}.png"
            ),
            "审核记录ID": review_id,
            "业务ID": business_id,
            "公开业务单号": (
                f"PUBLIC-{business_id:04d}"
            ),
            "业务金额": Decimal("100.00"),
            "已核销金额": Decimal("40.00"),
            "剩余金额": Decimal("60.00"),
            "业务核销状态": "部分付款",
            "金额安全性": amount_safety,
            "人工决定": decision,
            "确认人": confirmed_by,
            "确认时间": confirmed_at,
        }
    )

    return row


class VoucherAssignmentConflictAuditTests(unittest.TestCase):
    def test_allocation_state_distinguishes_all_boundaries(self):
        cases = (
            (make_business(100, 0), "未付款"),
            (make_business(100, 40), "部分付款"),
            (make_business(100, 100), "已结清"),
            (make_business(100, 120), "超额核销"),
            (make_business(100, -1), "金额异常"),
            (make_business(100, 0, 1), "金额异常"),
        )

        for business, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(
                    allocation_state(business),
                    expected,
                )

    def test_missing_business_is_an_amount_error(self):
        self.assertEqual(
            candidate_allocation_state(None),
            "金额异常",
        )

    def test_group_has_no_continuable_candidate(self):
        group = make_group(1, 2, 3)
        businesses = {
            1: make_business(100, 100),
            2: make_business(100, 120),
            3: make_business(100, 0, 1),
        }

        self.assertEqual(
            continuable_business_ids(group, businesses),
            (),
        )
        self.assertEqual(
            group_category(group, businesses),
            "无可继续核销候选",
        )

    def test_group_has_one_continuable_candidate(self):
        group = make_group(1, 2, 3)
        businesses = {
            1: make_business(100, 100),
            2: make_business(100, 40),
            3: make_business(100, 120),
        }

        self.assertEqual(
            continuable_business_ids(group, businesses),
            (2,),
        )
        self.assertEqual(
            group_category(group, businesses),
            "仅一个可继续核销候选",
        )

    def test_group_has_multiple_continuable_candidates(self):
        group = make_group(1, 2, 3)
        businesses = {
            1: make_business(100, 0),
            2: make_business(100, 40),
            3: make_business(100, 100),
        }

        self.assertEqual(
            continuable_business_ids(group, businesses),
            (1, 2),
        )
        self.assertEqual(
            group_category(group, businesses),
            "多个可继续核销候选",
        )


    def test_exact_remaining_amount_is_amount_safe(self):
        group = make_group(1)
        businesses = {
            1: make_business(100, 40),
        }

        self.assertEqual(
            remaining_amount(businesses[1]),
            Decimal("60.00"),
        )
        self.assertEqual(
            amount_safe_business_ids(
                group,
                businesses,
                60,
            ),
            (1,),
        )
        self.assertEqual(
            amount_safe_group_category(
                group,
                businesses,
                60,
            ),
            "仅一个金额安全候选",
        )

    def test_amount_above_remaining_is_not_amount_safe(self):
        group = make_group(1)
        businesses = {
            1: make_business(100, 40),
        }

        self.assertEqual(
            amount_safe_business_ids(
                group,
                businesses,
                60.01,
            ),
            (),
        )
        self.assertEqual(
            amount_safe_group_category(
                group,
                businesses,
                60.01,
            ),
            "无金额安全候选",
        )

    def test_amount_safety_can_leave_one_candidate(self):
        group = make_group(1, 2)
        businesses = {
            1: make_business(100, 40),
            2: make_business(100, 0),
        }

        self.assertEqual(
            amount_safe_business_ids(
                group,
                businesses,
                80,
            ),
            (2,),
        )
        self.assertEqual(
            amount_safe_group_category(
                group,
                businesses,
                80,
            ),
            "仅一个金额安全候选",
        )

    def test_amount_safety_can_keep_multiple_candidates(self):
        group = make_group(1, 2)
        businesses = {
            1: make_business(100, 40),
            2: make_business(100, 0),
        }

        self.assertEqual(
            amount_safe_business_ids(
                group,
                businesses,
                50,
            ),
            (1, 2),
        )
        self.assertEqual(
            amount_safe_group_category(
                group,
                businesses,
                50,
            ),
            "多个金额安全候选",
        )

    def test_invalid_voucher_amount_has_no_safe_candidate(self):
        group = make_group(1)
        businesses = {
            1: make_business(100, 0),
        }

        for voucher_amount in (None, 0, -1):
            with self.subTest(voucher_amount=voucher_amount):
                self.assertEqual(
                    amount_safe_business_ids(
                        group,
                        businesses,
                        voucher_amount,
                    ),
                    (),
                )
                self.assertEqual(
                    amount_safe_group_category(
                        group,
                        businesses,
                        voucher_amount,
                    ),
                    "无金额安全候选",
                )


    def test_no_safe_candidate_is_blocked(self):
        group = make_group(1)
        businesses = {
            1: make_business(100, 40),
        }

        self.assertEqual(
            unresolved_group_action(
                group,
                businesses,
                60.01,
            ),
            NO_SAFE_CANDIDATE_ACTION,
        )

    def test_unique_safe_candidate_still_requires_review(self):
        group = make_group(1, 2)
        businesses = {
            1: make_business(100, 40),
            2: make_business(100, 0),
        }

        self.assertEqual(
            unresolved_group_action(
                group,
                businesses,
                80,
            ),
            UNIQUE_SAFE_CANDIDATE_ACTION,
        )

    def test_multiple_safe_candidates_require_manual_assignment(self):
        group = make_group(1, 2)
        businesses = {
            1: make_business(100, 40),
            2: make_business(100, 0),
        }

        self.assertEqual(
            unresolved_group_action(
                group,
                businesses,
                50,
            ),
            MULTIPLE_SAFE_CANDIDATE_ACTION,
        )


    def test_manual_disposition_row_contains_complete_evidence(self):
        voucher = {
            "id": 6,
            "filename": "voucher-006.png",
            "voucher_amount": 60,
        }
        business = make_business(
            100,
            40,
            business_id=8,
            business_no="PUBLIC-0008",
        )
        review = make_review(
            review_id=31,
            voucher_id=6,
            business_record_id=8,
            review_status="待初审",
            allocation_amount=60,
            match_status="候选匹配",
            name_match="姓名完全匹配",
            bank_match="银行卡未匹配",
            amount_match="金额完全匹配",
            score=3,
            created_at="2026-08-05 10:00:00",
        )

        row = build_manual_disposition_row(
            case_no="D1",
            case_type="未决冲突",
            voucher_id=6,
            voucher=voucher,
            review=review,
            business=business,
            action=UNIQUE_SAFE_CANDIDATE_ACTION,
        )

        self.assertEqual(
            tuple(row),
            MANUAL_DISPOSITION_COLUMNS,
        )
        self.assertEqual(
            row,
            {
                "案例编号": "D1",
                "案例类型": "未决冲突",
                "凭证ID": 6,
                "凭证文件名": "voucher-006.png",
                "凭证金额": Decimal("60.00"),
                "审核记录ID": 31,
                "审核状态": "待初审",
                "本次核销金额": Decimal("60.00"),
                "审核记录创建时间": (
                    "2026-08-05 10:00:00"
                ),
                "业务ID": 8,
                "公开业务单号": "PUBLIC-0008",
                "业务金额": Decimal("100.00"),
                "已核销金额": Decimal("40.00"),
                "剩余金额": Decimal("60.00"),
                "业务核销状态": "部分付款",
                "金额安全性": "金额安全候选",
                "匹配分数": 3,
                "匹配状态": "候选匹配",
                "姓名匹配证据": "姓名完全匹配",
                "银行卡匹配证据": "银行卡未匹配",
                "金额匹配证据": "金额完全匹配",
                "初审人ID": None,
                "初审结果": None,
                "初审时间": None,
                "复核人ID": None,
                "复核结果": None,
                "复核时间": None,
                "处置建议": (
                    UNIQUE_SAFE_CANDIDATE_ACTION
                ),
                "人工决定": "",
                "确认人": "",
                "确认时间": "",
                "备注": "",
            },
        )
        self.assertNotIn("建议归属业务ID", row)

    def test_historical_row_preserves_review_chain_and_risk(self):
        voucher = {
            "id": 45,
            "filename": "voucher-045.png",
            "voucher_amount": 60,
        }
        business = make_business(
            100,
            120,
            business_id=9,
            business_no="PUBLIC-0009",
        )
        review = make_review(
            review_id=90,
            voucher_id=45,
            business_record_id=9,
            review_status="已通过",
            allocation_amount=60,
            match_status="高可信匹配",
            name_match="姓名完全匹配",
            bank_match="银行卡完全匹配",
            amount_match="金额完全匹配",
            score=5,
            created_at="2026-08-01 09:00:00",
            primary_reviewer_id=2,
            primary_review_result="通过",
            primary_reviewed_at="2026-08-01 09:10:00",
            secondary_reviewer_id=3,
            secondary_review_result="通过",
            secondary_reviewed_at="2026-08-01 09:20:00",
        )

        row = build_manual_disposition_row(
            case_no="E1",
            case_type="历史重复通过",
            voucher_id=45,
            voucher=voucher,
            review=review,
            business=business,
            action=HISTORICAL_DUPLICATE_ACTION,
        )

        self.assertEqual(
            row["剩余金额"],
            Decimal("-20.00"),
        )
        self.assertEqual(
            row["业务核销状态"],
            "超额核销",
        )
        self.assertEqual(
            row["金额安全性"],
            "非金额安全候选",
        )
        self.assertEqual(row["初审人ID"], 2)
        self.assertEqual(row["复核人ID"], 3)
        self.assertEqual(
            row["处置建议"],
            HISTORICAL_DUPLICATE_ACTION,
        )
        self.assertEqual(
            (
                row["人工决定"],
                row["确认人"],
                row["确认时间"],
                row["备注"],
            ),
            ("", "", "", ""),
        )
        self.assertNotIn("建议归属业务ID", row)


    def test_unresolved_groups_expand_every_candidate_business(self):
        groups = (
            make_audit_group(
                6,
                unresolved_review_ids=(31, 32),
                pending_business_record_ids=(8, 9),
            ),
            make_audit_group(
                7,
                unresolved_review_ids=(33,),
                pending_business_record_ids=(10, 11),
            ),
        )
        vouchers = {
            6: {
                "filename": "voucher-006.png",
                "voucher_amount": 60,
            },
            7: {
                "filename": "voucher-007.png",
                "voucher_amount": 200,
            },
        }
        businesses = {
            8: make_business(
                100,
                40,
                business_id=8,
                business_no="PUBLIC-0008",
            ),
            9: make_business(
                80,
                0,
                business_id=9,
                business_no="PUBLIC-0009",
            ),
            10: make_business(
                100,
                0,
                business_id=10,
                business_no="PUBLIC-0010",
            ),
            11: make_business(
                100,
                0,
                business_id=11,
                business_no="PUBLIC-0011",
            ),
        }
        reviews_by_id = {
            31: make_review(
                review_id=31,
                voucher_id=6,
                business_record_id=8,
                review_status="待初审",
                allocation_amount=60,
                match_status="候选匹配",
                name_match="匹配",
                bank_match="未匹配",
                amount_match="匹配",
                score=3,
                created_at="2026-08-05 10:00:00",
            ),
            32: make_review(
                review_id=32,
                voucher_id=6,
                business_record_id=9,
                review_status="待初审",
                allocation_amount=60,
                match_status="候选匹配",
                name_match="匹配",
                bank_match="未匹配",
                amount_match="匹配",
                score=3,
                created_at="2026-08-05 10:01:00",
            ),
            33: make_review(
                review_id=33,
                voucher_id=7,
                business_record_id=10,
                review_status="待初审",
                allocation_amount=200,
                match_status="候选匹配",
                name_match="匹配",
                bank_match="未匹配",
                amount_match="未匹配",
                score=2,
                created_at="2026-08-05 10:02:00",
            ),
        }

        rows = build_unresolved_manual_disposition_rows(
            groups,
            vouchers,
            reviews_by_id,
            businesses,
        )

        self.assertEqual(
            [row["案例编号"] for row in rows],
            ["D1", "D1", "D2", "D2"],
        )
        self.assertEqual(
            [row["业务ID"] for row in rows],
            [8, 9, 10, 11],
        )
        self.assertEqual(
            [row["审核记录ID"] for row in rows],
            [31, 32, 33, None],
        )
        self.assertEqual(
            [row["处置建议"] for row in rows],
            [
                MULTIPLE_SAFE_CANDIDATE_ACTION,
                MULTIPLE_SAFE_CANDIDATE_ACTION,
                NO_SAFE_CANDIDATE_ACTION,
                NO_SAFE_CANDIDATE_ACTION,
            ],
        )
        self.assertTrue(
            all(
                tuple(row) == MANUAL_DISPOSITION_COLUMNS
                for row in rows
            )
        )
        self.assertNotIn("建议归属业务ID", rows[0])

    def test_historical_groups_include_only_approved_relations(self):
        group = make_audit_group(
            45,
            review_ids=(90, 91, 92),
        )
        vouchers = {
            45: {
                "filename": "voucher-045.png",
                "voucher_amount": 60,
            }
        }
        businesses = {
            business_id: make_business(
                100,
                60,
                business_id=business_id,
                business_no=f"PUBLIC-{business_id:04d}",
            )
            for business_id in (9, 10, 11)
        }
        reviews_by_id = {
            review_id: make_review(
                review_id=review_id,
                voucher_id=45,
                business_record_id=business_id,
                review_status=review_status,
                allocation_amount=60,
                match_status="高可信匹配",
                name_match="匹配",
                bank_match="匹配",
                amount_match="匹配",
                score=5,
                created_at="2026-08-01 09:00:00",
                primary_reviewer_id=2,
                primary_review_result="通过",
                primary_reviewed_at=(
                    "2026-08-01 09:10:00"
                ),
                secondary_reviewer_id=3,
                secondary_review_result="通过",
                secondary_reviewed_at=(
                    "2026-08-01 09:20:00"
                ),
            )
            for review_id, business_id, review_status in (
                (90, 9, "已通过"),
                (91, 10, "已通过"),
                (92, 11, "待初审"),
            )
        }

        rows = build_historical_manual_disposition_rows(
            (group,),
            vouchers,
            reviews_by_id,
            businesses,
        )

        self.assertEqual(len(rows), 2)
        self.assertEqual(
            [row["案例编号"] for row in rows],
            ["E1", "E1"],
        )
        self.assertEqual(
            [row["审核记录ID"] for row in rows],
            [90, 91],
        )
        self.assertEqual(
            {row["处置建议"] for row in rows},
            {HISTORICAL_DUPLICATE_ACTION},
        )
        self.assertTrue(
            all(row["初审人ID"] == 2 for row in rows)
        )
        self.assertTrue(
            all(row["复核人ID"] == 3 for row in rows)
        )

    def test_combined_rows_keep_d_and_e_case_series_separate(self):
        unresolved_group = make_audit_group(
            6,
            unresolved_review_ids=(31, 32),
            pending_business_record_ids=(8, 9),
        )
        historical_group = make_audit_group(
            45,
            review_ids=(90, 91),
        )
        vouchers = {
            6: {
                "filename": "voucher-006.png",
                "voucher_amount": 60,
            },
            45: {
                "filename": "voucher-045.png",
                "voucher_amount": 60,
            },
        }
        businesses = {
            business_id: make_business(
                100,
                0,
                business_id=business_id,
                business_no=f"PUBLIC-{business_id:04d}",
            )
            for business_id in (8, 9, 10, 11)
        }
        reviews_by_id = {}

        for review_id, voucher_id, business_id, status in (
            (31, 6, 8, "待初审"),
            (32, 6, 9, "待初审"),
            (90, 45, 10, "已通过"),
            (91, 45, 11, "已通过"),
        ):
            reviews_by_id[review_id] = make_review(
                review_id=review_id,
                voucher_id=voucher_id,
                business_record_id=business_id,
                review_status=status,
                allocation_amount=60,
                match_status="候选匹配",
                name_match="匹配",
                bank_match="匹配",
                amount_match="匹配",
                score=3,
                created_at="2026-08-05 10:00:00",
            )

        rows = build_manual_disposition_rows(
            (unresolved_group,),
            (historical_group,),
            vouchers,
            reviews_by_id,
            businesses,
        )

        self.assertEqual(
            [row["案例编号"] for row in rows],
            ["D1", "D1", "E1", "E1"],
        )
        self.assertEqual(
            [row["案例类型"] for row in rows],
            [
                "未决冲突",
                "未决冲突",
                "历史重复通过",
                "历史重复通过",
            ],
        )


    def test_csv_export_uses_utf8_bom_and_exact_columns(self):
        row = make_manual_disposition_csv_row()

        with tempfile.TemporaryDirectory() as temp_dir:
            output_path = (
                Path(temp_dir) / "manual-disposition.csv"
            )
            exported_path = export_manual_disposition_csv(
                (row,),
                output_path,
            )
            raw = output_path.read_bytes()

            with output_path.open(
                encoding="utf-8-sig",
                newline="",
            ) as csv_file:
                reader = csv.DictReader(csv_file)
                exported_rows = list(reader)

        self.assertEqual(exported_path, output_path)
        self.assertTrue(raw.startswith(b"\xef\xbb\xbf"))
        self.assertEqual(
            reader.fieldnames,
            list(MANUAL_DISPOSITION_COLUMNS),
        )
        self.assertEqual(len(exported_rows), 1)
        self.assertEqual(
            exported_rows[0]["凭证金额"],
            "60.00",
        )
        self.assertEqual(
            exported_rows[0]["审核记录ID"],
            "",
        )
        self.assertEqual(
            exported_rows[0]["剩余金额"],
            "-20.00",
        )
        self.assertEqual(
            (
                exported_rows[0]["人工决定"],
                exported_rows[0]["确认人"],
                exported_rows[0]["确认时间"],
                exported_rows[0]["备注"],
            ),
            ("", "", "", ""),
        )

    def test_csv_export_escapes_formula_like_text_only(self):
        row = make_manual_disposition_csv_row()
        row["凭证文件名"] = "=dangerous-formula"
        row["公开业务单号"] = "+dangerous-formula"

        with tempfile.TemporaryDirectory() as temp_dir:
            output_path = (
                Path(temp_dir) / "manual-disposition.csv"
            )
            export_manual_disposition_csv(
                (row,),
                output_path,
            )

            with output_path.open(
                encoding="utf-8-sig",
                newline="",
            ) as csv_file:
                exported_row = next(
                    csv.DictReader(csv_file)
                )

        self.assertEqual(
            exported_row["凭证文件名"],
            "'=dangerous-formula",
        )
        self.assertEqual(
            exported_row["公开业务单号"],
            "'+dangerous-formula",
        )
        self.assertEqual(
            exported_row["剩余金额"],
            "-20.00",
        )

    def test_csv_export_refuses_to_overwrite_existing_file(self):
        row = make_manual_disposition_csv_row()

        with tempfile.TemporaryDirectory() as temp_dir:
            output_path = (
                Path(temp_dir) / "manual-disposition.csv"
            )
            original_content = "人工已填写内容"
            output_path.write_text(
                original_content,
                encoding="utf-8",
            )

            with self.assertRaises(FileExistsError):
                export_manual_disposition_csv(
                    (row,),
                    output_path,
                )

            self.assertEqual(
                output_path.read_text(encoding="utf-8"),
                original_content,
            )

    def test_csv_export_rejects_wrong_columns_before_creation(self):
        row = make_manual_disposition_csv_row()
        row["额外字段"] = "unexpected"

        with tempfile.TemporaryDirectory() as temp_dir:
            output_path = (
                Path(temp_dir) / "manual-disposition.csv"
            )

            with self.assertRaisesRegex(
                ValueError,
                "字段不完整或顺序不一致",
            ):
                export_manual_disposition_csv(
                    (row,),
                    output_path,
                )

            self.assertFalse(output_path.exists())


    def test_csv_validation_accepts_manual_fields_without_mutation(
        self,
    ):
        expected_row = make_semantic_disposition_row()
        filled_row = dict(expected_row)
        filled_row.update(
            {
                "人工决定": (
                    UNRESOLVED_CONFIRM_DECISION
                ),
                "确认人": "管理员A",
                "确认时间": "2026-08-06 16:30:00",
                "备注": "仅用于校验测试",
            }
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            output_path = (
                Path(temp_dir) / "manual-disposition.csv"
            )
            export_manual_disposition_csv(
                (filled_row,),
                output_path,
            )
            before_validation = output_path.read_bytes()

            result = validate_manual_disposition_csv(
                (expected_row,),
                output_path,
            )

            after_validation = output_path.read_bytes()

        self.assertEqual(result.row_count, 1)
        self.assertEqual(
            result.manual_filled_row_count,
            1,
        )
        self.assertEqual(
            result.completed_group_count,
            1,
        )
        self.assertEqual(
            result.pending_group_count,
            0,
        )
        self.assertEqual(
            before_validation,
            after_validation,
        )

    def test_csv_validation_ignores_row_order(self):
        first_row = make_manual_disposition_csv_row()
        second_row = dict(first_row)
        second_row.update(
            {
                "案例编号": "D2",
                "凭证ID": 7,
                "凭证文件名": "voucher-007.png",
                "业务ID": 9,
                "公开业务单号": "PUBLIC-0009",
            }
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            output_path = (
                Path(temp_dir) / "manual-disposition.csv"
            )
            export_manual_disposition_csv(
                (second_row, first_row),
                output_path,
            )

            result = validate_manual_disposition_csv(
                (first_row, second_row),
                output_path,
            )

        self.assertEqual(result.row_count, 2)
        self.assertEqual(
            result.completed_group_count,
            0,
        )
        self.assertEqual(
            result.pending_group_count,
            2,
        )

    def test_csv_validation_rejects_modified_evidence(self):
        expected_row = make_manual_disposition_csv_row()
        modified_row = dict(expected_row)
        modified_row["公开业务单号"] = (
            "PUBLIC-TAMPERED"
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            output_path = (
                Path(temp_dir) / "manual-disposition.csv"
            )
            export_manual_disposition_csv(
                (modified_row,),
                output_path,
            )

            with self.assertRaisesRegex(
                ManualDispositionCsvValidationError,
                "缺失 1 行.*变更 1 行",
            ):
                validate_manual_disposition_csv(
                    (expected_row,),
                    output_path,
                )

    def test_csv_validation_rejects_missing_or_duplicate_rows(
        self,
    ):
        first_row = make_manual_disposition_csv_row()
        second_row = dict(first_row)
        second_row.update(
            {
                "案例编号": "D2",
                "凭证ID": 7,
                "凭证文件名": "voucher-007.png",
                "业务ID": 9,
                "公开业务单号": "PUBLIC-0009",
            }
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            output_path = (
                Path(temp_dir) / "manual-disposition.csv"
            )
            export_manual_disposition_csv(
                (first_row, first_row),
                output_path,
            )

            with self.assertRaisesRegex(
                ManualDispositionCsvValidationError,
                "缺失 1 行.*变更 1 行",
            ):
                validate_manual_disposition_csv(
                    (first_row, second_row),
                    output_path,
                )

    def test_csv_validation_rejects_wrong_header(self):
        expected_row = make_manual_disposition_csv_row()
        wrong_columns = MANUAL_DISPOSITION_COLUMNS[:-1]

        with tempfile.TemporaryDirectory() as temp_dir:
            output_path = (
                Path(temp_dir) / "manual-disposition.csv"
            )

            with output_path.open(
                "w",
                encoding="utf-8-sig",
                newline="",
            ) as csv_file:
                writer = csv.DictWriter(
                    csv_file,
                    fieldnames=wrong_columns,
                )
                writer.writeheader()
                writer.writerow(
                    {
                        column: expected_row[column]
                        for column in wrong_columns
                    }
                )

            with self.assertRaisesRegex(
                ManualDispositionCsvValidationError,
                "列名或顺序不一致",
            ):
                validate_manual_disposition_csv(
                    (expected_row,),
                    output_path,
                )


    def test_manual_group_accepts_complete_unresolved_decision(
        self,
    ):
        rows = (
            make_semantic_disposition_row(
                decision=UNRESOLVED_CONFIRM_DECISION,
                confirmed_by="管理员A",
                confirmed_at="2026-08-06 16:30:00",
            ),
            make_semantic_disposition_row(
                review_id=32,
                business_id=9,
                amount_safety="非金额安全候选",
                decision=UNRESOLVED_EXCLUDE_DECISION,
                confirmed_by="管理员A",
                confirmed_at="2026-08-06 16:30:00",
            ),
        )

        result = validate_manual_disposition_groups(
            rows
        )

        self.assertEqual(
            result.completed_group_count,
            1,
        )
        self.assertEqual(
            result.pending_group_count,
            0,
        )

    def test_manual_group_accepts_complete_historical_decision(
        self,
    ):
        rows = (
            make_semantic_disposition_row(
                case_no="E1",
                case_type=HISTORICAL_CASE_TYPE,
                voucher_id=45,
                review_id=90,
                business_id=10,
                decision=HISTORICAL_KEEP_DECISION,
                confirmed_by="管理员A",
                confirmed_at="2026-08-06 16:40:00",
            ),
            make_semantic_disposition_row(
                case_no="E1",
                case_type=HISTORICAL_CASE_TYPE,
                voucher_id=45,
                review_id=91,
                business_id=11,
                decision=HISTORICAL_REVOKE_DECISION,
                confirmed_by="管理员A",
                confirmed_at="2026-08-06 16:40:00",
            ),
        )

        result = validate_manual_disposition_groups(
            rows
        )

        self.assertEqual(
            result.completed_group_count,
            1,
        )
        self.assertEqual(
            result.pending_group_count,
            0,
        )

    def test_manual_group_rejects_unsupported_decision(
        self,
    ):
        row = make_semantic_disposition_row(
            decision="等待人工确认",
            confirmed_by="管理员A",
            confirmed_at="2026-08-06 16:30:00",
        )

        with self.assertRaisesRegex(
            ManualDispositionCsvValidationError,
            "不允许的人工决定",
        ):
            validate_manual_disposition_groups(
                (row,)
            )

    def test_manual_group_rejects_partial_decisions(
        self,
    ):
        rows = (
            make_semantic_disposition_row(
                decision=UNRESOLVED_CONFIRM_DECISION,
                confirmed_by="管理员A",
                confirmed_at="2026-08-06 16:30:00",
            ),
            make_semantic_disposition_row(
                review_id=32,
                business_id=9,
            ),
        )

        with self.assertRaisesRegex(
            ManualDispositionCsvValidationError,
            "人工决定未完整填写",
        ):
            validate_manual_disposition_groups(
                rows
            )

    def test_unresolved_group_requires_exactly_one_target(
        self,
    ):
        rows = (
            make_semantic_disposition_row(
                decision=UNRESOLVED_CONFIRM_DECISION,
                confirmed_by="管理员A",
                confirmed_at="2026-08-06 16:30:00",
            ),
            make_semantic_disposition_row(
                review_id=32,
                business_id=9,
                decision=UNRESOLVED_CONFIRM_DECISION,
                confirmed_by="管理员A",
                confirmed_at="2026-08-06 16:30:00",
            ),
        )

        with self.assertRaisesRegex(
            ManualDispositionCsvValidationError,
            "必须且只能确认 1 行",
        ):
            validate_manual_disposition_groups(
                rows
            )

    def test_unresolved_target_requires_safety_and_review(
        self,
    ):
        cases = (
            (
                {
                    "金额安全性": "非金额安全候选",
                },
                "必须是金额安全候选",
            ),
            (
                {
                    "审核记录ID": None,
                },
                "缺少审核记录ID",
            ),
        )

        for target_updates, expected_message in cases:
            with self.subTest(
                expected_message=expected_message
            ):
                target = make_semantic_disposition_row(
                    decision=(
                        UNRESOLVED_CONFIRM_DECISION
                    ),
                    confirmed_by="管理员A",
                    confirmed_at=(
                        "2026-08-06 16:30:00"
                    ),
                )
                target.update(target_updates)
                excluded = make_semantic_disposition_row(
                    review_id=32,
                    business_id=9,
                    decision=(
                        UNRESOLVED_EXCLUDE_DECISION
                    ),
                    confirmed_by="管理员A",
                    confirmed_at=(
                        "2026-08-06 16:30:00"
                    ),
                )

                with self.assertRaisesRegex(
                    ManualDispositionCsvValidationError,
                    expected_message,
                ):
                    validate_manual_disposition_groups(
                        (target, excluded)
                    )

    def test_historical_group_requires_exactly_one_kept_row(
        self,
    ):
        rows = (
            make_semantic_disposition_row(
                case_no="E1",
                case_type=HISTORICAL_CASE_TYPE,
                voucher_id=45,
                review_id=90,
                business_id=10,
                decision=HISTORICAL_KEEP_DECISION,
                confirmed_by="管理员A",
                confirmed_at="2026-08-06 16:40:00",
            ),
            make_semantic_disposition_row(
                case_no="E1",
                case_type=HISTORICAL_CASE_TYPE,
                voucher_id=45,
                review_id=91,
                business_id=11,
                decision=HISTORICAL_KEEP_DECISION,
                confirmed_by="管理员A",
                confirmed_at="2026-08-06 16:40:00",
            ),
        )

        with self.assertRaisesRegex(
            ManualDispositionCsvValidationError,
            "必须且只能保留 1 行",
        ):
            validate_manual_disposition_groups(
                rows
            )

    def test_manual_group_validates_confirmation_metadata(
        self,
    ):
        inconsistent_rows = (
            make_semantic_disposition_row(
                decision=UNRESOLVED_CONFIRM_DECISION,
                confirmed_by="管理员A",
                confirmed_at="2026-08-06 16:30:00",
            ),
            make_semantic_disposition_row(
                review_id=32,
                business_id=9,
                decision=UNRESOLVED_EXCLUDE_DECISION,
                confirmed_by="管理员B",
                confirmed_at="2026-08-06 16:30:00",
            ),
        )
        invalid_time_rows = (
            make_semantic_disposition_row(
                decision=UNRESOLVED_CONFIRM_DECISION,
                confirmed_by="管理员A",
                confirmed_at="2026/08/06 16:30",
            ),
            make_semantic_disposition_row(
                review_id=32,
                business_id=9,
                decision=UNRESOLVED_EXCLUDE_DECISION,
                confirmed_by="管理员A",
                confirmed_at="2026/08/06 16:30",
            ),
        )

        cases = (
            (
                inconsistent_rows,
                "必须在组内保持一致",
            ),
            (
                invalid_time_rows,
                "YYYY-MM-DD HH:MM:SS",
            ),
        )

        for rows, expected_message in cases:
            with self.subTest(
                expected_message=expected_message
            ):
                with self.assertRaisesRegex(
                    ManualDispositionCsvValidationError,
                    expected_message,
                ):
                    validate_manual_disposition_groups(
                        rows
                    )


if __name__ == "__main__":
    unittest.main()
