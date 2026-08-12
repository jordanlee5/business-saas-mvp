import unittest
from types import SimpleNamespace
from datetime import datetime, timezone
from unittest.mock import patch

from app.admin_permissions import (
    PRIMARY_REVIEWER,
    SECONDARY_REVIEWER,
    SUPER_ADMIN,
)
from app.match_review_workflow import (
    APPROVED_REVIEW_STATUS,
    PENDING_PRIMARY_REVIEW_STATUS,
    PENDING_SECONDARY_REVIEW_STATUS,
    REJECTED_REVIEW_STATUS,
    REVIEW_RESULT_APPROVED,
    REVIEW_RESULT_REJECTED,
    apply_primary_review_decision,
    apply_secondary_review_decision,
    build_voucher_assignment_conflict_audit_groups,
    can_primary_review_match,
    can_secondary_review_match,
    get_hidden_low_confidence_conflict_review_ids,
    get_unresolved_assignment_conflict_review_ids,
    get_hidden_completed_business_review_ids,
)


def make_admin(
    user_id: int,
    admin_level: str,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=user_id,
        role="admin",
        admin_level=admin_level,
    )


def make_review(
    review_status: str,
    primary_reviewer_id: int | None = None,
    review_id: int = 100,
    voucher_id: int = 10,
    business_record_id: int = 20,
    score: int = 1,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=review_id,
        voucher_id=voucher_id,
        business_record_id=business_record_id,
        score=score,
        review_status=review_status,
        primary_reviewer_id=primary_reviewer_id,
    )


class MatchReviewWorkflowTests(unittest.TestCase):
    def test_hides_primary_reviews_for_completed_businesses(self):
        hidden_ids = (
            get_hidden_completed_business_review_ids(
                [
                    make_review(
                        PENDING_PRIMARY_REVIEW_STATUS,
                        review_id=1,
                        business_record_id=100,
                    ),
                    make_review(
                        "待审核",
                        review_id=2,
                        business_record_id=100,
                    ),
                ],
                completed_business_record_ids={
                    100,
                },
            )
        )

        self.assertEqual(
            hidden_ids,
            frozenset({1, 2}),
        )

    def test_hides_secondary_reviews_for_completed_businesses(self):
        hidden_ids = (
            get_hidden_completed_business_review_ids(
                [
                    make_review(
                        PENDING_SECONDARY_REVIEW_STATUS,
                        review_id=3,
                        business_record_id=100,
                    ),
                ],
                completed_business_record_ids={
                    100,
                },
            )
        )

        self.assertEqual(
            hidden_ids,
            frozenset({3}),
        )

    def test_keeps_finalized_and_open_business_reviews_visible(self):
        hidden_ids = (
            get_hidden_completed_business_review_ids(
                [
                    make_review(
                        APPROVED_REVIEW_STATUS,
                        review_id=1,
                        business_record_id=100,
                    ),
                    make_review(
                        REJECTED_REVIEW_STATUS,
                        review_id=2,
                        business_record_id=100,
                    ),
                    make_review(
                        PENDING_PRIMARY_REVIEW_STATUS,
                        review_id=3,
                        business_record_id=200,
                    ),
                ],
                completed_business_record_ids={
                    100,
                },
            )
        )

        self.assertEqual(
            hidden_ids,
            frozenset(),
        )

    def test_groups_cross_business_reviews_by_voucher(self):
        groups = build_voucher_assignment_conflict_audit_groups(
            [
                make_review(
                    PENDING_PRIMARY_REVIEW_STATUS,
                    review_id=3,
                    voucher_id=20,
                    business_record_id=300,
                ),
                make_review(
                    PENDING_PRIMARY_REVIEW_STATUS,
                    review_id=1,
                    voucher_id=10,
                    business_record_id=100,
                ),
                make_review(
                    "待审核",
                    review_id=2,
                    voucher_id=10,
                    business_record_id=200,
                ),
                make_review(
                    PENDING_PRIMARY_REVIEW_STATUS,
                    review_id=4,
                    voucher_id=20,
                    business_record_id=400,
                ),
            ]
        )

        self.assertEqual(
            [group.voucher_id for group in groups],
            [10, 20],
        )
        self.assertEqual(groups[0].review_ids, (1, 2))
        self.assertEqual(
            groups[0].business_record_ids,
            (100, 200),
        )

    def test_excludes_single_business_voucher_group(self):
        groups = build_voucher_assignment_conflict_audit_groups(
            [
                make_review(
                    PENDING_PRIMARY_REVIEW_STATUS,
                    review_id=1,
                    business_record_id=100,
                ),
                make_review(
                    "待审核",
                    review_id=2,
                    business_record_id=100,
                ),
            ]
        )

        self.assertEqual(groups, ())

    def test_marks_unresolved_reviews_in_audit_group(self):
        groups = build_voucher_assignment_conflict_audit_groups(
            [
                make_review(
                    PENDING_PRIMARY_REVIEW_STATUS,
                    review_id=1,
                    business_record_id=100,
                ),
                make_review(
                    "待审核",
                    review_id=2,
                    business_record_id=200,
                ),
                make_review(
                    REJECTED_REVIEW_STATUS,
                    review_id=3,
                    business_record_id=300,
                ),
            ]
        )

        self.assertEqual(
            groups[0].unresolved_review_ids,
            (1, 2),
        )
        self.assertEqual(
            groups[0].pending_business_record_ids,
            (100, 200),
        )

    def test_reservation_clears_unresolved_audit_reviews(self):
        groups = build_voucher_assignment_conflict_audit_groups(
            [
                make_review(
                    PENDING_PRIMARY_REVIEW_STATUS,
                    review_id=1,
                    business_record_id=100,
                ),
                make_review(
                    PENDING_PRIMARY_REVIEW_STATUS,
                    review_id=2,
                    business_record_id=200,
                ),
                make_review(
                    PENDING_SECONDARY_REVIEW_STATUS,
                    review_id=3,
                    business_record_id=100,
                ),
            ]
        )

        self.assertEqual(
            groups[0].unresolved_review_ids,
            (),
        )
        self.assertEqual(
            groups[0].reserved_business_record_ids,
            (100,),
        )

    def test_audits_completed_open_and_multi_approved_businesses(self):
        groups = build_voucher_assignment_conflict_audit_groups(
            [
                make_review(
                    APPROVED_REVIEW_STATUS,
                    review_id=1,
                    business_record_id=100,
                ),
                make_review(
                    APPROVED_REVIEW_STATUS,
                    review_id=2,
                    business_record_id=200,
                ),
                make_review(
                    REJECTED_REVIEW_STATUS,
                    review_id=3,
                    business_record_id=300,
                ),
            ],
            completed_business_record_ids={100, 300},
        )

        group = groups[0]
        self.assertEqual(
            group.approved_business_record_ids,
            (100, 200),
        )
        self.assertEqual(
            group.completed_business_record_ids,
            (100, 300),
        )
        self.assertEqual(
            group.open_business_record_ids,
            (200,),
        )
        self.assertTrue(
            group.has_multiple_approved_businesses
        )

    def test_marks_unresolved_candidates_for_different_businesses(self):
        first_candidate = make_review(
            PENDING_PRIMARY_REVIEW_STATUS,
            review_id=1,
            business_record_id=100,
        )
        second_candidate = make_review(
            PENDING_PRIMARY_REVIEW_STATUS,
            review_id=2,
            business_record_id=200,
        )

        conflict_ids = (
            get_unresolved_assignment_conflict_review_ids(
                [
                    first_candidate,
                    second_candidate,
                ]
            )
        )

        self.assertEqual(
            conflict_ids,
            frozenset({1, 2}),
        )

    def test_keeps_single_business_candidates_out_of_conflict_pool(self):
        first_candidate = make_review(
            PENDING_PRIMARY_REVIEW_STATUS,
            review_id=1,
            business_record_id=100,
        )
        second_candidate = make_review(
            "待审核",
            review_id=2,
            business_record_id=100,
        )

        conflict_ids = (
            get_unresolved_assignment_conflict_review_ids(
                [
                    first_candidate,
                    second_candidate,
                ]
            )
        )

        self.assertEqual(
            conflict_ids,
            frozenset(),
        )

    def test_keeps_different_vouchers_out_of_conflict_pool(self):
        first_candidate = make_review(
            PENDING_PRIMARY_REVIEW_STATUS,
            review_id=1,
            voucher_id=10,
            business_record_id=100,
        )
        second_candidate = make_review(
            PENDING_PRIMARY_REVIEW_STATUS,
            review_id=2,
            voucher_id=20,
            business_record_id=200,
        )

        conflict_ids = (
            get_unresolved_assignment_conflict_review_ids(
                [
                    first_candidate,
                    second_candidate,
                ]
            )
        )

        self.assertEqual(
            conflict_ids,
            frozenset(),
        )

    def test_assignment_reservation_resolves_unresolved_conflict(self):
        first_candidate = make_review(
            PENDING_PRIMARY_REVIEW_STATUS,
            review_id=1,
            business_record_id=100,
        )
        second_candidate = make_review(
            PENDING_PRIMARY_REVIEW_STATUS,
            review_id=2,
            business_record_id=200,
        )
        reserved_owner = make_review(
            PENDING_SECONDARY_REVIEW_STATUS,
            review_id=3,
            business_record_id=100,
        )

        conflict_ids = (
            get_unresolved_assignment_conflict_review_ids(
                [
                    first_candidate,
                    second_candidate,
                    reserved_owner,
                ]
            )
        )

        self.assertEqual(
            conflict_ids,
            frozenset(),
        )

    def test_hides_one_point_candidate_reserved_by_other_business(self):
        candidate = make_review(
            PENDING_PRIMARY_REVIEW_STATUS,
            review_id=1,
            business_record_id=100,
        )
        reserved_review = make_review(
            PENDING_SECONDARY_REVIEW_STATUS,
            review_id=2,
            business_record_id=200,
        )

        hidden_ids = (
            get_hidden_low_confidence_conflict_review_ids(
                [
                    candidate,
                    reserved_review,
                ]
            )
        )

        self.assertEqual(
            hidden_ids,
            frozenset({1}),
        )

    def test_hides_legacy_pending_candidate_for_approved_owner(self):
        candidate = make_review(
            "待审核",
            review_id=1,
            business_record_id=100,
        )
        approved_review = make_review(
            APPROVED_REVIEW_STATUS,
            review_id=2,
            business_record_id=200,
        )

        hidden_ids = (
            get_hidden_low_confidence_conflict_review_ids(
                [
                    candidate,
                    approved_review,
                ]
            )
        )

        self.assertEqual(
            hidden_ids,
            frozenset({1}),
        )

    def test_keeps_reserved_review_visible(self):
        candidate = make_review(
            PENDING_PRIMARY_REVIEW_STATUS,
            review_id=1,
            business_record_id=100,
        )
        reserved_review = make_review(
            PENDING_SECONDARY_REVIEW_STATUS,
            review_id=2,
            business_record_id=200,
        )

        hidden_ids = (
            get_hidden_low_confidence_conflict_review_ids(
                [
                    candidate,
                    reserved_review,
                ]
            )
        )

        self.assertNotIn(2, hidden_ids)

    def test_keeps_candidate_when_same_business_owns_voucher(self):
        candidate = make_review(
            PENDING_PRIMARY_REVIEW_STATUS,
            review_id=1,
            business_record_id=100,
        )
        reserved_review = make_review(
            PENDING_SECONDARY_REVIEW_STATUS,
            review_id=2,
            business_record_id=100,
        )

        hidden_ids = (
            get_hidden_low_confidence_conflict_review_ids(
                [
                    candidate,
                    reserved_review,
                ]
            )
        )

        self.assertEqual(
            hidden_ids,
            frozenset(),
        )

    def test_hides_one_point_candidate_when_owner_has_higher_score(self):
        low_confidence_candidate = make_review(
            PENDING_PRIMARY_REVIEW_STATUS,
            review_id=1,
            business_record_id=100,
            score=1,
        )
        reserved_owner = make_review(
            PENDING_SECONDARY_REVIEW_STATUS,
            review_id=2,
            business_record_id=200,
            score=4,
        )
        stronger_candidate = make_review(
            PENDING_PRIMARY_REVIEW_STATUS,
            review_id=3,
            business_record_id=300,
            score=4,
        )

        hidden_ids = (
            get_hidden_low_confidence_conflict_review_ids(
                [
                    low_confidence_candidate,
                    reserved_owner,
                    stronger_candidate,
                ]
            )
        )

        self.assertEqual(
            hidden_ids,
            frozenset({1}),
        )

    def test_rejected_owner_releases_hidden_candidate(self):
        candidate = make_review(
            PENDING_PRIMARY_REVIEW_STATUS,
            review_id=1,
            business_record_id=100,
        )
        rejected_review = make_review(
            REJECTED_REVIEW_STATUS,
            review_id=2,
            business_record_id=200,
        )

        hidden_ids = (
            get_hidden_low_confidence_conflict_review_ids(
                [
                    candidate,
                    rejected_review,
                ]
            )
        )

        self.assertEqual(
            hidden_ids,
            frozenset(),
        )

    def test_primary_reviewer_can_review_legacy_pending_record(self):
        user = make_admin(1, PRIMARY_REVIEWER)
        review = make_review("待审核")

        self.assertTrue(
            can_primary_review_match(user, review)
        )

    def test_primary_reviewer_can_review_new_pending_record(self):
        user = make_admin(1, PRIMARY_REVIEWER)
        review = make_review(
            PENDING_PRIMARY_REVIEW_STATUS
        )

        self.assertTrue(
            can_primary_review_match(user, review)
        )

    def test_primary_review_rejects_wrong_stage(self):
        user = make_admin(1, PRIMARY_REVIEWER)
        review = make_review(
            PENDING_SECONDARY_REVIEW_STATUS
        )

        self.assertFalse(
            can_primary_review_match(user, review)
        )

    def test_secondary_reviewer_cannot_perform_primary_review(self):
        user = make_admin(2, SECONDARY_REVIEWER)
        review = make_review(
            PENDING_PRIMARY_REVIEW_STATUS
        )

        self.assertFalse(
            can_primary_review_match(user, review)
        )

    def test_secondary_reviewer_can_review_different_primary_reviewer(self):
        user = make_admin(2, SECONDARY_REVIEWER)
        review = make_review(
            PENDING_SECONDARY_REVIEW_STATUS,
            primary_reviewer_id=1,
        )

        self.assertTrue(
            can_secondary_review_match(user, review)
        )

    def test_secondary_review_rejects_same_reviewer(self):
        user = make_admin(2, SECONDARY_REVIEWER)
        review = make_review(
            PENDING_SECONDARY_REVIEW_STATUS,
            primary_reviewer_id=2,
        )

        self.assertFalse(
            can_secondary_review_match(user, review)
        )

    def test_secondary_review_requires_primary_reviewer(self):
        user = make_admin(2, SECONDARY_REVIEWER)
        review = make_review(
            PENDING_SECONDARY_REVIEW_STATUS
        )

        self.assertFalse(
            can_secondary_review_match(user, review)
        )

    def test_primary_reviewer_cannot_perform_secondary_review(self):
        user = make_admin(1, PRIMARY_REVIEWER)
        review = make_review(
            PENDING_SECONDARY_REVIEW_STATUS,
            primary_reviewer_id=3,
        )

        self.assertFalse(
            can_secondary_review_match(user, review)
        )

    def test_super_admin_cannot_review_own_primary_decision(self):
        user = make_admin(1, SUPER_ADMIN)
        review = make_review(
            PENDING_SECONDARY_REVIEW_STATUS,
            primary_reviewer_id=1,
        )

        self.assertFalse(
            can_secondary_review_match(user, review)
        )


    def test_primary_approval_records_decision_and_moves_to_secondary(self):
        user = make_admin(1, PRIMARY_REVIEWER)
        review = make_review(
            PENDING_PRIMARY_REVIEW_STATUS
        )
        reviewed_at = datetime(
            2026,
            7,
            27,
            8,
            0,
            tzinfo=timezone.utc,
        )

        applied = apply_primary_review_decision(
            user=user,
            review=review,
            result=REVIEW_RESULT_APPROVED,
            comment="  初审资料一致  ",
            reviewed_at=reviewed_at,
        )

        self.assertTrue(applied)
        self.assertEqual(
            review.review_status,
            PENDING_SECONDARY_REVIEW_STATUS,
        )
        self.assertEqual(review.primary_reviewer_id, 1)
        self.assertEqual(
            review.primary_review_result,
            REVIEW_RESULT_APPROVED,
        )
        self.assertEqual(
            review.primary_review_comment,
            "初审资料一致",
        )
        self.assertEqual(
            review.primary_reviewed_at,
            reviewed_at,
        )

    def test_primary_decision_uses_utc8_now_by_default(self):
        user = make_admin(1, PRIMARY_REVIEWER)
        review = make_review(
            PENDING_PRIMARY_REVIEW_STATUS
        )
        expected_time = datetime(
            2026,
            7,
            29,
            16,
            0,
            0,
        )

        with patch(
            "app.match_review_workflow.utc8_now",
            return_value=expected_time,
        ):
            applied = apply_primary_review_decision(
                user=user,
                review=review,
                result=REVIEW_RESULT_APPROVED,
            )

        self.assertTrue(applied)
        self.assertEqual(
            review.primary_reviewed_at,
            expected_time,
        )
        self.assertIsNone(
            review.primary_reviewed_at.tzinfo
        )

    def test_primary_rejection_requires_comment(self):
        user = make_admin(1, PRIMARY_REVIEWER)
        review = make_review(
            PENDING_PRIMARY_REVIEW_STATUS
        )

        applied = apply_primary_review_decision(
            user=user,
            review=review,
            result=REVIEW_RESULT_REJECTED,
            comment="   ",
        )

        self.assertFalse(applied)
        self.assertEqual(
            review.review_status,
            PENDING_PRIMARY_REVIEW_STATUS,
        )

    def test_primary_rejection_records_decision(self):
        user = make_admin(1, PRIMARY_REVIEWER)
        review = make_review(
            PENDING_PRIMARY_REVIEW_STATUS
        )

        applied = apply_primary_review_decision(
            user=user,
            review=review,
            result=REVIEW_RESULT_REJECTED,
            comment="凭证金额不一致",
        )

        self.assertTrue(applied)
        self.assertEqual(
            review.review_status,
            REJECTED_REVIEW_STATUS,
        )
        self.assertEqual(
            review.primary_review_result,
            REVIEW_RESULT_REJECTED,
        )
        self.assertEqual(
            review.primary_review_comment,
            "凭证金额不一致",
        )

    def test_primary_decision_rejects_invalid_result(self):
        user = make_admin(1, PRIMARY_REVIEWER)
        review = make_review(
            PENDING_PRIMARY_REVIEW_STATUS
        )

        applied = apply_primary_review_decision(
            user=user,
            review=review,
            result="未知结果",
        )

        self.assertFalse(applied)
        self.assertEqual(
            review.review_status,
            PENDING_PRIMARY_REVIEW_STATUS,
        )

    def test_secondary_approval_requires_allocation_validation(self):
        user = make_admin(2, SECONDARY_REVIEWER)
        review = make_review(
            PENDING_SECONDARY_REVIEW_STATUS,
            primary_reviewer_id=1,
        )

        applied = apply_secondary_review_decision(
            user=user,
            review=review,
            result=REVIEW_RESULT_APPROVED,
        )

        self.assertFalse(applied)
        self.assertEqual(
            review.review_status,
            PENDING_SECONDARY_REVIEW_STATUS,
        )
        self.assertIsNone(
            getattr(
                review,
                "secondary_reviewer_id",
                None,
            )
        )

    def test_secondary_approval_records_final_decision(self):
        user = make_admin(2, SECONDARY_REVIEWER)
        review = make_review(
            PENDING_SECONDARY_REVIEW_STATUS,
            primary_reviewer_id=1,
        )
        reviewed_at = datetime(
            2026,
            7,
            27,
            9,
            0,
            tzinfo=timezone.utc,
        )

        applied = apply_secondary_review_decision(
            user=user,
            review=review,
            result=REVIEW_RESULT_APPROVED,
            comment="复核无误",
            reviewed_at=reviewed_at,
            allocation_validated=True,
        )

        self.assertTrue(applied)
        self.assertEqual(
            review.review_status,
            APPROVED_REVIEW_STATUS,
        )
        self.assertEqual(
            review.secondary_reviewer_id,
            2,
        )
        self.assertEqual(
            review.secondary_review_result,
            REVIEW_RESULT_APPROVED,
        )
        self.assertEqual(
            review.secondary_review_comment,
            "复核无误",
        )
        self.assertEqual(
            review.secondary_reviewed_at,
            reviewed_at,
        )

    def test_secondary_decision_uses_utc8_now_by_default(self):
        user = make_admin(2, SECONDARY_REVIEWER)
        review = make_review(
            PENDING_SECONDARY_REVIEW_STATUS,
            primary_reviewer_id=1,
        )
        expected_time = datetime(
            2026,
            7,
            29,
            16,
            30,
            0,
        )

        with patch(
            "app.match_review_workflow.utc8_now",
            return_value=expected_time,
        ):
            applied = apply_secondary_review_decision(
                user=user,
                review=review,
                result=REVIEW_RESULT_APPROVED,
                allocation_validated=True,
            )

        self.assertTrue(applied)
        self.assertEqual(
            review.secondary_reviewed_at,
            expected_time,
        )
        self.assertIsNone(
            review.secondary_reviewed_at.tzinfo
        )

    def test_secondary_rejection_requires_comment(self):
        user = make_admin(2, SECONDARY_REVIEWER)
        review = make_review(
            PENDING_SECONDARY_REVIEW_STATUS,
            primary_reviewer_id=1,
        )

        applied = apply_secondary_review_decision(
            user=user,
            review=review,
            result=REVIEW_RESULT_REJECTED,
            comment="",
        )

        self.assertFalse(applied)
        self.assertEqual(
            review.review_status,
            PENDING_SECONDARY_REVIEW_STATUS,
        )

    def test_secondary_rejection_records_final_decision(self):
        user = make_admin(2, SECONDARY_REVIEWER)
        review = make_review(
            PENDING_SECONDARY_REVIEW_STATUS,
            primary_reviewer_id=1,
        )

        applied = apply_secondary_review_decision(
            user=user,
            review=review,
            result=REVIEW_RESULT_REJECTED,
            comment="复核发现银行信息不符",
        )

        self.assertTrue(applied)
        self.assertEqual(
            review.review_status,
            REJECTED_REVIEW_STATUS,
        )
        self.assertEqual(
            review.secondary_review_result,
            REVIEW_RESULT_REJECTED,
        )
        self.assertEqual(
            review.secondary_review_comment,
            "复核发现银行信息不符",
        )

    def test_secondary_decision_does_not_allow_same_reviewer(self):
        user = make_admin(1, SUPER_ADMIN)
        review = make_review(
            PENDING_SECONDARY_REVIEW_STATUS,
            primary_reviewer_id=1,
        )

        applied = apply_secondary_review_decision(
            user=user,
            review=review,
            result=REVIEW_RESULT_APPROVED,
            allocation_validated=True,
        )

        self.assertFalse(applied)
        self.assertEqual(
            review.review_status,
            PENDING_SECONDARY_REVIEW_STATUS,
        )


if __name__ == "__main__":
    unittest.main()