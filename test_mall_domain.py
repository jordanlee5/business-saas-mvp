import unittest
from datetime import datetime, timezone
from decimal import Decimal

from app.mall import (
    BusinessChannel,
    VALID_BUSINESS_CHANNELS,
    calculate_points_expiry,
    is_activation_within_deadline,
    normalize_business_channel,
    normalize_points,
)


class MallDomainTests(unittest.TestCase):
    def test_business_channels_are_fixed(self):
        self.assertEqual(
            VALID_BUSINESS_CHANNELS,
            {
                "CASH_REBATE",
                "MALL_REDEMPTION",
            },
        )

    def test_normalizes_business_channel(self):
        self.assertIs(
            normalize_business_channel(
                "CASH_REBATE"
            ),
            BusinessChannel.CASH_REBATE,
        )
        self.assertIs(
            normalize_business_channel(
                BusinessChannel.MALL_REDEMPTION
            ),
            BusinessChannel.MALL_REDEMPTION,
        )

    def test_rejects_unknown_business_channel(self):
        for invalid_value in (
            "cash_rebate",
            "UNKNOWN",
            "",
            None,
        ):
            with self.subTest(
                invalid_value=invalid_value
            ):
                with self.assertRaises(ValueError):
                    normalize_business_channel(
                        invalid_value
                    )

    def test_points_use_two_decimal_half_up_precision(self):
        self.assertEqual(
            normalize_points("1.005"),
            Decimal("1.01"),
        )
        self.assertEqual(
            normalize_points("1.004"),
            Decimal("1.00"),
        )

    def test_points_preserve_signed_ledger_values(self):
        self.assertEqual(
            normalize_points("-12.345"),
            Decimal("-12.35"),
        )

    def test_points_reject_boolean_and_invalid_values(self):
        for invalid_value in (
            True,
            "not-a-number",
            "NaN",
            "Infinity",
            None,
        ):
            with self.subTest(
                invalid_value=invalid_value
            ):
                with self.assertRaises(ValueError):
                    normalize_points(
                        invalid_value
                    )

    def test_expiry_is_same_time_next_calendar_year(self):
        activated_at = datetime(
            2026,
            8,
            27,
            16,
            30,
            45,
        )

        self.assertEqual(
            calculate_points_expiry(
                activated_at
            ),
            datetime(
                2027,
                8,
                27,
                16,
                30,
                45,
            ),
        )

    def test_leap_day_expires_on_next_february_last_day(self):
        activated_at = datetime(
            2028,
            2,
            29,
            9,
            15,
            0,
        )

        self.assertEqual(
            calculate_points_expiry(
                activated_at
            ),
            datetime(
                2029,
                2,
                28,
                9,
                15,
                0,
            ),
        )

    def test_expiry_converts_aware_time_to_utc8(self):
        activated_at = datetime(
            2026,
            8,
            27,
            8,
            30,
            45,
            tzinfo=timezone.utc,
        )

        self.assertEqual(
            calculate_points_expiry(
                activated_at
            ),
            datetime(
                2027,
                8,
                27,
                16,
                30,
                45,
            ),
        )

    def test_activation_at_deadline_is_allowed(self):
        claim_deadline = datetime(
            2026,
            12,
            31,
            23,
            59,
            59,
        )

        self.assertTrue(
            is_activation_within_deadline(
                claim_deadline,
                claim_deadline,
            )
        )

    def test_activation_after_deadline_is_rejected(self):
        claim_deadline = datetime(
            2026,
            12,
            31,
            23,
            59,
            59,
        )
        activation_at = datetime(
            2027,
            1,
            1,
            0,
            0,
            0,
        )

        self.assertFalse(
            is_activation_within_deadline(
                activation_at,
                claim_deadline,
            )
        )

    def test_activated_points_keep_full_year_after_claim_deadline(self):
        activation_at = datetime(
            2026,
            8,
            30,
            10,
            0,
            0,
        )
        claim_deadline = datetime(
            2026,
            8,
            31,
            23,
            59,
            59,
        )

        self.assertTrue(
            is_activation_within_deadline(
                activation_at,
                claim_deadline,
            )
        )
        self.assertEqual(
            calculate_points_expiry(
                activation_at
            ),
            datetime(
                2027,
                8,
                30,
                10,
                0,
                0,
            ),
        )

    def test_activation_time_comparison_uses_utc8(self):
        activation_at = datetime(
            2026,
            8,
            27,
            8,
            0,
            tzinfo=timezone.utc,
        )
        claim_deadline = datetime(
            2026,
            8,
            27,
            16,
            0,
        )

        self.assertTrue(
            is_activation_within_deadline(
                activation_at,
                claim_deadline,
            )
        )

    def test_datetime_rules_reject_invalid_values(self):
        with self.assertRaises(ValueError):
            calculate_points_expiry(None)

        with self.assertRaises(ValueError):
            is_activation_within_deadline(
                datetime(2026, 8, 27),
                None,
            )


if __name__ == "__main__":
    unittest.main()
