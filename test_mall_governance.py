import unittest
from types import SimpleNamespace

from app.admin_permissions import (
    MALL_AUDIT_ACTION_LEVELS,
    OPERATOR,
    PRIMARY_REVIEWER,
    SECONDARY_REVIEWER,
    SUPER_ADMIN,
    can_adjust_mall_points,
    can_confirm_mall_supplier_settlements,
    can_manage_mall_catalog,
    can_manage_mall_inventory,
    can_manage_mall_orders,
    can_manage_mall_suppliers,
    can_perform_mall_audit_action,
)
from app.mall import (
    MallAuditActionType,
    VALID_MALL_AUDIT_ACTION_TYPES,
    normalize_mall_audit_action_type,
)


def make_user(
    role: str,
    admin_level: str | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=1,
        role=role,
        admin_level=admin_level,
    )


class MallPermissionTests(unittest.TestCase):
    def setUp(self):
        self.super_admin = make_user(
            "admin",
            SUPER_ADMIN,
        )
        self.operator = make_user(
            "admin",
            OPERATOR,
        )
        self.primary_reviewer = make_user(
            "admin",
            PRIMARY_REVIEWER,
        )
        self.secondary_reviewer = make_user(
            "admin",
            SECONDARY_REVIEWER,
        )
        self.partner = make_user("partner")

    def test_super_admin_and_operator_manage_standard_domains(
        self,
    ):
        permission_functions = (
            can_manage_mall_catalog,
            can_manage_mall_inventory,
            can_manage_mall_orders,
            can_manage_mall_suppliers,
        )

        for permission_function in permission_functions:
            with self.subTest(
                permission=permission_function.__name__,
            ):
                self.assertTrue(
                    permission_function(
                        self.super_admin
                    )
                )
                self.assertTrue(
                    permission_function(
                        self.operator
                    )
                )

    def test_reviewers_and_non_admins_cannot_manage_mall(
        self,
    ):
        users = (
            self.primary_reviewer,
            self.secondary_reviewer,
            self.partner,
            make_user("admin"),
            make_user("admin", "unknown_level"),
            None,
        )
        permission_functions = (
            can_manage_mall_catalog,
            can_manage_mall_inventory,
            can_manage_mall_orders,
            can_manage_mall_suppliers,
            can_adjust_mall_points,
            can_confirm_mall_supplier_settlements,
        )

        for user in users:
            for permission_function in permission_functions:
                with self.subTest(
                    user=user,
                    permission=permission_function.__name__,
                ):
                    self.assertFalse(
                        permission_function(user)
                    )

    def test_sensitive_operations_are_super_admin_only(
        self,
    ):
        for permission_function in (
            can_adjust_mall_points,
            can_confirm_mall_supplier_settlements,
        ):
            with self.subTest(
                permission=permission_function.__name__,
            ):
                self.assertTrue(
                    permission_function(
                        self.super_admin
                    )
                )
                self.assertFalse(
                    permission_function(
                        self.operator
                    )
                )

    def test_every_audit_action_has_explicit_permission_mapping(
        self,
    ):
        self.assertEqual(
            set(MALL_AUDIT_ACTION_LEVELS),
            set(MallAuditActionType),
        )

    def test_standard_audit_actions_allow_operator(
        self,
    ):
        sensitive_actions = {
            MallAuditActionType.POINTS_ADJUST,
            MallAuditActionType.SUPPLIER_SETTLEMENT_CONFIRM,
        }

        for action_type in MallAuditActionType:
            if action_type in sensitive_actions:
                continue

            with self.subTest(action_type=action_type):
                self.assertTrue(
                    can_perform_mall_audit_action(
                        self.super_admin,
                        action_type,
                    )
                )
                self.assertTrue(
                    can_perform_mall_audit_action(
                        self.operator,
                        action_type.value,
                    )
                )

    def test_sensitive_audit_actions_reject_operator(
        self,
    ):
        for action_type in (
            MallAuditActionType.POINTS_ADJUST,
            MallAuditActionType.SUPPLIER_SETTLEMENT_CONFIRM,
        ):
            with self.subTest(action_type=action_type):
                self.assertTrue(
                    can_perform_mall_audit_action(
                        self.super_admin,
                        action_type,
                    )
                )
                self.assertFalse(
                    can_perform_mall_audit_action(
                        self.operator,
                        action_type,
                    )
                )

    def test_audit_actions_reject_non_operational_roles(
        self,
    ):
        users = (
            self.primary_reviewer,
            self.secondary_reviewer,
            self.partner,
            make_user("admin"),
            make_user("admin", "unknown_level"),
            None,
        )

        for user in users:
            for action_type in MallAuditActionType:
                with self.subTest(
                    user=user,
                    action_type=action_type,
                ):
                    self.assertFalse(
                        can_perform_mall_audit_action(
                            user,
                            action_type,
                        )
                    )

    def test_unknown_audit_actions_fail_closed(self):
        for action_type in (
            "mall_unknown_action",
            "MALL_POINTS_ADJUST",
            "",
            None,
        ):
            with self.subTest(action_type=action_type):
                self.assertFalse(
                    can_perform_mall_audit_action(
                        self.super_admin,
                        action_type,
                    )
                )


class MallAuditActionTypeTests(unittest.TestCase):
    def test_valid_values_match_enum(self):
        self.assertEqual(
            VALID_MALL_AUDIT_ACTION_TYPES,
            frozenset(
                action.value
                for action in MallAuditActionType
            ),
        )
        self.assertEqual(
            len(VALID_MALL_AUDIT_ACTION_TYPES),
            len(MallAuditActionType),
        )

    def test_normalize_accepts_enum_and_trimmed_value(self):
        self.assertIs(
            normalize_mall_audit_action_type(
                MallAuditActionType.ORDER_SHIP
            ),
            MallAuditActionType.ORDER_SHIP,
        )
        self.assertIs(
            normalize_mall_audit_action_type(
                "  mall_order_ship  "
            ),
            MallAuditActionType.ORDER_SHIP,
        )

    def test_normalize_rejects_unknown_or_non_string_values(self):
        for value in (
            "mall_order_unknown",
            "MALL_ORDER_SHIP",
            "",
            None,
            1,
        ):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    normalize_mall_audit_action_type(
                        value
                    )


if __name__ == "__main__":
    unittest.main()
