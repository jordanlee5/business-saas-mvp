import unittest
from datetime import datetime, timezone

from app.time_utils import (
    UTC8_TIMEZONE,
    format_utc8,
    utc8_now,
)


class TimeUtilsTests(unittest.TestCase):
    def test_utc8_now_returns_current_utc8_wall_time(self):
        before = datetime.now(
            UTC8_TIMEZONE
        ).replace(tzinfo=None)

        value = utc8_now()

        after = datetime.now(
            UTC8_TIMEZONE
        ).replace(tzinfo=None)

        self.assertIsNone(value.tzinfo)
        self.assertLessEqual(before, value)
        self.assertLessEqual(value, after)

    def test_format_utc8_converts_aware_utc_time(self):
        value = datetime(
            2026,
            7,
            29,
            0,
            0,
            0,
            tzinfo=timezone.utc,
        )

        self.assertEqual(
            format_utc8(value),
            "2026-07-29 08:00:00",
        )

    def test_format_utc8_keeps_naive_database_time(self):
        value = datetime(
            2026,
            7,
            29,
            8,
            0,
            0,
        )

        self.assertEqual(
            format_utc8(value),
            "2026-07-29 08:00:00",
        )

    def test_format_utc8_handles_none(self):
        self.assertEqual(
            format_utc8(None),
            "-",
        )


if __name__ == "__main__":
    unittest.main()