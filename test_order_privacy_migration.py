from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text, inspect
from app.migration_baseline import ALEMBIC_CONFIG_PATH
from app.schema_readiness import assert_database_schema_ready


class OrderPrivacyMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = Config(str(ALEMBIC_CONFIG_PATH))
        self.config.attributes['database_url'] = 'sqlite:///' + (Path(self.temp.name) / 'isolated.db').as_posix()
        command.upgrade(self.config, '0013_order_delivery_snapshot')
        self.engine = create_engine(self.config.attributes['database_url'])
        self.addCleanup(self.engine.dispose)

    def test_empty_upgrade_downgrade_round_trip(self):
        before = set(inspect(self.engine).get_table_names())
        command.upgrade(self.config, 'head')
        assert_database_schema_ready(self.engine)
        self.assertEqual(set(inspect(self.engine).get_table_names()) - before,
                         {'order_privacy_receipts', 'order_fulfillment_notices'})
        command.downgrade(self.config, '0013_order_delivery_snapshot')
        self.assertEqual(set(inspect(self.engine).get_table_names()), before)
        command.upgrade(self.config, 'head')
        assert_database_schema_ready(self.engine)

    def _check_preserved(self, table, statement):
        command.upgrade(self.config, 'head')
        # Minimal sentinel rows only in this disposable DB, with FK checks off.
        # Production domain tests separately enforce valid orders and FKs.
        with self.engine.begin() as connection:
            connection.execute(text(statement), {'digest': 'a' * 64})
        with self.assertRaisesRegex(RuntimeError, '禁止降级'):
            command.downgrade(self.config, '0013_order_delivery_snapshot')
        assert_database_schema_ready(self.engine)
        with self.engine.connect() as connection:
            self.assertEqual(connection.execute(text('select count(*) from ' + table)).scalar(), 1)

    def test_erasure_receipt_blocks_downgrade(self):
        self._check_preserved('order_privacy_receipts',
            "insert into order_privacy_receipts values (1, :digest, 'isolated-test', '2026-10-28 12:00:00')")

    def test_fulfillment_notice_blocks_downgrade(self):
        self._check_preserved('order_fulfillment_notices',
            "insert into order_fulfillment_notices values (1, :digest, '{}')")
