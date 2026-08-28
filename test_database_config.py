import os
import unittest
from unittest.mock import patch

from sqlalchemy import text

from app.database import (
    DEFAULT_DATABASE_URL,
    create_database_engine,
    get_engine_kwargs,
    resolve_database_url,
)


class DatabaseConfigTests(unittest.TestCase):
    def test_missing_database_url_uses_existing_sqlite_default(self):
        self.assertEqual(
            resolve_database_url({}),
            DEFAULT_DATABASE_URL,
        )
        self.assertEqual(
            DEFAULT_DATABASE_URL,
            "sqlite:///./saas_mvp.db",
        )

    def test_blank_database_url_uses_existing_sqlite_default(self):
        self.assertEqual(
            resolve_database_url({"DATABASE_URL": "   "}),
            DEFAULT_DATABASE_URL,
        )

    def test_configured_database_url_is_trimmed(self):
        configured_url = "sqlite:///./configured.db"

        self.assertEqual(
            resolve_database_url(
                {"DATABASE_URL": f"  {configured_url}  "}
            ),
            configured_url,
        )

    def test_database_url_is_read_from_process_environment(self):
        configured_url = "sqlite:///./environment.db"

        with patch.dict(
            os.environ,
            {"DATABASE_URL": configured_url},
        ):
            self.assertEqual(
                resolve_database_url(),
                configured_url,
            )

    def test_sqlite_engine_keeps_thread_compatibility_option(self):
        self.assertEqual(
            get_engine_kwargs("sqlite:///./local.db"),
            {
                "connect_args": {
                    "check_same_thread": False,
                }
            },
        )

    def test_in_memory_sqlite_uses_same_engine_option(self):
        self.assertEqual(
            get_engine_kwargs("sqlite://"),
            {
                "connect_args": {
                    "check_same_thread": False,
                }
            },
        )

    def test_postgresql_does_not_receive_sqlite_engine_options(self):
        self.assertEqual(
            get_engine_kwargs(
                "postgresql+psycopg://user:secret@db/app"
            ),
            {},
        )

    def test_configured_sqlite_engine_can_execute_query(self):
        configured_engine = create_database_engine("sqlite://")
        try:
            with configured_engine.connect() as connection:
                self.assertEqual(
                    connection.scalar(text("SELECT 1")),
                    1,
                )
        finally:
            configured_engine.dispose()

    def test_blank_explicit_engine_url_uses_existing_default(self):
        configured_engine = create_database_engine("   ")
        try:
            self.assertEqual(
                configured_engine.url.render_as_string(
                    hide_password=True
                ),
                DEFAULT_DATABASE_URL,
            )
        finally:
            configured_engine.dispose()


if __name__ == "__main__":
    unittest.main()
