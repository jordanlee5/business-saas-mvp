import json
import unittest
from datetime import timedelta

from sqlalchemy import text
from sqlalchemy.orm import Session

import test_miniprogram_orders as checkout
from app.mall.order_privacy import aware, review_fingerprint
from app.models import Order
from app.order_delivery_migration_check import file_fingerprints
from app.privacy_rehearsal import rehearse
from app.schema_readiness import CURRENT_SCHEMA_REVISION


class PrivacyPreviewTests(unittest.TestCase):
    def test_preview_reports_candidates_without_erasure(self):
        case = checkout.MemberOrderTests('runTest')
        self.addCleanup(case.doCleanups)
        case.setUp()
        reviewed = case._cancel_for_privacy()
        case.body['request_id'] = 'b' * 32
        unreviewed = case._cancel_for_privacy()
        case.body['request_id'] = 'c' * 32
        active = case.place()['order_public_id']
        with case.f.engine.begin() as db:
            db.execute(text('CREATE TABLE alembic_version (version_num VARCHAR(32) PRIMARY KEY)'))
            db.execute(text('INSERT INTO alembic_version VALUES (:rev)'), {'rev': CURRENT_SCHEMA_REVISION})
        with Session(case.f.engine) as db:
            order = db.query(Order).filter_by(order_public_id=reviewed).one()
            reviews = {reviewed: dict(clearance='NO_HOLD_OR_OPEN_CASE', order=review_fingerprint(order))}
            now = aware(order.updated_at) + timedelta(days=1)

        policy = dict(version='test-only', approved=True, days=dict(CANCELLED=1, COMPLETED=1, REFUNDED=1))
        source = case.f.path
        original = file_fingerprints(source)
        output = source.parent / 'preview'
        result = rehearse(source, output, policy, reviews, now, preview_only=True)
        self.assertEqual(result['counts'], dict(reviewed=3, eligible=1, cleared=0))
        self.assertEqual({row['order_public_id']: row['reason'] for row in result['orders']},
                         {reviewed: 'due_and_reviewed', unreviewed: 'review_required', active: 'active_order'})
        self.assertEqual(list(output.iterdir()), [output / 'report.json'])
        report = (output / 'report.json').read_text('utf8')
        self.assertEqual(json.loads(report), result)
        for field in ('recipient_name', 'detail', 'phone'):
            self.assertNotIn(checkout.BODY[field], report)
        self.assertEqual(file_fingerprints(source), original)
        self.assertEqual(case.call('GET', '/' + reviewed).json()['address'], checkout.BODY)

        conflict = source.parent / 'conflict'
        with self.assertRaisesRegex(ValueError, '不能重放'):
            rehearse(source, conflict, policy, reviews, now, receipt_journal=[], preview_only=True)
        self.assertFalse(conflict.exists())
        applied = rehearse(source, source.parent / 'apply', policy, reviews, now)
        self.assertEqual(applied['counts'], dict(reviewed=3, cleared=1))
        self.assertEqual([row['order_public_id'] for row in applied['orders'] if row['reason']=='cleared'], [reviewed])
        self.assertEqual(file_fingerprints(source), original)
