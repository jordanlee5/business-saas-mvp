import unittest
from decimal import Decimal
from types import SimpleNamespace

from audit_voucher_assignment_conflicts import (
    HISTORICAL_DUPLICATE_ACTION,
    MANUAL_DISPOSITION_COLUMNS,
    MULTIPLE_SAFE_CANDIDATE_ACTION,
    NO_SAFE_CANDIDATE_ACTION,
    UNIQUE_SAFE_CANDIDATE_ACTION,
    allocation_state,
    amount_safe_business_ids,
    amount_safe_group_category,
    build_manual_disposition_row,
    candidate_allocation_state,
    continuable_business_ids,
    group_category,
    remaining_amount,
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


if __name__ == "__main__":
    unittest.main()
