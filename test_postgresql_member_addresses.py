"""The same 15 HTTP/isolation/concurrency cases on migrated PostgreSQL tables.

Refuses any database containing application tables before each test. Never uses
the application's database or a merely named-but-nonempty test database.
"""
import os
import unittest

from alembic import command
from sqlalchemy import text

from app.database import create_database_engine, resolve_database_url
import test_member_addresses as cases
from test_postgresql_migration import (
    POSTGRES_TEST_DATABASE_URL_ENV_NAME, POSTGRES_TEST_ALLOW_RESET_ENV_NAME,
    validate_postgresql_test_database_url, get_database_state, build_alembic_config,
)


class PostgreSQLMemberAddressTests(cases.MemberAddressTests):
    @classmethod
    def setUpClass(cls):
        configured = os.environ.get(POSTGRES_TEST_DATABASE_URL_ENV_NAME, "").strip()
        if not configured:
            raise unittest.SkipTest("未配置独立 PostgreSQL 测试数据库")
        if os.environ.get(POSTGRES_TEST_ALLOW_RESET_ENV_NAME) != "1":
            raise RuntimeError("必须显式设置 POSTGRES_TEST_ALLOW_RESET=1")
        cls.database_url = validate_postgresql_test_database_url(configured, resolve_database_url())

    def make_engine(self):
        tables, revision = get_database_state(self.database_url)
        if tables - {"alembic_version"} or revision is not None:
            raise RuntimeError("独立测试库非空，禁止迁移或清理")
        config = build_alembic_config(self.database_url)
        command.upgrade(config, "head")
        engine = create_database_engine(self.database_url)

        def cleanup():
            # Only after the empty-database guard and our successful migration.
            # Address migration intentionally refuses downgrade with audit rows.
            with engine.begin() as connection:
                connection.execute(text("DELETE FROM member_address_events"))
                connection.execute(text("DELETE FROM member_addresses"))
            engine.dispose()
            command.downgrade(config, "base")
            remaining, version = get_database_state(self.database_url)
            self.assertFalse(remaining - {"alembic_version"})
            self.assertIsNone(version)

        self.addCleanup(cleanup)
        return engine
