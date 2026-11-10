import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests
from app import login_captcha as gate
from app import main
from app.browser_sessions import COOKIE_NAME
import test_browser_sessions as existing


def virtual_config():
    return dict(app_id='123', app_secret_key='virtual-only',
                secret_id='virtual-id', secret_key='virtual-signing-only')


class CaptchaProtocolTests(unittest.TestCase):
    def setUp(self):
        change = patch.object(gate, 'settings', return_value=virtual_config())
        change.start()
        self.addCleanup(change.stop)

    def reply(self, result):
        response = Mock()
        response.json.return_value = {'Response': result}
        return response

    def test_only_server_verified_result_accepted_and_fixed_transport(self):
        with patch.object(gate.requests, 'post', return_value=self.reply({'CaptchaCode': 1})) as send:
            gate.verify('virtual-ticket', 'virtual-random', '192.0.2.1')
        args, kwargs = send.call_args
        self.assertEqual(args, ('https://captcha.tencentcloudapi.com/',))
        self.assertEqual(kwargs['timeout'], (2, 5))
        self.assertFalse(kwargs['allow_redirects'])
        payload = json.loads(kwargs['data'])
        self.assertEqual(payload['UserIp'], '192.0.2.1')
        self.assertEqual(payload['CaptchaType'], 9)
        self.assertEqual(payload['CaptchaAppId'], 123)
        self.assertIn('TC3-HMAC-SHA256 Credential=virtual-id/', kwargs['headers']['Authorization'])

    def test_provider_rejects_expiry_replay_mismatch_disaster_and_risk(self):
        for code in (0, 7, 8, 9, 15, 16, 21, 100, True, '1'):
            with self.subTest(code=code), patch.object(gate.requests, 'post', return_value=self.reply({'CaptchaCode': code})):
                with self.assertRaises(gate.CaptchaRejected):
                    gate.verify('virtual-ticket', 'random', '192.0.2.1')
        with patch.object(gate.requests, 'post', return_value=self.reply({'CaptchaCode': 1, 'EvilLevel': 100})):
            with self.assertRaises(gate.CaptchaRejected):
                gate.verify('virtual-ticket', 'random', '192.0.2.1')

    def test_missing_oversize_or_disaster_tickets_never_call_provider(self):
        with patch.object(gate.requests, 'post') as send:
            for ticket, randstr in (('', 'r'), ('t', ''), ('trerror_virtual', 'r'), ('t'*4097, 'r'), ('t', 'r'*257)):
                with self.subTest(ticket_length=len(ticket)), self.assertRaises(gate.CaptchaRejected):
                    gate.verify(ticket, randstr, '192.0.2.1')
            send.assert_not_called()

    def test_outage_malformed_and_api_errors_fail_closed(self):
        replies = [self.reply({'Error': {'Code': 'AuthFailure'}}), self.reply({}), self.reply(None)]
        for response in replies:
            with patch.object(gate.requests, 'post', return_value=response), self.assertRaises(gate.CaptchaUnavailable):
                gate.verify('virtual-ticket', 'random', '192.0.2.1')
        with patch.object(gate.requests, 'post', side_effect=requests.Timeout('virtual')), self.assertRaises(gate.CaptchaUnavailable):
            gate.verify('virtual-ticket', 'random', '192.0.2.1')


class CaptchaConfigTests(unittest.TestCase):
    def test_disabled_does_not_make_external_requests(self):
        with patch.dict(os.environ, {'ADMIN_CAPTCHA_CONFIG': ''}), patch.object(gate.requests, 'post') as send:
            gate.verify('', '', None)
            self.assertFalse(gate.public_context()['captcha_required'])
            send.assert_not_called()

    def test_missing_invalid_file_and_public_context_do_not_leak_secrets(self):
        with tempfile.TemporaryDirectory() as folder:
            p = Path(folder)/'private.json'
            with patch.dict(os.environ, {'ADMIN_CAPTCHA_CONFIG': str(p)}):
                for content in (None, 'broken', '{}', '[]', '{"app_id": "<script>"}'):
                    if content is not None:
                        p.write_text(content, encoding='utf-8')
                    with self.assertRaises(gate.CaptchaUnavailable):
                        gate.verify('t', 'r', '192.0.2.1')
                    self.assertTrue(gate.public_context()['captcha_unavailable'])
                p.write_text(json.dumps(virtual_config()), encoding='utf-8')
                self.assertEqual(gate.public_context(), {'captcha_required': True, 'captcha_app_id': '123', 'captcha_unavailable': False})


class CaptchaLoginTests(existing.BrowserSessionTests):
    # Existing virtual account/session tests also exercise the disabled feature.
    def setUp(self):
        super().setUp()
        change = patch.dict(os.environ, {'ADMIN_CAPTCHA_CONFIG': ''})
        change.start()
        self.addCleanup(change.stop)

    def test_enabled_gate_blocks_before_password_or_database_and_recovers(self):
        with patch.object(gate, 'settings', return_value=virtual_config()):
            page = self.client.get('/login')
            self.assertIn('login-captcha.js', page.text)
            self.assertIn('no-store', page.headers['cache-control'])
            self.assertNotIn('virtual-only', page.text)
            with patch.object(main, 'SessionLocal') as database, patch.object(main, 'verify_password') as password:
                response = self.login()
                self.assertEqual(response.status_code, 400)
                database.assert_not_called()
                password.assert_not_called()
                self.assertNotIn(COOKIE_NAME, response.cookies)
            fields = dict(username='operator', password=self.password,
                          captcha_ticket='virtual-ticket', captcha_randstr='random')
            with patch.object(gate.requests, 'post', side_effect=requests.Timeout()):
                response = self.client.post('/login', data=fields)
                self.assertEqual(response.status_code, 503)
                self.assertNotIn(self.password, response.text)
                self.assertNotIn(COOKIE_NAME, response.cookies)
            reply = Mock()
            reply.json.return_value = {'Response': {'CaptchaCode': 1}}
            with patch.object(gate.requests, 'post', return_value=reply) as send:
                response = self.client.post('/login', data=fields, headers={'X-Forwarded-For': '198.51.100.9'})
                # TestClient host must be a valid IP to reach provider.
                self.assertEqual(response.status_code, 503)
                send.assert_not_called()
            # ASGI's trusted client address, with a forged raw forwarding header.
            from fastapi.testclient import TestClient
            with patch.object(gate.requests, 'post', return_value=reply):
                client = TestClient(main.app, base_url='https://testserver', client=('192.0.2.1', 1234), follow_redirects=False)
                self.addCleanup(client.close)
                response = client.post('/login', data=fields, headers={'X-Forwarded-For': '198.51.100.9'})
                self.assertEqual(response.status_code, 302)
                self.assertIn(COOKIE_NAME, response.cookies)
                sent = json.loads(gate.requests.post.call_args.kwargs['data'])
                self.assertEqual(sent['UserIp'], '192.0.2.1')
                fields['password'] = 'incorrect'
                response = client.post('/login', data=fields)
                self.assertIn('用户名或密码错误', response.text)
                self.assertIn('login-captcha.js', response.text)
                self.assertNotIn(COOKIE_NAME, response.cookies)


if __name__ == '__main__':
    unittest.main()
