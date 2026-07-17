import unittest
from decimal import Decimal

from app.settlement_calculator import (
    EXTERNAL_MODE,
    INTERNAL_MODE,
    calculate_business_settlement,
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
        result = calculate_business_settlement(
            "10000",
            "5",
            INTERNAL_MODE,
            "3",
            EXTERNAL_MODE,
        )

        self.assertEqual(
            result.downstream.settlement_total,
            Decimal("10526.32"),
        )

        self.assertEqual(
            result.downstream.fee_amount,
            Decimal("526.32"),
        )

        self.assertEqual(
            result.upstream.settlement_total,
            Decimal("10300.00"),
        )

        self.assertEqual(
            result.upstream.fee_amount,
            Decimal("300.00"),
        )

        self.assertEqual(
            result.gross_profit,
            Decimal("226.32"),
        )


    def test_business_profit_matches_rounded_fees(
        self,
    ):
        # 下游：
        # 1.00 × 0.5% = 0.005
        # ROUND_HALF_UP 后服务费为 0.01。
        #
        # 上游：
        # 1.00 × 0.4% = 0.004
        # ROUND_HALF_UP 后成本费为 0.00。
        result = calculate_business_settlement(
            "1.00",
            "0.5",
            EXTERNAL_MODE,
            "0.4",
            EXTERNAL_MODE,
        )

        self.assertEqual(
            result.downstream.fee_amount,
            Decimal("0.01"),
        )

        self.assertEqual(
            result.upstream.fee_amount,
            Decimal("0.00"),
        )

        self.assertEqual(
            result.gross_profit,
            Decimal("0.01"),
        )

        self.assertEqual(
            result.gross_profit,
            (
                result.downstream.fee_amount
                - result.upstream.fee_amount
            ),
        )


    def test_aggregate_totals_equal_row_sums(
        self,
    ):
        first_result = calculate_business_settlement(
            "10000",
            "5",
            EXTERNAL_MODE,
            "3",
            EXTERNAL_MODE,
        )

        second_result = calculate_business_settlement(
            "1.00",
            "0.5",
            EXTERNAL_MODE,
            "0.4",
            EXTERNAL_MODE,
        )

        results = [
            first_result,
            second_result,
        ]

        total_receivable_fee = sum(
            (
                result.downstream.fee_amount
                for result in results
            ),
            Decimal("0.00"),
        )

        total_payable_cost = sum(
            (
                result.upstream.fee_amount
                for result in results
            ),
            Decimal("0.00"),
        )

        total_gross_profit = sum(
            (
                result.gross_profit
                for result in results
            ),
            Decimal("0.00"),
        )

        self.assertEqual(
            total_receivable_fee,
            Decimal("500.01"),
        )

        self.assertEqual(
            total_payable_cost,
            Decimal("300.00"),
        )

        self.assertEqual(
            total_gross_profit,
            Decimal("200.01"),
        )

        self.assertEqual(
            total_gross_profit,
            (
                total_receivable_fee
                - total_payable_cost
            ),
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