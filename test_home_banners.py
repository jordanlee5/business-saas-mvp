"""Synthetic carousel HTTP, publication, file safety and migration regressions."""
from contextlib import contextmanager
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from alembic import command
from alembic.config import Config
from PIL import Image
from sqlalchemy import create_engine, inspect
from sqlalchemy.exc import OperationalError
from app import main, models
from app.mall import home_banners as service
from app.mall.audit import MallAuditActionType
import test_home_entry_images as fixtures


class BannerAdminTests(unittest.TestCase):
    def setUp(self):
        self.case = fixtures.HomeEntryAdminTests('runTest')
        self.addCleanup(self.case.doCleanups)
        self.case.setUp()
        self.client, self.database, self.csrf = self.case.client, self.case.database, self.case.csrf
        self.user_id = self.case.user_id
        self.root = Path(self.case.temp.name) / 'banners'
        self.enterContext(patch.object(service, 'STORAGE_ROOT', self.root))

    def save(self, slot=1, revision=0, upload=True, **values):
        data = dict(action='save', revision=revision, title=f'虚拟素材{slot}', sort_order=slot*10,
                    enabled='false', target_category='', csrf_token=self.csrf)
        data.update(values)
        files = {'image_file':('virtual.png', fixtures.png(size=(2400,960)), 'image/png')} if upload else None
        return self.client.post(f'/mall-home/banners/{slot}', data=data, files=files)

    def row(self, slot=1):
        with self.database() as db:
            return service.snapshot(db)[slot-1]

    def toggle(self, slot=1, revision=1, enabled=True):
        return self.save(slot, revision, upload=False, action='toggle', enabled=str(enabled).lower())

    def test_empty_editor_and_upload_default_disabled(self):
        page = self.client.get('/mall-home?section=carousel')
        self.assertEqual(page.status_code, 200)
        self.assertEqual(page.text.count('class="banner-card"'), 5)
        self.assertIn('brand-fallback', page.text)
        self.assertEqual(self.save().status_code,303)
        row = self.row()
        self.assertFalse(row['enabled'])
        with self.database() as db:
            self.assertEqual(service.published(db), [])
            self.assertEqual(db.query(models.AdminActionLog).filter_by(action_type=MallAuditActionType.HOME_BANNER_UPDATE.value).count(),1)
        result = self.client.get(row['image_url'])
        self.assertEqual(result.status_code,200)
        with Image.open(BytesIO(result.content)) as image:
            self.assertEqual(image.format,'WEBP')
            self.assertEqual(image.size,(1600,640))
            self.assertFalse(image.info.get('exif'))

    def test_replace_preserves_previous_file_but_old_url_is_inaccessible(self):
        self.save(enabled='true'); first = self.row()['image_url']
        self.save(revision=1,enabled='true'); second = self.row()['image_url']
        self.assertNotEqual(first,second)
        self.assertTrue(service.image_target(first).exists())
        self.assertEqual(self.client.get(first).status_code,404)
        self.assertEqual(self.client.get(second).status_code,200)

    def test_sort_ties_enable_disable_and_metadata_only_update(self):
        for slot,order in ((1,20),(2,5),(3,5)):
            self.save(slot,sort_order=order,enabled='true',target_category='food')
        with self.database() as db:
            self.assertEqual([x['slot'] for x in service.published(db)],[2,3,1])
        old_url = self.row()['image_url']
        self.save(revision=1,upload=False,sort_order=0,title='换季推荐',enabled='true',target_category='kids')
        self.assertEqual(self.row()['image_url'],old_url)
        with self.database() as db:
            self.assertEqual([x['slot'] for x in service.published(db)],[1,2,3])
        self.toggle(revision=2,enabled=False)
        self.assertFalse(self.row()['enabled'])
        self.assertEqual(self.row()['title'],'换季推荐')
        self.assertEqual(self.row()['target_category'],'kids')
        self.toggle(revision=3,enabled=True)
        self.assertTrue(self.row()['enabled'])

    def test_old_upload_reorder_or_toggle_cannot_overwrite(self):
        self.save(); first = self.row()
        for revision in (0,3):
            self.assertIn('error=',self.save(revision=revision).headers['location'])
            self.assertEqual(self.row(),first)
        self.toggle(revision=1)
        second = self.row()
        self.toggle(revision=1,enabled=False)
        self.save(revision=1,upload=False,sort_order=0)
        self.assertEqual(self.row(),second)
        self.assertEqual(len(list(self.root.rglob('*.webp'))),1)

    def test_bad_input_and_external_targets_leave_published_state_unchanged(self):
        self.save(enabled='true'); first=self.row()
        for values in ({'sort_order':-1},{'sort_order':1000},{'title':''},{'title':'x'*81},
                       {'target_category':'https://evil.invalid'},{'target_category':'../orders'}):
            self.assertIn('error=',self.save(revision=1,upload=False,**values).headers['location'])
            self.assertEqual(self.row(),first)
        self.assertEqual(self.save(slot=6).status_code,400)
        self.assertEqual(self.save(revision=-1).status_code,400)
        self.assertEqual(self.save(revision=2147483647).status_code,400)
        self.assertEqual(self.save(action='delete').status_code,400)

    def test_empty_slot_cannot_be_published_without_an_image(self):
        for response in (self.toggle(revision=0),self.save(upload=False,enabled='true')):
            self.assertIn('error=',response.headers['location'])
        self.assertEqual(self.row()['revision'],0)

    def test_invalid_image_content_size_and_extension_do_not_replace(self):
        self.save(); first=self.row()
        data=dict(action='save',revision=1,title='素材',sort_order=0,csrf_token=self.csrf)
        for filename,content in (('x.svg',b'<svg/>'),('x.jpg',fixtures.png()),('x.png',b'bad'),
                                 ('x.png',b'x'*(service.MAX_PRODUCT_IMAGE_BYTES+1))):
            response=self.client.post('/mall-home/banners/1',data=data,files={'image_file':(filename,content,'image/png')})
            self.assertIn('error=',response.headers['location'])
            self.assertEqual(self.row(),first)
        self.assertEqual(len(list(self.root.rglob('*.webp'))),1)

    def test_csrf_and_permissions_are_required_for_edits(self):
        self.assertEqual(self.save(csrf_token='wrong').status_code,403)
        for values in ({'admin_level':'primary_reviewer'},{'role':'partner'},{'is_active':False}):
            user=SimpleNamespace(id=self.user_id,role='admin',admin_level='operator',is_active=True,must_change_password=False)
            for key,value in values.items(): setattr(user,key,value)
            with patch.object(main,'get_current_user',return_value=user):
                self.assertEqual(self.save().status_code,403)
                self.assertEqual(self.client.get('/mall-home?section=carousel').status_code,403)
        self.client.cookies.clear()
        self.assertEqual(self.save().status_code,401)
        self.assertFalse(self.root.exists())

    def test_actor_is_rechecked_in_service_and_cross_slot_image_is_denied(self):
        image=service.save_image(1,fixtures.png(),'virtual.png')
        with self.database.begin() as db:
            with self.assertRaises(ValueError):
                service.change(db,self.user_id,2,0,action='save',image_url=image,title='虚拟',sort_order=0)
        with self.database.begin() as db:
            db.get(models.User,self.user_id).is_active=False
        with self.database.begin() as db:
            with self.assertRaises(PermissionError):
                service.change(db,self.user_id,1,0,action='save',image_url=image,title='虚拟',sort_order=0)

    def test_lost_commit_acknowledgement_retains_the_committed_image(self):
        self.save(); factory=self.database
        class LostAck:
            def __call__(self): return factory()
            @contextmanager
            def begin(self):
                with factory.begin() as db: yield db
                raise OperationalError('synthetic lost acknowledgement',{},Exception())
        with patch.object(main,'SessionLocal',LostAck()):
            self.assertIn('error=',self.save(revision=1,enabled='true').headers['location'])
        self.assertEqual(self.row()['revision'],2)
        self.assertTrue(service.image_target(self.row()['image_url']).is_file())
        self.assertEqual(self.client.get(self.row()['image_url']).status_code,200)

    def test_database_and_storage_failures_do_not_overwrite_existing_settings(self):
        self.save(); first=self.row()
        with patch.object(service,'change',side_effect=OperationalError('synthetic',{},Exception())):
            self.assertIn('error=',self.save(revision=1).headers['location'])
        self.assertEqual(self.row(),first)
        with patch.object(service,'save_image',side_effect=OSError('synthetic disk error')):
            self.assertIn('error=',self.save(revision=1).headers['location'])
        self.assertEqual(self.row(),first)

    def test_html_title_is_escaped_in_editor_and_preview(self):
        self.save(enabled='true',title='<script>alert(1)</script>')
        result=self.client.get('/mall-home?section=carousel')
        self.assertIn('&lt;script&gt;',result.text)
        self.assertNotIn('<script>alert(1)</script>',result.text)


class BannerMemberTests(unittest.TestCase):
    def setUp(self):
        self.case=fixtures.HomeEntryMemberTests('runTest')
        self.addCleanup(self.case.doCleanups);self.case.setUp()
        self.client,self.f=self.case.client,self.case.f
        self.tokens=self.case.case.tokens
        self.enterContext(patch.object(service,'STORAGE_ROOT',self.f.path.parent/'banners'))
        self.urls=[service.save_image(i,fixtures.png(),'virtual.png') for i in (1,2)]
        with self.f.Session.begin() as db:
            for slot in (1,2):
                service.change(db,self.f.operator.id,slot,0,action='save',image_url=self.urls[slot-1],
                               title='虚拟推荐',sort_order=slot,enabled=slot==1,target_category='home')

    def auth(self,index=0): return {'Authorization':'Bearer '+self.tokens[index]}

    def test_manifest_and_original_head_range_require_current_member_qualification(self):
        for index,status in ((0,200),(1,403),(None,401)):
            auth=self.auth(index) if index is not None else {}
            for url in ('/api/miniprogram/v1/home-banners',self.urls[0]):
                result=self.client.get(url,headers=auth)
                self.assertEqual(result.status_code,status)
                self.assertEqual(result.headers['cache-control'],'no-store, private')
            self.assertEqual(self.client.head(self.urls[0],headers=auth).status_code,status)
            self.assertEqual(self.client.get(self.urls[0],headers={**auth,'Range':'bytes=0-9'}).status_code,206 if status==200 else status)
        data=self.client.get('/api/miniprogram/v1/home-banners',headers=self.auth()).json()
        self.assertEqual(data,[dict(slot=1,title='虚拟推荐',image_url=self.urls[0],target_category='home')])
        with self.f.Session.begin() as db: db.query(models.MemberQualification).update({'enabled':False})
        self.assertEqual(self.client.get(self.urls[0],headers=self.auth()).status_code,403)

    def test_disabled_images_hidden_from_members_but_available_to_operators(self):
        self.assertEqual(self.client.get(self.urls[1],headers=self.auth()).status_code,404)
        operator=SimpleNamespace(id=self.f.operator.id,role='admin',admin_level='operator',is_active=True,must_change_password=False)
        with patch.object(main,'get_current_user',return_value=operator):
            self.assertEqual(self.client.get(self.urls[1]).status_code,200)
            self.assertEqual(self.client.get(self.urls[1],headers={'Authorization':'Bearer forged'}).status_code,401)
        with self.f.Session.begin() as db:
            service.change(db,self.f.operator.id,1,1,action='toggle',enabled=False)
        self.assertEqual(self.client.get(self.urls[0],headers=self.auth()).status_code,404)
        self.assertEqual(self.client.get('/api/miniprogram/v1/home-banners',headers=self.auth()).json(),[])

    def test_missing_files_and_unknown_paths_are_private_404(self):
        service.image_target(self.urls[0]).unlink()
        for url in (self.urls[0],'/uploads/mall_banners/1/'+'0'*32+'.webp','/uploads/mall_banners/6/a.webp'):
            self.assertEqual(self.client.get(url,headers=self.auth()).status_code,404)
        for url in ('/uploads/mall_banners/../private.webp','/uploads/mall_banners/1/../../x.webp',self.urls[0]+'?x=1'):
            with self.assertRaises(ValueError): service.image_target(url)

    def test_symlink_escape_is_rejected(self):
        target=service.image_target(self.urls[0]);target.unlink()
        outside=self.f.path.parent/'private-sentinel.webp';outside.write_bytes(b'private sentinel')
        try: target.symlink_to(outside)
        except OSError: self.skipTest('Windows symlink privilege unavailable; covered on Linux')
        result=self.client.get(self.urls[0],headers=self.auth())
        self.assertEqual(result.status_code,404);self.assertNotIn(b'private sentinel',result.content)


class BannerMigrationTests(unittest.TestCase):
    def test_upgrade_matches_models_and_nonempty_downgrade_is_blocked(self):
        with TemporaryDirectory() as directory:
            url='sqlite:///'+(Path(directory)/'virtual.db').as_posix()
            config=Config('alembic.ini');config.attributes['database_url']=url
            command.upgrade(config,'0019_home_entry_images');command.upgrade(config,'head');command.check(config)
            engine=create_engine(url)
            try:
                self.assertIn('home_banners',inspect(engine).get_table_names())
                command.downgrade(config,'0019_home_entry_images');command.upgrade(config,'head')
                with engine.begin() as connection:
                    connection.exec_driver_sql("INSERT INTO home_banners (slot,title,image_path,sort_order,enabled,target_category,revision,updated_by_id,updated_at) VALUES (1,'虚拟','/uploads/mall_banners/1/"+'a'*32+".webp',0,0,'',1,1,'2026-11-12')")
                with self.assertRaises(RuntimeError): command.downgrade(config,'0019_home_entry_images')
                self.assertIn('home_banners',inspect(engine).get_table_names())
            finally: engine.dispose()
