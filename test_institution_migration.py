"""Upgrade legacy synthetic data without assigning ambiguous institutions."""
from pathlib import Path
import unittest
from alembic import command
from alembic.config import Config
from sqlalchemy import text,inspect
from app import models as m
from test_candidate_upgrade import fingerprint
from test_institution_isolation import InstitutionIsolationTests

class InstitutionMigrationTests(unittest.TestCase):
    def test_mixed_legacy_order_not_assigned_and_original_rows_unchanged(self):
        f=InstitutionIsolationTests('runTest');f.setUp();self.addCleanup(f.doCleanups)
        order=f.f.place();f.switch_later(f.b)
        # Two proven grant institutions, one historical mixed order: retain it
        # without an invented owner. Tests assemble this predecessor only here.
        f.db.close()
        config=Config('alembic.ini');config.attributes['database_url']='sqlite:///'+f.f.path.as_posix()
        with f.f.engine.begin() as db:
            db.execute(text('DROP TABLE institution_orders'));db.execute(text('DROP TABLE institution_grants'))
        command.stamp(config,'0024_client_price_rules')
        names=[name for name in inspect(f.f.engine).get_table_names() if name!='alembic_version']
        before=fingerprint(f.f.engine,names);command.upgrade(config,'head')
        self.assertEqual(fingerprint(f.f.engine,names),before)
        with f.f.Session() as db:
            self.assertEqual(db.query(m.InstitutionGrant).count(),2)
            self.assertIsNone(db.get(m.InstitutionOrder,order.order_id))
        with self.assertRaises(RuntimeError):f.f.cancel(order.order_public_id)

    def test_unverified_legacy_points_are_not_assigned_by_uploader_or_name(self):
        f=InstitutionIsolationTests('runTest');f.setUp();self.addCleanup(f.doCleanups)
        order=f.f.place()
        f.db.query(m.ClientBatchOwnership).delete();f.db.commit();f.db.close()
        config=Config('alembic.ini');config.attributes['database_url']='sqlite:///'+f.f.path.as_posix()
        with f.f.engine.begin() as db:
            db.execute(text('DROP TABLE institution_orders'));db.execute(text('DROP TABLE institution_grants'))
        command.stamp(config,'0024_client_price_rules');command.upgrade(config,'head')
        with f.f.Session() as db:
            self.assertEqual(db.query(m.InstitutionGrant).count(),0)
            self.assertIsNone(db.get(m.InstitutionOrder,order.order_id))
        # Historical unassigned orders can safely cancel to their own original batches.
        f.f.cancel(order.order_public_id)
        with self.assertRaises(ValueError):f.f.place(key='new-unverified-order')
