from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine,text
from app.order_delivery_migration_check import rehearse,PREVIOUS,CURRENT,file_fingerprints
from app.migration_baseline import ALEMBIC_CONFIG_PATH
from app.schema_readiness import assert_database_schema_ready
class OrderDeliveryMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp=TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'source.db'
        self.config=Config(str(ALEMBIC_CONFIG_PATH));self.config.attributes['database_url']='sqlite:///'+self.path.as_posix()
        command.upgrade(self.config,PREVIOUS)
    def test_rehearsal_preserves_source_and_round_trip(self):
        before=file_fingerprints(self.path)
        report=rehearse(self.path,Path(self.temp.name)/'rehearsal')
        self.assertEqual(report['result'],'PASS');self.assertEqual(before,file_fingerprints(self.path))
        self.assertEqual(report['live_upgrade'],'NOT_EXECUTED')
    def test_head_readiness_and_retained_snapshot_prevents_downgrade(self):
        command.upgrade(self.config,CURRENT)
        engine=create_engine(self.config.attributes['database_url']);self.addCleanup(engine.dispose)
        assert_database_schema_ready(engine)
        # Minimal fixture only in this disposable SQLite; production foreign keys remain enforced.
        with engine.begin() as c:
            c.execute(text("INSERT INTO order_delivery_snapshots (order_id,address_public_id,request_digest,recipient_name,phone,province,city,district,detail) VALUES (1,:id,:digest,'test','1234567','p','c','d','x')"),{'id':'ADDR-'+'a'*32,'digest':'a'*64})
        with self.assertRaisesRegex(RuntimeError,'禁止自动降级'):command.downgrade(self.config,PREVIOUS)
