import unittest
from decimal import Decimal
from types import SimpleNamespace

from audit_voucher_assignment_conflicts import (
    allocation_state,
    amount_safe_business_ids,
    amount_safe_group_category,
    candidate_allocation_state,
    continuable_business_ids,
    group_category,
    remaining_amount,
)


def make_business(
    business_amount,
    approved_amount,
    missing_amount_count=0,
):
    return {
        "business_amount": business_amount,
        "approved_amount": approved_amount,
        "missing_amount_count": missing_amount_count,
    }


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


if __name__ == "__main__":
    unittest.main()