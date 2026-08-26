import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace

from app.business_status_service import (
    BUSINESS_STATUS_MATCHED_UNSETTLED,
    BUSINESS_STATUS_SETTLED,
    BUSINESS_STATUS_UNMATCHED,
)
from app.stats_visualization_service import (
    build_business_status_distribution,
    build_business_trend,
    format_amount,
)


def make_record(
    *,
    created_at,
    points_amount,
):
    return SimpleNamespace(
        created_at=created_at,
        points_amount=points_amount,
    )


class StatsVisualizationServiceTests(
    unittest.TestCase
):
    def test_format_amount_uses_thousands_separator_and_two_decimals(
        self,
    ):
        self.assertEqual(
            format_amount(5548673.98),
            "5,548,673.98",
        )
        self.assertEqual(
            format_amount("324443.1"),
            "324,443.10",
        )
        self.assertEqual(
            format_amount(0),
            "0.00",
        )
        self.assertEqual(
            format_amount("1.005"),
            "1.01",
        )

    def test_format_amount_marks_invalid_values(
        self,
    ):
        for value in (
            None,
            True,
            "invalid",
            "NaN",
            "Infinity",
        ):
            with self.subTest(value=value):
                self.assertEqual(
                    format_amount(value),
                    "—",
                )

    def test_trend_groups_records_by_date(
        self,
    ):
        trend = build_business_trend(
            [
                make_record(
                    created_at=datetime(
                        2026,
                        8,
                        1,
                        9,
                    ),
                    points_amount="10.005",
                ),
                make_record(
                    created_at=datetime(
                        2026,
                        8,
                        1,
                        18,
                    ),
                    points_amount="20.00",
                ),
                make_record(
                    created_at=datetime(
                        2026,
                        8,
                        2,
                        8,
                    ),
                    points_amount="7.50",
                ),
            ]
        )

        self.assertEqual(
            trend.total_date_count,
            2,
        )
        self.assertEqual(
            trend.hidden_date_count,
            0,
        )
        self.assertEqual(
            [
                point.full_date_label
                for point in trend.points
            ],
            [
                "2026-08-01",
                "2026-08-02",
            ],
        )
        self.assertEqual(
            trend.points[0].record_count,
            2,
        )
        self.assertEqual(
            trend.points[0].points_amount,
            30.01,
        )

    def test_trend_height_uses_visible_maximum(
        self,
    ):
        trend = build_business_trend(
            [
                make_record(
                    created_at=datetime(
                        2026,
                        8,
                        1,
                    ),
                    points_amount=1,
                ),
                make_record(
                    created_at=datetime(
                        2026,
                        8,
                        2,
                    ),
                    points_amount=1,
                ),
                make_record(
                    created_at=datetime(
                        2026,
                        8,
                        2,
                        1,
                    ),
                    points_amount=1,
                ),
            ]
        )

        self.assertEqual(
            trend.points[0].height_percent,
            50,
        )
        self.assertEqual(
            trend.points[1].height_percent,
            100,
        )

    def test_trend_keeps_latest_active_dates(
        self,
    ):
        first_day = datetime(
            2026,
            7,
            1,
        )
        records = [
            make_record(
                created_at=(
                    first_day
                    + timedelta(days=index)
                ),
                points_amount=index,
            )
            for index in range(16)
        ]

        trend = build_business_trend(
            records,
            point_limit=14,
        )

        self.assertEqual(
            trend.total_date_count,
            16,
        )
        self.assertEqual(
            trend.hidden_date_count,
            2,
        )
        self.assertEqual(
            trend.points[0].full_date_label,
            "2026-07-03",
        )
        self.assertEqual(
            trend.points[-1].full_date_label,
            "2026-07-16",
        )

    def test_trend_ignores_missing_date_and_invalid_amount(
        self,
    ):
        trend = build_business_trend(
            [
                make_record(
                    created_at=None,
                    points_amount=100,
                ),
                make_record(
                    created_at=datetime(
                        2026,
                        8,
                        3,
                    ),
                    points_amount="invalid",
                ),
            ]
        )

        self.assertEqual(
            len(trend.points),
            1,
        )
        self.assertEqual(
            trend.points[0].points_amount,
            0.0,
        )

    def test_trend_rejects_invalid_point_limit(
        self,
    ):
        with self.assertRaises(ValueError):
            build_business_trend(
                [],
                point_limit=0,
            )

    def test_distribution_uses_mutually_exclusive_business_counts(
        self,
    ):
        stages = build_business_status_distribution(
            total_business_records=65,
            matched_business_count=52,
            settled_business_count=15,
        )

        self.assertEqual(
            [stage.value for stage in stages],
            [13, 37, 15],
        )
        self.assertEqual(
            [stage.label for stage in stages],
            [
                BUSINESS_STATUS_UNMATCHED,
                BUSINESS_STATUS_MATCHED_UNSETTLED,
                BUSINESS_STATUS_SETTLED,
            ],
        )
        self.assertEqual(
            [stage.percentage for stage in stages],
            [20.0, 56.92, 23.08],
        )
        self.assertEqual(
            sum(stage.value for stage in stages),
            65,
        )

    def test_distribution_handles_empty_dataset(
        self,
    ):
        stages = build_business_status_distribution(
            total_business_records=0,
            matched_business_count=0,
            settled_business_count=0,
        )

        self.assertTrue(
            all(
                stage.percentage is None
                for stage in stages
            )
        )
        self.assertTrue(
            all(
                stage.width_percent == 0.0
                for stage in stages
            )
        )

    def test_distribution_normalizes_inconsistent_counts(
        self,
    ):
        stages = build_business_status_distribution(
            total_business_records=10,
            matched_business_count=12,
            settled_business_count=11,
        )

        self.assertEqual(
            [stage.value for stage in stages],
            [0, 0, 10],
        )
        self.assertEqual(
            sum(stage.value for stage in stages),
            10,
        )
        self.assertEqual(
            [stage.percentage for stage in stages],
            [0.0, 0.0, 100.0],
        )


if __name__ == "__main__":
    unittest.main()
