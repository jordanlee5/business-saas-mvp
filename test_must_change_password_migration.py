import sqlite3
import unittest

from add_must_change_password import (
    COLUMN_NAME,
    get_column_names,
    migrate,
)


class MustChangePasswordMigrationTests(
    unittest.TestCase
):
    def setUp(self):
        self.connection = sqlite3.connect(
            ":memory:"
        )
        self.connection.executescript(
            """
            CREATE TABLE users (
                id INTEGER PRIMARY KEY,
                username TEXT NOT NULL
            );

            INSERT INTO users (
                id,
                username
            )
            VALUES
                (1, 'admin'),
                (2, 'partner_a');
            """
        )

    def tearDown(self):
        self.connection.close()

    def test_adds_column_without_forcing_historical_users(
        self,
    ):
        (
            column_added,
            normalized_count,
        ) = migrate(self.connection)

        self.assertTrue(column_added)
        self.assertEqual(
            normalized_count,
            0,
        )
        self.assertIn(
            COLUMN_NAME,
            get_column_names(
                self.connection
            ),
        )

        rows = self.connection.execute(
            """
            SELECT
                id,
                must_change_password
            FROM users
            ORDER BY id
            """
        ).fetchall()

        self.assertEqual(
            rows,
            [
                (1, 0),
                (2, 0),
            ],
        )

    def test_repeat_migration_preserves_existing_status(
        self,
    ):
        migrate(self.connection)

        self.connection.execute(
            """
            UPDATE users
            SET must_change_password = 1
            WHERE id = 1
            """
        )

        (
            column_added,
            normalized_count,
        ) = migrate(self.connection)

        rows = self.connection.execute(
            """
            SELECT
                id,
                must_change_password
            FROM users
            ORDER BY id
            """
        ).fetchall()

        self.assertFalse(column_added)
        self.assertEqual(
            normalized_count,
            0,
        )
        self.assertEqual(
            rows,
            [
                (1, 1),
                (2, 0),
            ],
        )


if __name__ == "__main__":
    unittest.main()