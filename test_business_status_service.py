import unittest

from app.business_status_service import (
    BUSINESS_STATUS_ALL,
    BUSINESS_STATUS_MATCHED_UNSETTLED,
    BUSINESS_STATUS_SETTLED,
    BUSINESS_STATUS_UNMATCHED,
    classify_business_status,
    normalize_business_status_filter,
)
from app.voucher_allocation import (
    ALLOCATION_STATUS_COMPLETED,
    ALLOCATION_STATUS_PARTIAL,
    ALLOCATION_STATUS_UNPAID,
)


class BusinessStatusServiceTests(
    unittest.TestCase
):
    def test_unaccepted_business_has_no_processing_status(
        self,
    ):
        for acceptance_status in (
            "待承接",
            "已拒绝",
        ):
            with self.subTest(
                acceptance_status=acceptance_status
            ):
                self.assertIsNone(
                    classify_business_status(
                        acceptance_status=(
                            acceptance_status
                        ),
                        has_matching_record=True,
                        allocation_status=(
                            ALLOCATION_STATUS_COMPLETED
                        ),
                    )
                )

    def test_accepted_business_without_match_is_unmatched(
        self,
    ):
        self.assertEqual(
            classify_business_status(
                acceptance_status="已承接",
                has_matching_record=False,
                allocation_status=(
                    ALLOCATION_STATUS_UNPAID
                ),
            ),
            BUSINESS_STATUS_UNMATCHED,
        )

    def test_matched_unsettled_business_is_distinct(
        self,
    ):
        self.assertEqual(
            classify_business_status(
                acceptance_status="已承接",
                has_matching_record=True,
                allocation_status=(
                    ALLOCATION_STATUS_PARTIAL
                ),
            ),
            BUSINESS_STATUS_MATCHED_UNSETTLED,
        )

    def test_completed_business_is_settled(
        self,
    ):
        self.assertEqual(
            classify_business_status(
                acceptance_status="已承接",
                has_matching_record=True,
                allocation_status=(
                    ALLOCATION_STATUS_COMPLETED
                ),
            ),
            BUSINESS_STATUS_SETTLED,
        )

    def test_invalid_filter_falls_back_to_all(
        self,
    ):
        self.assertEqual(
            normalize_business_status_filter(
                BUSINESS_STATUS_SETTLED
            ),
            BUSINESS_STATUS_SETTLED,
        )
        self.assertEqual(
            normalize_business_status_filter(
                "未知状态"
            ),
            BUSINESS_STATUS_ALL,
        )


if __name__ == "__main__":
    unittest.main()
