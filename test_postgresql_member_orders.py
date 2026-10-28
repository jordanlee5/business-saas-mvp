import os
import unittest
from unittest.mock import patch
from alembic import command
from sqlalchemy import text
import test_miniprogram_orders as cases
from app.database import Base, create_database_engine, resolve_database_url
from test_postgresql_migration import validate_postgresql_test_database_url, get_database_state, build_alembic_config

class PostgreSQLMemberOrderTests(cases.MemberOrderTests):
    @classmethod
    def setUpClass(cls):
        configured=os.environ.get('POSTGRES_TEST_DATABASE_URL','')
        if not configured:raise unittest.SkipTest('未配置独立 PostgreSQL 测试数据库')
        if os.environ.get('POSTGRES_TEST_ALLOW_RESET')!='1':raise RuntimeError('必须显式允许独立测试库重置')
        cls.url=validate_postgresql_test_database_url(configured,resolve_database_url())
    def setUp(self):
        tables,revision=get_database_state(self.url)
        if tables-{'alembic_version'} or revision is not None:raise RuntimeError('测试库非空，停止')
        config=build_alembic_config(self.url);command.upgrade(config,'head')
        engine=create_database_engine(self.url)
        def cleanup():
            with engine.begin() as c:
                # Only fixtures created after this test's verified empty-database guard.
                for table in reversed(Base.metadata.sorted_tables):
                    c.execute(table.delete())
            engine.dispose();command.downgrade(config,'base')
            tables,revision=get_database_state(self.url)
            self.assertFalse(tables-{'alembic_version'});self.assertIsNone(revision)
        self.addCleanup(cleanup)
        with patch('test_order_service.create_engine',return_value=engine):super().setUp()
