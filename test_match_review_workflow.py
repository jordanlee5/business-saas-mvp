import unittest
from types import SimpleNamespace
from datetime import datetime, timezone

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
    can_primary_review_match,
    can_secondary_review_match,
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
) -> SimpleNamespace:
    return SimpleNamespace(
        id=100,
        review_status=review_status,
        primary_reviewer_id=primary_reviewer_id,
    )


class MatchReviewWorkflowTests(unittest.TestCase):
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
        )

        self.assertFalse(applied)
        self.assertEqual(
            review.review_status,
            PENDING_SECONDARY_REVIEW_STATUS,
        )


if __name__ == "__main__":
    unittest.main()