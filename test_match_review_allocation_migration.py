import sqlite3
import unittest

from add_match_review_allocation_amount import (
    ALLOCATION_COLUMN,
    build_summary,
    get_column_names,
    migrate,
)


class MatchReviewAllocationMigrationTests(
    unittest.TestCase
):
    def setUp(self):
        self.connection = sqlite3.connect(
            ":memory:"
        )
        self.connection.executescript(
            """
            CREATE TABLE voucher_records (
                id INTEGER PRIMARY KEY,
                voucher_amount REAL
            );

            CREATE TABLE business_records (
                id INTEGER PRIMARY KEY,
                points_amount REAL
            );

            CREATE TABLE match_reviews (
                id INTEGER PRIMARY KEY,
                voucher_id INTEGER,
                business_record_id INTEGER,
                review_status TEXT
            );
            """
        )
        self.connection.executemany(
            """
            INSERT INTO voucher_records (
                id,
                voucher_amount
            )
            VALUES (?, ?)
            """,
            (
                (1, 300.126),
                (2, 200.0),
                (3, 0.0),
            ),
        )
        self.connection.executemany(
            """
            INSERT INTO business_records (
                id,
                points_amount
            )
            VALUES (?, ?)
            """,
            (
                (1, 500.0),
                (2, 200.0),
                (3, 100.0),
            ),
        )
        self.connection.executemany(
            """
            INSERT INTO match_reviews (
                id,
                voucher_id,
                business_record_id,
                review_status
            )
            VALUES (?, ?, ?, ?)
            """,
            (
                (1, 1, 1, "已通过"),
                (2, 2, 2, "待复核"),
                (3, 1, 1, "待审核"),
                (4, 1, 1, "已驳回"),
                (5, 3, 3, "已通过"),
            ),
        )
        self.connection.commit()

    def tearDown(self):
        self.connection.close()

    def test_adds_column_and_backfills_only_eligible_rows(
        self,
    ):
        (
            column_added,
            backfilled_rows,
            summary,
        ) = migrate(self.connection)

        self.assertTrue(column_added)
        self.assertEqual(
            backfilled_rows,
            2,
        )
        self.assertIn(
            ALLOCATION_COLUMN,
            get_column_names(
                self.connection,
                "match_reviews",
            ),
        )

        rows = self.connection.execute(
            """
            SELECT
                id,
                allocation_amount
            FROM match_reviews
            ORDER BY id
            """
        ).fetchall()

        self.assertEqual(
            rows,
            [
                (1, 300.13),
                (2, 200),
                (3, None),
                (4, None),
                (5, None),
            ],
        )
        self.assertEqual(
            summary["approved_total"],
            2,
        )
        self.assertEqual(
            summary[
                "approved_missing_allocation"
            ],
            1,
        )

    def test_migration_is_repeatable_and_preserves_values(
        self,
    ):
        migrate(self.connection)
        self.connection.execute(
            """
            UPDATE match_reviews
            SET allocation_amount = 123.45
            WHERE id = 1
            """
        )

        (
            column_added,
            backfilled_rows,
            _,
        ) = migrate(self.connection)

        value = self.connection.execute(
            """
            SELECT allocation_amount
            FROM match_reviews
            WHERE id = 1
            """
        ).fetchone()[0]

        self.assertFalse(column_added)
        self.assertEqual(
            backfilled_rows,
            0,
        )
        self.assertEqual(
            value,
            123.45,
        )

    def test_summary_reports_historical_overallocation(
        self,
    ):
        migrate(self.connection)
        self.connection.execute(
            """
            INSERT INTO match_reviews (
                id,
                voucher_id,
                business_record_id,
                review_status,
                allocation_amount
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                6,
                1,
                3,
                "已通过",
                300.13,
            ),
        )

        summary = build_summary(
            self.connection
        )

        self.assertEqual(
            summary["business_overpayments"],
            1,
        )
        self.assertEqual(
            summary["voucher_overallocations"],
            1,
        )


if __name__ == "__main__":
    unittest.main()