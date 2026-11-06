"""Same member isolation/failure checks against an explicitly disposable PG DB."""
import os
import unittest
from unittest.mock import patch
from alembic import command
from app.database import Base, create_database_engine, resolve_database_url
from test_postgresql_migration import validate_postgresql_test_database_url, get_database_state, build_alembic_config
import test_parcel_tracking as cases


class PostgreSQLParcelTrackingTests(cases.ParcelTrackingTests):
    @classmethod
    def setUpClass(cls):
        url=os.environ.get('POSTGRES_TEST_DATABASE_URL','')
        if not url:raise unittest.SkipTest('未配置独立 PostgreSQL 测试数据库')
        if os.environ.get('POSTGRES_TEST_ALLOW_RESET')!='1':raise RuntimeError('测试库重置未授权')
        cls.url=validate_postgresql_test_database_url(url,resolve_database_url())

    def setUp(self):
        tables,revision=get_database_state(self.url)
        if tables-{'alembic_version'} or revision is not None:raise RuntimeError('独立测试库必须为空')
        config=build_alembic_config(self.url);command.upgrade(config,'head')
        engine=create_database_engine(self.url)
        def cleanup():
            with engine.begin() as c:
                for table in reversed(Base.metadata.sorted_tables):c.execute(table.delete())
            engine.dispose();command.downgrade(config,'base')
            tables,revision=get_database_state(self.url)
            self.assertFalse(tables-{'alembic_version'});self.assertIsNone(revision)
        self.addCleanup(cleanup)
        with patch('test_order_service.create_engine',return_value=engine):super().setUp()
