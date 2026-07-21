import unittest
from types import SimpleNamespace

from app.admin_permissions import (
    OPERATOR,
    PRIMARY_REVIEWER,
    SECONDARY_REVIEWER,
    SUPER_ADMIN,
    can_manage_administrators,
    can_operate,
    can_primary_review,
    can_secondary_review,
    get_admin_level,
    is_admin_user,
    is_super_admin,
)


def make_user(
    role: str,
    admin_level: str | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        role=role,
        admin_level=admin_level,
    )


class AdminPermissionsTests(unittest.TestCase):
    def test_super_admin_has_all_permissions(self):
        user = make_user(
            role="admin",
            admin_level=SUPER_ADMIN,
        )

        self.assertTrue(is_admin_user(user))
        self.assertTrue(is_super_admin(user))
        self.assertTrue(can_manage_administrators(user))
        self.assertTrue(can_primary_review(user))
        self.assertTrue(can_secondary_review(user))
        self.assertTrue(can_operate(user))

    def test_primary_reviewer_only_has_primary_review_permission(self):
        user = make_user(
            role="admin",
            admin_level=PRIMARY_REVIEWER,
        )

        self.assertTrue(is_admin_user(user))
        self.assertFalse(is_super_admin(user))
        self.assertFalse(can_manage_administrators(user))
        self.assertTrue(can_primary_review(user))
        self.assertFalse(can_secondary_review(user))
        self.assertFalse(can_operate(user))

    def test_secondary_reviewer_only_has_secondary_review_permission(self):
        user = make_user(
            role="admin",
            admin_level=SECONDARY_REVIEWER,
        )

        self.assertTrue(is_admin_user(user))
        self.assertFalse(is_super_admin(user))
        self.assertFalse(can_manage_administrators(user))
        self.assertFalse(can_primary_review(user))
        self.assertTrue(can_secondary_review(user))
        self.assertFalse(can_operate(user))

    def test_operator_only_has_operation_permission(self):
        user = make_user(
            role="admin",
            admin_level=OPERATOR,
        )

        self.assertTrue(is_admin_user(user))
        self.assertFalse(is_super_admin(user))
        self.assertFalse(can_manage_administrators(user))
        self.assertFalse(can_primary_review(user))
        self.assertFalse(can_secondary_review(user))
        self.assertTrue(can_operate(user))

    def test_partner_has_no_admin_permissions(self):
        user = make_user(
            role="partner",
            admin_level=None,
        )

        self.assertIsNone(get_admin_level(user))
        self.assertFalse(is_admin_user(user))
        self.assertFalse(is_super_admin(user))
        self.assertFalse(can_manage_administrators(user))
        self.assertFalse(can_primary_review(user))
        self.assertFalse(can_secondary_review(user))
        self.assertFalse(can_operate(user))

    def test_admin_without_level_is_rejected(self):
        user = make_user(
            role="admin",
            admin_level=None,
        )

        self.assertIsNone(get_admin_level(user))
        self.assertFalse(is_admin_user(user))

    def test_invalid_admin_level_is_rejected(self):
        user = make_user(
            role="admin",
            admin_level="unknown_level",
        )

        self.assertIsNone(get_admin_level(user))
        self.assertFalse(is_admin_user(user))

    def test_none_user_has_no_permissions(self):
        self.assertIsNone(get_admin_level(None))
        self.assertFalse(is_admin_user(None))
        self.assertFalse(is_super_admin(None))
        self.assertFalse(can_manage_administrators(None))
        self.assertFalse(can_primary_review(None))
        self.assertFalse(can_secondary_review(None))
        self.assertFalse(can_operate(None))


if __name__ == "__main__":
    unittest.main()
