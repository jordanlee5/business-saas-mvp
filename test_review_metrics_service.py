import unittest
from types import SimpleNamespace

from app.review_metrics_service import (
    build_review_metrics,
    get_business_review_allocation_status,
)
from app.voucher_allocation import (
    ALLOCATION_STATUS_ABNORMAL,
    ALLOCATION_STATUS_COMPLETED,
    ALLOCATION_STATUS_PARTIAL,
    ALLOCATION_STATUS_UNPAID,
)


def make_review(
    *,
    review_status,
    business_record_id=1,
    allocation_amount=None,
):
    return SimpleNamespace(
        review_status=review_status,
        business_record_id=(
            business_record_id
        ),
        allocation_amount=(
            allocation_amount
        ),
    )


class ReviewMetricsServiceTests(
    unittest.TestCase
):
    def test_counts_two_stage_and_legacy_statuses(
        self,
    ):
        metrics = build_review_metrics(
            [
                make_review(
                    review_status="待审核",
                ),
                make_review(
                    review_status="待初审",
                ),
                make_review(
                    review_status="待复核",
                ),
                make_review(
                    review_status="已通过",
                ),
                make_review(
                    review_status="已驳回",
                ),
            ],
            total_business_records=4,
        )

        self.assertEqual(
            metrics.pending_primary_reviews,
            2,
        )
        self.assertEqual(
            metrics.pending_secondary_reviews,
            1,
        )
        self.assertEqual(
            metrics.pending_reviews,
            3,
        )
        self.assertEqual(
            metrics.approved_reviews,
            1,
        )
        self.assertEqual(
            metrics.rejected_reviews,
            1,
        )
        self.assertEqual(
            metrics.completed_reviews,
            2,
        )

    def test_rates_use_distinct_businesses_and_final_reviews(
        self,
    ):
        metrics = build_review_metrics(
            [
                make_review(
                    review_status="已通过",
                    business_record_id=1,
                ),
                make_review(
                    review_status="已驳回",
                    business_record_id=1,
                ),
                make_review(
                    review_status="已通过",
                    business_record_id=2,
                ),
                make_review(
                    review_status="待复核",
                    business_record_id=3,
                ),
            ],
            total_business_records=4,
        )

        self.assertEqual(
            metrics.matched_business_count,
            3,
        )
        self.assertEqual(
            metrics.business_match_coverage_rate,
            75.0,
        )
        self.assertEqual(
            metrics.review_approval_rate,
            66.67,
        )

    def test_rates_are_none_without_denominator(
        self,
    ):
        metrics = build_review_metrics(
            [],
            total_business_records=0,
        )

        self.assertIsNone(
            metrics.business_match_coverage_rate
        )
        self.assertIsNone(
            metrics.review_approval_rate
        )

    def test_matching_ignores_missing_business_id(
        self,
    ):
        metrics = build_review_metrics(
            [
                make_review(
                    review_status="待初审",
                    business_record_id=None,
                ),
            ],
            total_business_records=2,
        )

        self.assertEqual(
            metrics.matched_business_count,
            0,
        )
        self.assertEqual(
            metrics.business_match_coverage_rate,
            0.0,
        )

    def test_full_allocation_stays_completed_after_rejection(
        self,
    ):
        status = (
            get_business_review_allocation_status(
                100,
                [
                    make_review(
                        review_status="已通过",
                        allocation_amount="40.00",
                    ),
                    make_review(
                        review_status="已通过",
                        allocation_amount="60.00",
                    ),
                    make_review(
                        review_status="已驳回",
                        allocation_amount=None,
                    ),
                ],
            )
        )

        self.assertEqual(
            status,
            ALLOCATION_STATUS_COMPLETED,
        )

    def test_partial_and_unpaid_statuses(
        self,
    ):
        partial_status = (
            get_business_review_allocation_status(
                100,
                [
                    make_review(
                        review_status="已通过",
                        allocation_amount="35.00",
                    ),
                ],
            )
        )
        unpaid_status = (
            get_business_review_allocation_status(
                100,
                [
                    make_review(
                        review_status="已驳回",
                    ),
                ],
            )
        )

        self.assertEqual(
            partial_status,
            ALLOCATION_STATUS_PARTIAL,
        )
        self.assertEqual(
            unpaid_status,
            ALLOCATION_STATUS_UNPAID,
        )

    def test_missing_or_excess_reservation_is_abnormal(
        self,
    ):
        missing_status = (
            get_business_review_allocation_status(
                100,
                [
                    make_review(
                        review_status="已通过",
                        allocation_amount=None,
                    ),
                ],
            )
        )
        excess_status = (
            get_business_review_allocation_status(
                100,
                [
                    make_review(
                        review_status="已通过",
                        allocation_amount="90.00",
                    ),
                    make_review(
                        review_status="待复核",
                        allocation_amount="20.00",
                    ),
                ],
            )
        )

        self.assertEqual(
            missing_status,
            ALLOCATION_STATUS_ABNORMAL,
        )
        self.assertEqual(
            excess_status,
            ALLOCATION_STATUS_ABNORMAL,
        )


if __name__ == "__main__":
    unittest.main()
