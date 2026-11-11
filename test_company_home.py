import unittest
from unittest.mock import patch

from app import main
from app.browser_sessions import COOKIE_NAME
import test_browser_sessions as fixtures


class CompanyAccessTests(unittest.TestCase):
    setUp = fixtures.BrowserSessionTests.setUp
    login = fixtures.BrowserSessionTests.login
    assert_blocked = fixtures.BrowserSessionTests.assert_blocked
    def test_home_is_public_but_admin_pages_remain_private(self):
        for url in ('/', '/company'):
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200)
            self.assertIn('瑞泓开礼', response.text)
            self.assertIn('href="/login"', response.text)
        for url in ('/dashboard', '/mall-orders', '/member-points', '/mall-catalog'):
            response = self.client.get(url)
            self.assertEqual((response.status_code, response.headers.get('location')), (302, '/login'))
        self.assertEqual(self.login().status_code, 302)
        self.assertEqual(self.client.get('/dashboard').status_code, 200)
        self.assertEqual(self.client.get('/mall-orders').status_code, 200)
        self.assertEqual(self.client.get('/').status_code, 200)
        self.client.get('/logout')
        self.assertEqual(self.client.get('/mall-orders').headers.get('location'), '/login')

    def test_stale_cookie_does_not_break_public_home_or_open_admin(self):
        cookie = {COOKIE_NAME: 'invalid-public-preview-token', 'user_id': str(self.user_id)}
        with patch.object(main, 'SessionLocal', side_effect=AssertionError('public page must not query DB')), patch.object(main, 'get_current_user', side_effect=AssertionError('public page must not inspect identity')):
            for path in ('/', '/company', '/static/company/city.svg'):
                self.assertEqual(self.client.get(path, cookies=cookie).status_code, 200)
        self.assert_blocked(f'{COOKIE_NAME}=invalid-public-preview-token')
        self.assertNotEqual(self.client.get('/company/private').status_code, 200)

    def test_forced_password_change_still_applies_after_home(self):
        from app.models import User
        with self.database.begin() as db:
            db.get(User, self.user_id).must_change_password = True
        self.login()
        self.assertEqual(self.client.get('/').status_code, 200)
        self.assertEqual(self.client.get('/dashboard').headers.get('location'), '/change-password')


if __name__ == '__main__':
    unittest.main()
