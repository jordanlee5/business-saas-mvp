import unittest
from tools.check_secrets import inspect


class SecretGuardTests(unittest.TestCase):
    def test_blocks_private_file_even_with_unknown_format(self):
        for name in ['.env', '.env.production', 'private/server-config.json', 'private.key', 'private/admin-captcha.json', 'private/admin-captcha-prod.json']:
            self.assertIn('private-file', inspect(name, b'unknown-format'))

    def test_identifiers_and_environment_lookups_are_not_secrets(self):
        self.assertEqual(inspect('code.py', b"APP_ID='wx0123456789abcdef'"), [])
        self.assertEqual(inspect('code.py', b"secret=os.environ['WECHAT_MINIPROGRAM_APP_SECRET']"), [])

    def test_detects_without_returning_secret_value(self):
        for prefix, value, expected in [
            (b'WECHAT_MINIPROGRAM_APP_SECRET=', b'abcdef01'*4, 'wechat-secret'),
            (b'', b'gh' + b'p_' + b'a'*36, 'github-token'),
            (b'', b'-----BEGIN ' + b'PRIVATE KEY-----', 'private-key'),
        ]:
            self.assertEqual(inspect('code.py', prefix+value), [expected])
