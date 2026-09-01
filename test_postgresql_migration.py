import io
import os
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import inspect
from sqlalchemy.engine import make_url

from app import models as application_models  # noqa: F401
from app.database import (
    Base,
    create_database_engine,
    resolve_database_url,
)
from app.migration_baseline import BASELINE_REVISION


PROJECT_ROOT = Path(__file__).resolve().parent
ALEMBIC_CONFIG_PATH = PROJECT_ROOT / "alembic.ini"
POSTGRES_TEST_DATABASE_URL_ENV_NAME = (
    "POSTGRES_TEST_DATABASE_URL"
)
POSTGRES_TEST_ALLOW_RESET_ENV_NAME = (
    "POSTGRES_TEST_ALLOW_RESET"
)


def build_alembic_config(database_url: str) -> Config:
    config = Config(str(ALEMBIC_CONFIG_PATH))
    config.attributes["database_url"] = database_url
    return config


def validate_postgresql_test_database_url(
    database_url: str,
    application_database_url: str,
) -> str:
    normalized_url = database_url.strip()
    if not normalized_url:
        raise ValueError(
            "PostgreSQL 测试数据库地址不能为空"
        )

    candidate = make_url(normalized_url)
    if candidate.drivername != "postgresql+psycopg":
        raise ValueError(
            "PostgreSQL 集成测试必须使用 postgresql+psycopg"
        )

    database_name = candidate.database or ""
    if not database_name.lower().endswith("_test"):
        raise ValueError(
            "PostgreSQL 集成测试数据库名必须以 _test 结尾"
        )

    application_url = make_url(
        application_database_url.strip()
    )
    candidate_target = (
        (candidate.host or "").lower(),
        candidate.port or 5432,
        candidate.database,
    )
    application_target = (
        (application_url.host or "").lower(),
        application_url.port or 5432,
        application_url.database,
    )
    if (
        application_url.get_backend_name() == "postgresql"
        and candidate_target == application_target
    ):
        raise ValueError(
            "PostgreSQL 集成测试数据库不得与应用数据库相同"
        )

    return normalized_url


def get_database_state(
    database_url: str,
) -> tuple[set[str], str | None]:
    engine = create_database_engine(database_url)
    try:
        with engine.connect() as connection:
            tables = set(
                inspect(connection).get_table_names()
            )
            revision = MigrationContext.configure(
                connection
            ).get_current_revision()
            return tables, revision
    finally:
        engine.dispose()


class PostgreSQLTestConfigurationTests(unittest.TestCase):
    def test_accepts_dedicated_psycopg_test_database(self):
        database_url = (
            "postgresql+psycopg://tester:secret@db/"
            "business_saas_test"
        )

        self.assertEqual(
            validate_postgresql_test_database_url(
                database_url,
                "sqlite:///./saas_mvp.db",
            ),
            database_url,
        )

    def test_rejects_non_postgresql_database(self):
        with self.assertRaisesRegex(
            ValueError,
            r"postgresql\+psycopg",
        ):
            validate_postgresql_test_database_url(
                "sqlite:///./business_saas_test.db",
                "sqlite:///./saas_mvp.db",
            )

    def test_rejects_database_without_test_suffix(self):
        with self.assertRaisesRegex(ValueError, "_test"):
            validate_postgresql_test_database_url(
                "postgresql+psycopg://tester:secret@db/"
                "business_saas",
                "sqlite:///./saas_mvp.db",
            )

    def test_rejects_application_database(self):
        database_url = (
            "postgresql+psycopg://test_runner:secret@db/"
            "business_saas_test"
        )
        application_url = (
            "postgresql+psycopg://app_runner:other@db:5432/"
            "business_saas_test"
        )

        with self.assertRaisesRegex(
            ValueError,
            "不得与应用数据库相同",
        ):
            validate_postgresql_test_database_url(
                database_url,
                application_url,
            )

    def test_postgresql_offline_migration_sql_is_portable(self):
        database_url = (
            "postgresql+psycopg://tester:secret@"
            "example.invalid/business_saas_test"
        )
        output = io.StringIO()
        config = build_alembic_config(database_url)
        config.output_buffer = output

        with redirect_stdout(io.StringIO()):
            command.upgrade(config, "head", sql=True)

        migration_sql = output.getvalue()
        for table_name in Base.metadata.tables:
            self.assertIn(
                f"CREATE TABLE {table_name} (",
                migration_sql,
            )

        self.assertIn(
            "is_active BOOLEAN DEFAULT true NOT NULL",
            migration_sql,
        )
        self.assertIn(
            "must_change_password BOOLEAN DEFAULT false NOT NULL",
            migration_sql,
        )
        self.assertIn(
            "is_published BOOLEAN DEFAULT false NOT NULL",
            migration_sql,
        )
        self.assertIn(
            "is_read BOOLEAN DEFAULT false NOT NULL",
            migration_sql,
        )
        self.assertNotIn(
            "BOOLEAN DEFAULT 1",
            migration_sql,
        )
        self.assertNotIn(
            "BOOLEAN DEFAULT 0",
            migration_sql,
        )
        self.assertIn(BASELINE_REVISION, migration_sql)


class PostgreSQLMigrationIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        configured_url = os.environ.get(
            POSTGRES_TEST_DATABASE_URL_ENV_NAME,
            "",
        ).strip()
        if not configured_url:
            raise unittest.SkipTest(
                "未配置独立 PostgreSQL 测试数据库"
            )

        if os.environ.get(
            POSTGRES_TEST_ALLOW_RESET_ENV_NAME,
            "",
        ) != "1":
            raise RuntimeError(
                "运行 PostgreSQL 集成测试前必须显式设置 "
                "POSTGRES_TEST_ALLOW_RESET=1"
            )

        cls.database_url = (
            validate_postgresql_test_database_url(
                configured_url,
                resolve_database_url(),
            )
        )

    def test_upgrade_check_and_downgrade_on_disposable_database(self):
        tables_before, revision_before = get_database_state(
            self.database_url
        )
        self.assertFalse(
            tables_before - {"alembic_version"},
            "PostgreSQL 集成测试只允许使用空测试数据库",
        )
        self.assertIsNone(
            revision_before,
            "PostgreSQL 集成测试库不得已有版本标记",
        )

        config = build_alembic_config(self.database_url)
        upgraded = False
        try:
            command.upgrade(config, "head")
            upgraded = True
            command.check(config)

            tables_after, revision_after = get_database_state(
                self.database_url
            )
            self.assertEqual(
                revision_after,
                BASELINE_REVISION,
            )
            self.assertEqual(
                tables_after - {"alembic_version"},
                set(Base.metadata.tables),
            )
        finally:
            if upgraded:
                command.downgrade(config, "base")

        tables_final, revision_final = get_database_state(
            self.database_url
        )
        self.assertFalse(
            tables_final - {"alembic_version"}
        )
        self.assertIsNone(revision_final)


if __name__ == "__main__":
    unittest.main()
