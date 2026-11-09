"""Qualification status is private, read-only, and independent of history access."""
from datetime import timedelta
from unittest.mock import patch
import unittest
from sqlalchemy.exc import OperationalError
from app import models as m
import test_member_catalog_access as access_cases
from test_order_service import NOW


class MemberCatalogEntryTests(unittest.TestCase):
    def setUp(self):
        self.f = access_cases.MemberCatalogAccessTests('runTest')
        self.addCleanup(self.f.doCleanups)
        self.f.setUp()

    def status(self, owner=0):
        return self.f.request('/api/miniprogram/v1/catalog-access', owner=owner)

    def test_identity_and_minimal_status(self):
        self.assertEqual(self.status(None).status_code, 401)
        self.assertEqual(self.status(1).json(), {'status':'unconfirmed','valid_for_seconds':0})
        result = self.status().json()
        self.assertEqual(set(result), {'status','valid_for_seconds'})
        self.assertEqual(result['status'], 'eligible')
        self.assertGreater(result['valid_for_seconds'], 0)
        self.assertLessEqual(result['valid_for_seconds'], 900)

    def test_status_transitions_and_history_identity(self):
        self.f.change('DISABLE')
        self.assertEqual(self.status().json()['status'], 'disabled')
        # No account-wide disable: /auth/me still works and catalog remains closed.
        response = self.f.client.get('/api/miniprogram/v1/auth/me', headers={'Authorization':'Bearer '+self.f.case.tokens[0]})
        self.assertEqual(response.status_code, 200)
        self.f.change('ENABLE')
        self.assertEqual(self.status().json()['status'], 'eligible')
        with self.f.f.Session.begin() as db:
            row = db.query(m.MemberQualification).filter_by(member_id=self.f.f.member.id).one()
            row.valid_until = NOW - timedelta(seconds=1)
        self.assertEqual(self.status().json()['status'], 'expired')
        with self.f.f.Session.begin() as db:
            row = db.query(m.MemberQualification).filter_by(member_id=self.f.f.member.id).one()
            row.valid_from = NOW + timedelta(hours=1)
            row.valid_until = NOW + timedelta(days=1)
        self.assertEqual(self.status().json()['status'], 'pending')

    def test_fault_does_not_report_eligible_and_recovers(self):
        with patch('app.api.miniprogram_v1._require_current_member', side_effect=OperationalError('virtual',{},Exception('offline'))):
            self.assertEqual(self.status().status_code, 503)
        self.assertEqual(self.status().json()['status'], 'eligible')

    def test_remaining_time_not_point_balance_and_not_client_identity(self):
        with self.f.f.Session.begin() as db:
            row = db.query(m.MemberQualification).filter_by(member_id=self.f.f.member.id).one()
            row.valid_until = NOW + timedelta(seconds=3)
        self.assertEqual(self.status().json()['valid_for_seconds'], 3)
        response = self.f.request('/api/miniprogram/v1/catalog-access?member_id='+str(self.f.f.member.id), owner=1)
        self.assertEqual(response.json()['status'], 'unconfirmed')
