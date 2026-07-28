import unittest
from decimal import Decimal

from app.voucher_allocation import (
    BUSINESS_COMPLETED,
    BUSINESS_OVERPAID,
    VOUCHER_ASSIGNED_TO_OTHER_BUSINESS,
    VOUCHER_FULLY_ALLOCATED,
    VOUCHER_OVERALLOCATED,
    calculate_allocation_limits,
    calculate_reserved_allocation_limits,
    get_review_block_reason,
    get_voucher_business_block_reason,
    validate_allocation_amount,
)


class VoucherAllocationTests(
    unittest.TestCase
):
    def test_calculates_both_remaining_limits(self):
        limits = calculate_allocation_limits(
            business_amount="1000",
            approved_business_amount="400",
            voucher_amount="800",
            approved_voucher_amount="100",
        )

        self.assertEqual(
            limits.business_remaining,
            Decimal("600.00"),
        )
        self.assertEqual(
            limits.voucher_remaining,
            Decimal("700.00"),
        )
        self.assertEqual(
            limits.maximum_allocation,
            Decimal("600.00"),
        )

    def test_current_allocation_is_excluded_for_re_review(self):
        limits = calculate_allocation_limits(
            business_amount="1000",
            approved_business_amount="600",
            voucher_amount="800",
            approved_voucher_amount="600",
            current_allocation_amount="200",
        )

        self.assertEqual(
            limits.business_remaining,
            Decimal("600.00"),
        )
        self.assertEqual(
            limits.voucher_remaining,
            Decimal("400.00"),
        )
        self.assertEqual(
            limits.maximum_allocation,
            Decimal("400.00"),
        )

    def test_rejects_current_amount_above_approved_amount(self):
        with self.assertRaises(ValueError):
            calculate_allocation_limits(
                business_amount="1000",
                approved_business_amount="100",
                voucher_amount="800",
                approved_voucher_amount="100",
                current_allocation_amount="200",
            )

    def test_business_completed_blocks_review(self):
        limits = calculate_allocation_limits(
            business_amount="1000",
            approved_business_amount="1000",
            voucher_amount="800",
            approved_voucher_amount="0",
        )

        reason = get_review_block_reason(
            limits
        )

        self.assertIsNotNone(reason)
        self.assertEqual(
            reason.code,
            BUSINESS_COMPLETED,
        )

    def test_business_overpayment_is_reported_as_error(self):
        limits = calculate_allocation_limits(
            business_amount="1000",
            approved_business_amount="1000.01",
            voucher_amount="800",
            approved_voucher_amount="0",
        )

        reason = get_review_block_reason(
            limits
        )

        self.assertIsNotNone(reason)
        self.assertEqual(
            reason.code,
            BUSINESS_OVERPAID,
        )
        self.assertEqual(
            limits.maximum_allocation,
            Decimal("0.00"),
        )

    def test_fully_allocated_voucher_blocks_review(self):
        limits = calculate_allocation_limits(
            business_amount="1000",
            approved_business_amount="0",
            voucher_amount="800",
            approved_voucher_amount="800",
        )

        reason = get_review_block_reason(
            limits
        )

        self.assertIsNotNone(reason)
        self.assertEqual(
            reason.code,
            VOUCHER_FULLY_ALLOCATED,
        )

    def test_voucher_overallocation_is_reported_as_error(self):
        limits = calculate_allocation_limits(
            business_amount="1000",
            approved_business_amount="0",
            voucher_amount="800",
            approved_voucher_amount="800.01",
        )

        reason = get_review_block_reason(
            limits
        )

        self.assertIsNotNone(reason)
        self.assertEqual(
            reason.code,
            VOUCHER_OVERALLOCATED,
        )

    def test_allows_partial_allocation(self):
        limits = calculate_allocation_limits(
            business_amount="1000",
            approved_business_amount="400",
            voucher_amount="800",
            approved_voucher_amount="100",
        )

        result = validate_allocation_amount(
            "250",
            limits,
        )

        self.assertEqual(
            result.allocation_amount,
            Decimal("250.00"),
        )
        self.assertEqual(
            result.business_remaining_after,
            Decimal("350.00"),
        )
        self.assertEqual(
            result.voucher_remaining_after,
            Decimal("450.00"),
        )

    def test_allows_exact_business_completion(self):
        limits = calculate_allocation_limits(
            business_amount="1000",
            approved_business_amount="400",
            voucher_amount="800",
            approved_voucher_amount="100",
        )

        result = validate_allocation_amount(
            "600",
            limits,
        )

        self.assertEqual(
            result.business_remaining_after,
            Decimal("0.00"),
        )
        self.assertEqual(
            result.voucher_remaining_after,
            Decimal("100.00"),
        )

    def test_rounds_allocation_half_up(self):
        limits = calculate_allocation_limits(
            business_amount="200",
            approved_business_amount="0",
            voucher_amount="200",
            approved_voucher_amount="0",
        )

        result = validate_allocation_amount(
            "100.005",
            limits,
        )

        self.assertEqual(
            result.allocation_amount,
            Decimal("100.01"),
        )

    def test_rejects_zero_allocation(self):
        limits = calculate_allocation_limits(
            business_amount="1000",
            approved_business_amount="0",
            voucher_amount="800",
            approved_voucher_amount="0",
        )

        with self.assertRaisesRegex(
            ValueError,
            "必须大于 0",
        ):
            validate_allocation_amount(
                "0",
                limits,
            )

    def test_rejects_amount_above_business_remaining(self):
        limits = calculate_allocation_limits(
            business_amount="1000",
            approved_business_amount="900",
            voucher_amount="800",
            approved_voucher_amount="0",
        )

        with self.assertRaisesRegex(
            ValueError,
            "不能超过业务剩余金额",
        ):
            validate_allocation_amount(
                "100.01",
                limits,
            )

    def test_rejects_amount_above_voucher_remaining(self):
        limits = calculate_allocation_limits(
            business_amount="1000",
            approved_business_amount="0",
            voucher_amount="800",
            approved_voucher_amount="700",
        )

        with self.assertRaisesRegex(
            ValueError,
            "不能超过凭证剩余可分配金额",
        ):
            validate_allocation_amount(
                "100.01",
                limits,
            )

    def test_rejects_invalid_money_value(self):
        with self.assertRaises(ValueError):
            calculate_allocation_limits(
                business_amount="不是金额",
                approved_business_amount="0",
                voucher_amount="800",
                approved_voucher_amount="0",
            )

    def test_rejects_boolean_money_value(self):
        with self.assertRaises(ValueError):
            calculate_allocation_limits(
                business_amount=True,
                approved_business_amount="0",
                voucher_amount="800",
                approved_voucher_amount="0",
            )


    def test_voucher_without_owner_can_enter_review(self):
        reason = get_voucher_business_block_reason(
            current_business_record_id=100,
            assigned_business_record_ids=[],
        )

        self.assertIsNone(reason)

    def test_voucher_can_stay_with_same_business(self):
        reason = get_voucher_business_block_reason(
            current_business_record_id=100,
            assigned_business_record_ids=[
                100,
                100,
            ],
        )

        self.assertIsNone(reason)

    def test_voucher_cannot_move_to_other_business(self):
        reason = get_voucher_business_block_reason(
            current_business_record_id=100,
            assigned_business_record_ids=[200],
        )

        self.assertIsNotNone(reason)
        self.assertEqual(
            reason.code,
            VOUCHER_ASSIGNED_TO_OTHER_BUSINESS,
        )
        self.assertIn(
            "一张凭证只能归属一个业务",
            reason.message,
        )

    def test_any_other_business_blocks_assignment(self):
        reason = get_voucher_business_block_reason(
            current_business_record_id=100,
            assigned_business_record_ids=[
                100,
                200,
            ],
        )

        self.assertIsNotNone(reason)
        self.assertEqual(
            reason.code,
            VOUCHER_ASSIGNED_TO_OTHER_BUSINESS,
        )

    def test_voucher_assignment_requires_valid_ids(self):
        with self.assertRaises(ValueError):
            get_voucher_business_block_reason(
                current_business_record_id=0,
                assigned_business_record_ids=[],
            )

        with self.assertRaises(ValueError):
            get_voucher_business_block_reason(
                current_business_record_id=100,
                assigned_business_record_ids=[
                    True,
                ],
            )


    def test_pending_secondary_amount_reserves_both_limits(self):
        limits = calculate_reserved_allocation_limits(
            current_business_record_id=100,
            assigned_business_record_ids=[100],
            business_amount="1000",
            business_reserved_allocation_amounts=[
                "300",
            ],
            voucher_amount="800",
            voucher_reserved_allocation_amounts=[
                "300",
            ],
        )

        self.assertEqual(
            limits.business_remaining,
            Decimal("700.00"),
        )
        self.assertEqual(
            limits.voucher_remaining,
            Decimal("500.00"),
        )
        self.assertEqual(
            limits.maximum_allocation,
            Decimal("500.00"),
        )

    def test_reserved_limits_allow_same_business(self):
        limits = calculate_reserved_allocation_limits(
            current_business_record_id=100,
            assigned_business_record_ids=[
                100,
                100,
            ],
            business_amount="1000",
            business_reserved_allocation_amounts=[
                "100",
                "200",
            ],
            voucher_amount="900",
            voucher_reserved_allocation_amounts=[
                "200",
            ],
        )

        self.assertEqual(
            limits.maximum_allocation,
            Decimal("700.00"),
        )

    def test_reserved_limits_reject_other_business(self):
        with self.assertRaisesRegex(
            ValueError,
            "一张凭证只能归属一个业务",
        ):
            calculate_reserved_allocation_limits(
                current_business_record_id=100,
                assigned_business_record_ids=[200],
                business_amount="1000",
                business_reserved_allocation_amounts=[],
                voucher_amount="800",
                voucher_reserved_allocation_amounts=[
                    "100",
                ],
            )

    def test_missing_business_reservation_blocks_new_allocation(self):
        with self.assertRaisesRegex(
            ValueError,
            "缺少核销金额",
        ):
            calculate_reserved_allocation_limits(
                current_business_record_id=100,
                assigned_business_record_ids=[100],
                business_amount="1000",
                business_reserved_allocation_amounts=[
                    None,
                ],
                voucher_amount="800",
                voucher_reserved_allocation_amounts=[
                    "100",
                ],
            )

    def test_missing_voucher_reservation_blocks_new_allocation(self):
        with self.assertRaisesRegex(
            ValueError,
            "缺少核销金额",
        ):
            calculate_reserved_allocation_limits(
                current_business_record_id=100,
                assigned_business_record_ids=[100],
                business_amount="1000",
                business_reserved_allocation_amounts=[
                    "100",
                ],
                voucher_amount="800",
                voucher_reserved_allocation_amounts=[
                    None,
                ],
            )


if __name__ == "__main__":
    unittest.main()