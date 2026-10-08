"""现有订单状态机的允许流转、拒绝和重放约束。"""

import unittest

from app.mall.order_state_machine import (
    OrderAction,
    OrderStatus,
    resolve_order_transition,
)


class OrderStateMachineTests(unittest.TestCase):
    def test_every_action_accepts_only_source_and_exact_target_replay(self):
        transitions = {
            OrderAction.CANCEL: (OrderStatus.CREATED, OrderStatus.CANCELLED),
            OrderAction.FULFILL: (OrderStatus.CREATED, OrderStatus.FULFILLING),
            OrderAction.SHIP: (OrderStatus.FULFILLING, OrderStatus.SHIPPED),
            OrderAction.COMPLETE: (OrderStatus.SHIPPED, OrderStatus.COMPLETED),
            OrderAction.REFUND: (OrderStatus.COMPLETED, OrderStatus.REFUNDED),
        }
        for action, (source, target) in transitions.items():
            for status in OrderStatus:
                with self.subTest(action=action.value, status=status.value):
                    if status in (source, target):
                        result = resolve_order_transition(status.value, action)
                        self.assertEqual(result.status, target.value)
                        self.assertEqual(result.replayed, status == target)
                    else:
                        with self.assertRaisesRegex(ValueError, "当前订单状态不允许"):
                            resolve_order_transition(status.value, action)

    def test_unknown_status_or_action_is_rejected(self):
        for invalid_status in ("RESERVED", "PAID", "RECONCILED", "SETTLED", "UNKNOWN", None):
            with self.subTest(status=invalid_status):
                with self.assertRaises(ValueError):
                    resolve_order_transition(invalid_status, OrderAction.FULFILL)
        with self.assertRaisesRegex(ValueError, "订单操作无效"):
            resolve_order_transition("CREATED", "FULFILL")

    def test_cancelled_and_refunded_are_terminal_except_exact_replay(self):
        for status, replay_action in (
            (OrderStatus.CANCELLED, OrderAction.CANCEL),
            (OrderStatus.REFUNDED, OrderAction.REFUND),
        ):
            for action in OrderAction:
                with self.subTest(status=status.value, action=action.value):
                    if action == replay_action:
                        self.assertTrue(resolve_order_transition(status.value, action).replayed)
                    else:
                        with self.assertRaises(ValueError):
                            resolve_order_transition(status.value, action)


if __name__ == "__main__":
    unittest.main()
