import unittest
from types import SimpleNamespace

from app.admin_permissions import (
    PRIMARY_REVIEWER,
    SECONDARY_REVIEWER,
    SUPER_ADMIN,
)
from app.match_review_workflow import (
    PENDING_PRIMARY_REVIEW_STATUS,
    PENDING_SECONDARY_REVIEW_STATUS,
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


if __name__ == "__main__":
    unittest.main()