"""Synthetic HTTP, image safety, membership, concurrency and migration checks."""
import os
from contextlib import contextmanager
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import create_engine, inspect
from sqlalchemy.exc import OperationalError

from app import main, models
from app.admin_permissions import OPERATOR, PRIMARY_REVIEWER
from app.mall import home_entries as service
from app.mall.audit import MallAuditActionType
from app.schema_readiness import CURRENT_SCHEMA_REVISION
import test_browser_sessions as browser
import test_miniprogram_orders as members


def png(color='green', size=(900, 600)):
    output = BytesIO()
    Image.new('RGB', size, color).save(output, format='PNG')
    return output.getvalue()


class HomeEntryAdminTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, {'WECHAT_MINIPROGRAM_APP_ID': members.APPID,
            'MINIPROGRAM_SESSION_SIGNING_KEY': 'home-entry-test-only-key-20261112'}))
        browser.BrowserSessionTests.setUp(self)
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'images'
        self.enterContext(patch.object(service, 'STORAGE_ROOT', self.root))
        self.enterContext(patch('app.api.miniprogram_v1.SessionLocal', self.database))
        self.assertEqual(browser.BrowserSessionTests.login(self).status_code, 302)
        self.assertEqual(self.client.get('/mall-home').status_code, 200)
        self.csrf = self.client.cookies.get('mall_home_csrf')

    def edit(self, key='home', revision=0, action='upload', content=None, filename='virtual.png', csrf=None):
        return self.client.post('/mall-home/' + key, data={'revision': revision, 'action': action,
            'csrf_token': self.csrf if csrf is None else csrf},
            files={'image_file': (filename, png() if content is None else content, 'image/png')} if action == 'upload' else None)

    def row(self, key='home'):
        with self.database() as db:
            return next(x for x in service.snapshot(db) if x['key'] == key)

    def test_all_eight_slots_upload_and_preview_with_real_admin_session(self):
        self.assertEqual(len(service.ENTRIES), 8)
        for key, name in service.ENTRIES:
            self.assertEqual(self.edit(key).status_code, 303)
            row = self.row(key)
            image = self.client.get(row['image_url'])
            self.assertEqual(image.status_code, 200)
            self.assertEqual(image.headers['cache-control'], 'no-store, private')
            with Image.open(BytesIO(image.content)) as decoded:
                self.assertEqual(decoded.format, 'WEBP')
                self.assertEqual(decoded.size, (640, 427))
                self.assertFalse(decoded.info.get('exif'))
        page = self.client.get('/mall-home')
        self.assertEqual(page.text.count('class="entry-card"'), 8)
        with self.database() as db:
            self.assertEqual(db.query(models.AdminActionLog).filter_by(
                action_type=MallAuditActionType.HOME_ENTRY_IMAGE_UPDATE.value).count(), 8)

    def test_replace_reset_and_old_files_remain_private(self):
        self.edit(); original = self.row()['image_url']
        self.edit(revision=1, content=png('red')); replacement = self.row()['image_url']
        self.assertNotEqual(original, replacement)
        self.assertTrue(service.image_target(original).exists())
        self.assertEqual(self.client.get(original).status_code, 404)
        self.assertEqual(self.client.head(replacement).status_code, 200)
        self.edit(revision=2, action='reset')
        self.assertEqual(self.row()['revision'], 3)
        self.assertIsNone(self.row()['image_url'])
        self.assertEqual(self.client.get(replacement).status_code, 404)

    def test_stale_first_upload_and_replacement_cannot_overwrite(self):
        self.edit(); first = self.row()
        for revision in (0, 9):
            result = self.edit(revision=revision)
            self.assertIn('error=', result.headers['location'])
            self.assertEqual(self.row(), first)
        self.assertEqual(len(list(self.root.rglob('*.webp'))), 1)
        self.edit(revision=1)
        second = self.row()
        self.edit(revision=1, action='reset')
        self.assertEqual(self.row(), second)

    def test_invalid_format_mismatch_size_and_svg_leave_configuration_unchanged(self):
        self.edit(); first = self.row()
        for content, filename in ((b'<svg/>', 'x.svg'), (png(), 'x.jpg'), (b'invalid', 'x.png'),
                                  (b'x' * (service.MAX_PRODUCT_IMAGE_BYTES + 1), 'x.png')):
            self.assertIn('error=', self.edit(revision=1, content=content, filename=filename).headers['location'])
            self.assertEqual(self.row(), first)
        self.assertEqual(len(list(self.root.rglob('*.webp'))), 1)

    def test_csrf_bad_slot_or_operation_do_not_create_files(self):
        self.assertEqual(self.edit(csrf='wrong').status_code, 403)
        self.assertEqual(self.edit(key='other').status_code, 400)
        self.assertEqual(self.edit(action='delete').status_code, 400)
        self.assertFalse(self.root.exists())

    def test_readonly_role_inactive_and_forced_password_change_denied(self):
        for values in ({'admin_level': PRIMARY_REVIEWER}, {'is_active': False}, {'must_change_password': True}):
            user = SimpleNamespace(id=self.user_id, role='admin', admin_level=OPERATOR,
                                   is_active=True, must_change_password=False)
            for k,v in values.items(): setattr(user, k, v)
            with patch.object(main, 'get_current_user', return_value=user):
                expected = 302 if values.get('must_change_password') else 403
                self.assertEqual(self.client.get('/mall-home').status_code, expected)
                self.assertEqual(self.edit().status_code, expected)
        self.assertFalse(self.root.exists())

    def test_anonymous_and_forged_member_token_never_use_admin_cookie(self):
        self.edit(); url = self.row()['image_url']
        self.assertEqual(self.client.get(url, headers={'Authorization':'Bearer forged'}).status_code, 401)
        self.client.cookies.clear()
        for path in ('/mall-home', url, '/api/miniprogram/v1/home-entries'):
            self.assertEqual(self.client.get(path).status_code, 401)
        self.assertEqual(self.edit().status_code, 401)

    def test_storage_or_database_failure_preserves_previous_image(self):
        self.edit(); first = self.row()
        with patch.object(service, 'set_image', side_effect=OperationalError('synthetic', {}, Exception())):
            self.assertIn('error=', self.edit(revision=1).headers['location'])
        self.assertEqual(self.row(), first)
        self.assertEqual(len(list(self.root.rglob('*.webp'))), 2)
        with patch.object(service, 'save_image', side_effect=OSError('synthetic full disk')):
            self.assertIn('error=', self.edit(revision=1).headers['location'])
        self.assertEqual(self.row(), first)

    def test_lost_commit_acknowledgement_never_deletes_published_file(self):
        self.edit()
        factory = self.database
        class LostAcknowledgement:
            def __call__(self):
                return factory()
            @contextmanager
            def begin(self):
                with factory.begin() as db:
                    yield db
                raise OperationalError('synthetic lost commit acknowledgement', {}, Exception())
        with patch.object(main, 'SessionLocal', LostAcknowledgement()):
            self.assertIn('error=', self.edit(revision=1).headers['location'])
        saved = self.row()
        self.assertEqual(saved['revision'], 2)
        self.assertTrue(service.image_target(saved['image_url']).is_file())
        self.assertEqual(self.client.get(saved['image_url']).status_code, 200)

    def test_service_rechecks_actor_permissions_and_image_owner(self):
        image = service.save_image('home', png(), 'virtual.png')
        with self.database.begin() as db:
            with self.assertRaises(ValueError): service.set_image(db, self.user_id, 'kids', 0, image)
        with self.database.begin() as db:
            db.get(models.User, self.user_id).admin_level = PRIMARY_REVIEWER
        with self.database.begin() as db:
            with self.assertRaises(PermissionError): service.set_image(db, self.user_id, 'home', 0, image)

    def test_paths_cannot_escape_dedicated_storage(self):
        for url in ('/uploads/mall_home/../x', '/uploads/mall_home/home/../../x.webp',
                    '/uploads/mall_home/home/' + 'a'*32 + '.webp?x=1',
                    '/uploads/mall_products/PRD-X/main.webp'):
            with self.assertRaises(ValueError): service.image_target(url)


class HomeEntryMemberTests(unittest.TestCase):
    def setUp(self):
        self.case = members.MemberOrderTests('runTest')
        self.addCleanup(self.case.doCleanups)
        self.case.setUp()
        self.f = self.case.f
        self.enterContext(patch('app.api.miniprogram_v1.SessionLocal', self.f.Session))
        self.enterContext(patch.object(main, 'get_current_user', return_value=None))
        self.enterContext(patch.object(service, 'STORAGE_ROOT', self.f.path.parent / 'home-images'))
        self.url = service.save_image('food', png(), 'virtual.png')
        with self.f.Session.begin() as db:
            service.set_image(db, self.f.operator.id, 'food', 0, self.url)
        self.client = TestClient(main.app)
        self.addCleanup(self.client.close)

    def test_member_qualification_applies_to_manifest_get_head_and_range(self):
        for index, status in ((0, 200), (1, 403)):
            auth = {'Authorization': 'Bearer ' + self.case.tokens[index]}
            for url in ('/api/miniprogram/v1/home-entries', self.url):
                response = self.client.get(url, headers=auth)
                self.assertEqual(response.status_code, status)
                self.assertEqual(response.headers['cache-control'], 'no-store, private')
            self.assertEqual(self.client.head(self.url, headers=auth).status_code, status)
            self.assertEqual(self.client.get(self.url, headers={**auth, 'Range':'bytes=0-9'}).status_code, 206 if index == 0 else 403)
        auth = {'Authorization': 'Bearer ' + self.case.tokens[0]}
        data = self.client.get('/api/miniprogram/v1/home-entries', headers=auth).json()
        self.assertEqual(len(data), 8)
        self.assertEqual(data[1], {'key': 'food', 'name': '滋味零食', 'image_url': self.url})
        with self.f.Session.begin() as db:
            db.query(models.MemberQualification).update({'enabled':False})
        self.assertEqual(self.client.get(self.url, headers=auth).status_code, 403)

    def test_home_image_symlink_cannot_escape(self):
        original = service.image_target(self.url)
        private = self.f.path.parent / 'private-sentinel.webp'
        private.write_bytes(b'private sentinel')
        original.unlink()
        try:
            original.symlink_to(private)
        except OSError:
            self.skipTest('Windows symlink privilege unavailable; covered on Linux')
        auth = {'Authorization': 'Bearer ' + self.case.tokens[0]}
        response = self.client.get(self.url, headers=auth)
        self.assertEqual(response.status_code, 404)
        self.assertNotIn(b'private sentinel', response.content)

    def test_missing_files_and_wrong_names_are_private_404(self):
        service.image_target(self.url).unlink()
        auth = {'Authorization': 'Bearer ' + self.case.tokens[0]}
        for url in (self.url, '/uploads/mall_home/home/' + '0'*32 + '.webp', '/uploads/mall_home/home/not-an-image'):
            self.assertEqual(self.client.get(url, headers=auth).status_code, 404)


class HomeEntryMigrationTests(unittest.TestCase):
    def test_upgrade_metadata_readiness_and_nonempty_downgrade_guard(self):
        with TemporaryDirectory() as directory:
            config = Config('alembic.ini')
            url = 'sqlite:///' + (Path(directory) / 'virtual.db').as_posix()
            config.attributes['database_url'] = url
            command.upgrade(config, '0018_member_qualifications')
            command.upgrade(config, 'head')
            command.check(config)
            engine = create_engine(url)
            try:
                self.assertIn('home_entry_images', inspect(engine).get_table_names())
                self.assertEqual(CURRENT_SCHEMA_REVISION, '0019_home_entry_images')
                command.downgrade(config, '0018_member_qualifications')
                command.upgrade(config, 'head')
                with engine.begin() as c:
                    c.exec_driver_sql("INSERT INTO home_entry_images (entry_key,revision,updated_by_id,updated_at) VALUES ('home',1,1,'2026-11-12')")
                with self.assertRaises(RuntimeError): command.downgrade(config, '0018_member_qualifications')
                self.assertIn('home_entry_images', inspect(engine).get_table_names())
            finally:
                engine.dispose()
