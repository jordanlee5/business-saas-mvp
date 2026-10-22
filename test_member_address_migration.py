import io
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from app.member_address_migration_check import PREVIOUS, CURRENT, rehearse, file_fingerprints
from app.migration_baseline import ALEMBIC_CONFIG_PATH
from app.schema_readiness import assert_database_schema_ready, DatabaseSchemaNotReadyError


class MemberAddressMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.source = Path(self.temp.name) / "source.db"
        self.config = Config(str(ALEMBIC_CONFIG_PATH))
        self.config.attributes["database_url"] = "sqlite:///" + self.source.as_posix()
        command.upgrade(self.config, PREVIOUS)
        self.engine = create_engine(self.config.attributes["database_url"])
        self.addCleanup(self.engine.dispose)

    def test_copy_rehearsal_preserves_source_and_existing_member(self):
        with self.engine.begin() as db:
            db.execute(text("INSERT INTO members (member_public_id,is_active,created_at,updated_at) "
                            "VALUES ('MEMBER-REHEARSAL',1,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"))
        before = file_fingerprints(self.source)
        result = rehearse(self.source, Path(self.temp.name) / "copy")
        self.assertEqual(result["result"], "PASS")
        self.assertEqual(result["before"]["tables"]["members"]["rows"], 1)
        self.assertEqual(file_fingerprints(self.source), before)
        self.assertEqual(result["live_upgrade"], "NOT_EXECUTED")

    def test_rehearsal_rejects_existing_output_and_wrong_revision(self):
        with self.assertRaises(RuntimeError):
            rehearse(self.source, Path(self.temp.name))
        command.upgrade(self.config, CURRENT)
        with self.assertRaises(RuntimeError):
            rehearse(self.source, Path(self.temp.name) / "wrong")

    def test_schema_and_partial_unique_index_and_readiness(self):
        with self.assertRaises(DatabaseSchemaNotReadyError):
            assert_database_schema_ready(self.engine)
        command.upgrade(self.config, CURRENT)
        assert_database_schema_ready(self.engine)
        indexes = inspect(self.engine).get_indexes("member_addresses")
        index = next(x for x in indexes if x["name"] == "uq_member_addresses_one_default")
        self.assertTrue(index["unique"])
        self.assertIn("is_default", str(index["dialect_options"]["sqlite_where"]))
        with self.engine.begin() as db:
            db.execute(text("DROP TABLE member_address_events"))
        with self.assertRaises(DatabaseSchemaNotReadyError):
            assert_database_schema_ready(self.engine)

    def test_downgrade_refuses_retained_audit_even_when_book_empty(self):
        command.upgrade(self.config, CURRENT)
        with self.engine.begin() as db:
            db.execute(text("INSERT INTO members (id,member_public_id,is_active,created_at,updated_at) "
                            "VALUES (1,'M-AUDIT',1,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"))
            db.execute(text("INSERT INTO member_address_events "
                            "(member_id,address_public_id,action,created_at) "
                            "VALUES (1,:id,'DELETE',CURRENT_TIMESTAMP)"), {"id": "ADDR-" + "a" * 32})
        with self.assertRaisesRegex(RuntimeError, "禁止自动降级"):
            command.downgrade(self.config, PREVIOUS)
        with self.engine.connect() as db:
            self.assertEqual(db.execute(text("SELECT count(*) FROM member_address_events")).scalar(), 1)

    def test_postgresql_offline_upgrade_has_partial_unique_index_and_no_data_changes(self):
        output = io.StringIO()
        config = Config(str(ALEMBIC_CONFIG_PATH), output_buffer=output)
        config.attributes["database_url"] = "postgresql+psycopg://unused/addresses_test"
        command.upgrade(config, PREVIOUS + ":" + CURRENT, sql=True)
        sql = output.getvalue()
        self.assertIn("CREATE UNIQUE INDEX uq_member_addresses_one_default", sql)
        self.assertIn("WHERE is_default = true", sql)
        self.assertNotIn("UPDATE members", sql)
        with self.assertRaisesRegex(RuntimeError, "离线模式"):
            command.downgrade(config, CURRENT + ":" + PREVIOUS, sql=True)
