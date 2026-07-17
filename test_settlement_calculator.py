import unittest
from decimal import Decimal

from app.settlement_calculator import (
    EXTERNAL_MODE,
    INTERNAL_MODE,
    calculate_rate_settlement,
)


class SettlementCalculatorTests(
    unittest.TestCase
):
    def test_external_rate_calculation(self):
        result = calculate_rate_settlement(
            "10000",
            "5",
            EXTERNAL_MODE,
        )

        self.assertEqual(
            result.base_amount,
            Decimal("10000.00"),
        )
        self.assertEqual(
            result.settlement_total,
            Decimal("10500.00"),
        )
        self.assertEqual(
            result.fee_amount,
            Decimal("500.00"),
        )

    def test_internal_rate_calculation(self):
        result = calculate_rate_settlement(
            "10000",
            "5",
            INTERNAL_MODE,
        )

        self.assertEqual(
            result.settlement_total,
            Decimal("10526.32"),
        )
        self.assertEqual(
            result.fee_amount,
            Decimal("526.32"),
        )

    def test_zero_external_rate(self):
        result = calculate_rate_settlement(
            "10000",
            "0",
            EXTERNAL_MODE,
        )

        self.assertEqual(
            result.settlement_total,
            Decimal("10000.00"),
        )
        self.assertEqual(
            result.fee_amount,
            Decimal("0.00"),
        )

    def test_zero_internal_rate(self):
        result = calculate_rate_settlement(
            "10000",
            "0",
            INTERNAL_MODE,
        )

        self.assertEqual(
            result.settlement_total,
            Decimal("10000.00"),
        )
        self.assertEqual(
            result.fee_amount,
            Decimal("0.00"),
        )

    def test_round_half_up(self):
        # 1.00 × (1 + 0.5%) = 1.005
        # ROUND_HALF_UP 后应当是 1.01。
        result = calculate_rate_settlement(
            "1.00",
            "0.5",
            EXTERNAL_MODE,
        )

        self.assertEqual(
            result.settlement_total,
            Decimal("1.01"),
        )
        self.assertEqual(
            result.fee_amount,
            Decimal("0.01"),
        )

    def test_downstream_and_upstream_modes_are_independent(
        self,
    ):
        downstream = calculate_rate_settlement(
            "10000",
            "5",
            INTERNAL_MODE,
        )

        upstream = calculate_rate_settlement(
            "10000",
            "3",
            EXTERNAL_MODE,
        )

        gross_profit = (
            downstream.settlement_total
            - upstream.settlement_total
        )

        self.assertEqual(
            downstream.settlement_total,
            Decimal("10526.32"),
        )
        self.assertEqual(
            upstream.settlement_total,
            Decimal("10300.00"),
        )
        self.assertEqual(
            gross_profit,
            Decimal("226.32"),
        )

    def test_invalid_mode_is_rejected(self):
        with self.assertRaises(ValueError):
            calculate_rate_settlement(
                "10000",
                "5",
                "unknown",
            )

    def test_negative_amount_is_rejected(self):
        with self.assertRaises(ValueError):
            calculate_rate_settlement(
                "-1",
                "5",
                EXTERNAL_MODE,
            )

    def test_negative_rate_is_rejected(self):
        with self.assertRaises(ValueError):
            calculate_rate_settlement(
                "10000",
                "-1",
                EXTERNAL_MODE,
            )

    def test_external_rate_above_100_is_rejected(
        self,
    ):
        with self.assertRaises(ValueError):
            calculate_rate_settlement(
                "10000",
                "100.01",
                EXTERNAL_MODE,
            )

    def test_internal_rate_at_100_is_rejected(
        self,
    ):
        with self.assertRaises(ValueError):
            calculate_rate_settlement(
                "10000",
                "100",
                INTERNAL_MODE,
            )

    def test_non_numeric_value_is_rejected(self):
        with self.assertRaises(ValueError):
            calculate_rate_settlement(
                "不是金额",
                "5",
                EXTERNAL_MODE,
            )


if __name__ == "__main__":
    unittest.main()