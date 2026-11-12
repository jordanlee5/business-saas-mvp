from datetime import datetime,timezone,timedelta
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from tools import staging_external_observer as observer

class ExternalObserverTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.now=datetime(2026,11,12,tzinfo=timezone.utc)
        self.config={'project':'saas-pg-test-cvm','state_root':str(self.root/'state'),
                     'host':'operator@example.invalid','ssh':'ssh','key':str(self.root/'key'),
                     'known_hosts':str(self.root/'known_hosts')}
        for name in ('key','known_hosts'):Path(self.config[name]).write_text('virtual-fixture')
        self.path=self.root/'config.json';self.path.write_text(json.dumps(self.config))

    def good(self,now=None):
        now=now or self.now
        return {'code':'received','value':{'server_epoch':now.timestamp(),
           'timers':{'backup':True,'backup-watch':True},'watch':{'project':'saas-pg-test-cvm',
           'checked_at':now.isoformat(),'status':'ok','notification':'not_needed'}}}

    def test_live_healthy_response(self):
        self.assertEqual(observer.classify(self.good(),self.now),'ok')

    def test_stale_watch_and_stopped_timer(self):
        value=self.good();value['value']['watch']['checked_at']=(self.now-timedelta(minutes=16)).isoformat()
        self.assertEqual(observer.classify(value,self.now),'remote_watch_stale')
        value=self.good();value['value']['timers']['backup-watch']=False
        self.assertEqual(observer.classify(value,self.now),'remote_timer_inactive')

    def test_clock_identity_and_malformed_response(self):
        value=self.good();value['value']['server_epoch']+=600
        self.assertEqual(observer.classify(value,self.now),'observer_server_clock_mismatch')
        value=self.good();value['value']['watch']['project']='other'
        self.assertEqual(observer.classify(value,self.now),'remote_watch_invalid')
        self.assertEqual(observer.classify({'code':'received','value':{}},self.now),'remote_response_invalid')

    def test_watch_fault_and_notification_failure(self):
        value=self.good();value['value']['watch']['status']='alert'
        self.assertEqual(observer.classify(value,self.now),'remote_watch_alert')
        value=self.good();value['value']['watch']['notification']='delivery_failed'
        self.assertEqual(observer.classify(value,self.now),'remote_notification_problem')

    def test_failure_confirmation_dedup_and_recovery_without_sender(self):
        fail=lambda _: {'code':'remote_unreachable_or_ssh_failed'}
        first=observer.observe(self.path,now=self.now,reader=fail)
        self.assertEqual(first['status'],'pending')
        self.assertEqual(observer.observe(self.path,now=self.now+timedelta(minutes=5),reader=fail)['status'],'alert')
        events=self.root/'state/events';count=len(list(events.iterdir()))
        observer.observe(self.path,now=self.now+timedelta(minutes=10),reader=fail)
        self.assertEqual(len(list(events.iterdir())),count)
        recovered=observer.observe(self.path,now=self.now+timedelta(minutes=15),reader=lambda _:self.good(self.now+timedelta(minutes=15)))
        self.assertEqual(recovered['status'],'ok');self.assertFalse(recovered['outbound_notifications'])

    def test_pc_shutdown_gap_resets_failure_streak(self):
        fail=lambda _: {'code':'remote_unreachable_or_ssh_failed'}
        observer.observe(self.path,now=self.now,reader=fail)
        report=observer.observe(self.path,now=self.now+timedelta(hours=12),reader=fail)
        self.assertEqual(report['status'],'pending');self.assertTrue(report['observation_gap_reset'])

    def test_timeout_and_auth_failure_are_not_claimed_as_power_outage(self):
        with patch.object(observer.subprocess,'run',side_effect=subprocess.TimeoutExpired('ssh',35)):
            self.assertEqual(observer.probe(self.config)['code'],'remote_unreachable_or_ssh_failed')
        with patch.object(observer.subprocess,'run',return_value=subprocess.CompletedProcess([],255,b'',b'private-detail')):
            self.assertNotIn('private-detail',json.dumps(observer.probe(self.config)))

    def test_remote_command_fixed_and_strict_host_verification(self):
        response=json.dumps(self.good()['value']).encode()
        with patch.object(observer.subprocess,'run',return_value=subprocess.CompletedProcess([],0,response,b'')) as run:
            self.assertEqual(observer.probe(self.config)['code'],'received')
        args=run.call_args.args[0]
        self.assertIn('StrictHostKeyChecking=yes',args)
        self.assertEqual(args[-1],'sudo -n python3 -')
        self.assertEqual(run.call_args.kwargs['input'],observer.REMOTE.encode())

    def test_invalid_target_and_outbound_configuration_refused(self):
        self.config['host']='-oUnsafe'
        with self.assertRaises(ValueError):observer.probe(self.config)
        self.config['webhook_file']='must-not-be-used';self.path.write_text(json.dumps(self.config))
        with self.assertRaises(ValueError):observer.observe(self.path,reader=lambda _:self.good())

    def test_corrupt_and_oversized_transport_responses(self):
        for raw in (b'bad',b'x'*65537):
            with patch.object(observer.subprocess,'run',return_value=subprocess.CompletedProcess([],0,raw,b'')):
                self.assertEqual(observer.probe(self.config)['code'],'remote_response_invalid')
