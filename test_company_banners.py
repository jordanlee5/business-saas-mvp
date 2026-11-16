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
from app.mall import company_banners as service
from app.mall.audit import MallAuditActionType
import test_home_entry_images as fixtures


class CompanyBannerTests(unittest.TestCase):
    def setUp(self):
        self.case = fixtures.HomeEntryAdminTests('runTest')
        self.addCleanup(self.case.doCleanups)
        self.case.setUp()
        self.client, self.database, self.csrf = self.case.client, self.case.database, self.case.csrf
        self.client.get("/company-materials")
        self.csrf = self.client.cookies.get("company_banner_csrf")
        self.user_id = self.case.user_id
        self.root = Path(self.case.temp.name) / 'banners'
        self.enterContext(patch.object(service, 'STORAGE_ROOT', self.root))

    def save(self, slot=1, revision=0, upload=True, **values):
        data = dict(action='save', revision=revision, title=f'虚拟素材{slot}', sort_order=slot*10,
                    enabled='false', show_copy='true', csrf_token=self.csrf)
        data.update(values)
        files = {'image_file':('virtual.png', fixtures.png(size=(2400,960)), 'image/png')} if upload else None
        return self.client.post(f'/company-materials/banners/{slot}', data=data, files=files)

    def row(self, slot=1):
        with self.database() as db:
            return service.snapshot(db)[slot-1]

    def toggle(self, slot=1, revision=1, enabled=True):
        return self.save(slot, revision, upload=False, action='toggle', enabled=str(enabled).lower())

    def test_empty_editor_and_upload_default_disabled(self):
        page = self.client.get('/company-materials')
        self.assertEqual(page.status_code, 200)
        self.assertEqual(page.text.count('class="banner-card"'), 3)
        self.assertIn('原三张品牌插画', page.text)
        self.assertEqual(self.save().status_code,303)
        row = self.row()
        self.assertFalse(row['enabled'])
        with self.database() as db:
            self.assertEqual(service.published(db), [])
            self.assertEqual(db.query(models.AdminActionLog).filter_by(action_type=MallAuditActionType.COMPANY_BANNER_UPDATE.value).count(),1)
        result = self.client.get(row['image_url'])
        self.assertEqual(result.status_code,200)
        with Image.open(BytesIO(result.content)) as image:
            self.assertEqual(image.format,'WEBP')
            self.assertEqual(image.size,(1920,768))
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
            self.save(slot,sort_order=order,enabled='true',show_copy='true')
        with self.database() as db:
            self.assertEqual([x['slot'] for x in service.published(db)],[2,3,1])
        old_url = self.row()['image_url']
        self.save(revision=1,upload=False,sort_order=0,title='换季推荐',enabled='true',show_copy='false')
        self.assertEqual(self.row()['image_url'],old_url)
        with self.database() as db:
            self.assertEqual([x['slot'] for x in service.published(db)],[1,2,3])
        self.toggle(revision=2,enabled=False)
        self.assertFalse(self.row()['enabled'])
        self.assertEqual(self.row()['title'],'换季推荐')
        self.assertFalse(self.row()['show_copy'])
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
        for values in ({'sort_order':-1},{'sort_order':1000},{'title':''},{'title':'x'*81}):
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
            response=self.client.post('/company-materials/banners/1',data=data,files={'image_file':(filename,content,'image/png')})
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
                self.assertEqual(self.client.get('/company-materials').status_code,403)
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
        result=self.client.get('/company-materials')
        self.assertIn('&lt;script&gt;',result.text)
        self.assertNotIn('<script>alert(1)</script>',result.text)



    def test_public_publication_replacement_disable_and_stale_identity(self):
        from fastapi.testclient import TestClient
        anonymous = TestClient(main.app, base_url='https://testserver', follow_redirects=False)
        self.addCleanup(anonymous.close)
        self.save(enabled='false'); url=self.row()['image_url']
        self.assertEqual(anonymous.get(url).status_code,404)
        self.assertNotIn(url,anonymous.get('/company').text)
        self.toggle(revision=1,enabled=True)
        for response in (anonymous.get(url),anonymous.head(url),anonymous.get(url,headers={'Range':'bytes=0-9'})):
            self.assertIn(response.status_code,(200,206))
            self.assertEqual(response.headers['cache-control'],'no-store')
        response=anonymous.get('/company',cookies={'user_id':'999','saas_admin_session':'invalid'})
        self.assertIn(url,response.text)
        self.assertNotIn('data-pause',response.text)
        self.assertNotIn('暂停轮播',response.text)
        self.save(revision=2,enabled='true');new=self.row()['image_url']
        self.assertEqual(anonymous.get(url).status_code,404)
        self.assertEqual(anonymous.get(new).status_code,200)
        self.toggle(revision=3,enabled=False)
        self.assertEqual(anonymous.get(new).status_code,404)
        self.assertIn('/static/company/city.svg',anonymous.get('/').text)

    def test_public_order_poster_mode_escaping_and_missing_file_fallback(self):
        self.save(1,sort_order=20,enabled='true',show_copy='false',title='<img onerror=alert(1)>')
        self.save(2,sort_order=5,enabled='true')
        page=self.client.get('/company').text
        self.assertLess(page.index(self.row(2)['image_url']),page.index(self.row(1)['image_url']))
        self.assertIn('slide image-only',page)
        self.assertIn('&lt;img onerror=alert(1)&gt;',page)
        self.assertNotIn('<img onerror=alert(1)>',page)
        self.assertEqual(page.count('<h1 '),1)
        for slot in (1,2):service.image_target(self.row(slot)['image_url']).unlink()
        self.assertIn('/static/company/city.svg',self.client.get('/company').text)

    def test_public_page_only_selects_company_artwork(self):
        from sqlalchemy import event
        engine=self.database.kw['bind'];queries=[]
        def capture(conn,cursor,statement,parameters,context,many):
            if statement.lstrip().lower().startswith('select'):queries.append(statement.lower())
        event.listen(engine,'before_cursor_execute',capture)
        try:
            with patch.object(main,'get_current_user',side_effect=AssertionError('No visitor identity read')):
                self.assertEqual(self.client.get('/company').status_code,200)
        finally:event.remove(engine,'before_cursor_execute',capture)
        self.assertTrue(queries)
        self.assertTrue(all('from company_banners' in q for q in queries))


class CompanyMigrationTests(unittest.TestCase):
    def test_upgrade_model_match_and_nonempty_downgrade_refused(self):
        from sqlalchemy import text
        from sqlalchemy.orm import sessionmaker
        from app.schema_readiness import assert_database_schema_ready
        from app.time_utils import utc8_now
        with TemporaryDirectory() as folder:
            url='sqlite:///'+(Path(folder)/'virtual.db').as_posix()
            config=Config('alembic.ini');config.attributes['database_url']=url
            command.upgrade(config,'0021_admin_phone_login')
            engine=create_engine(url)
            try:
                with engine.connect() as c:
                    before={t:c.execute(text('SELECT COUNT(*) FROM '+t)).scalar() for t in inspect(engine).get_table_names() if t!='alembic_version'}
                command.upgrade(config,'head');command.check(config);assert_database_schema_ready(engine)
                with engine.connect() as c:
                    self.assertEqual(before,{t:c.execute(text('SELECT COUNT(*) FROM '+t)).scalar() for t in before})
                    self.assertEqual(c.execute(text('SELECT COUNT(*) FROM company_banners')).scalar(),0)
                command.downgrade(config,'0021_admin_phone_login');command.upgrade(config,'head')
                with sessionmaker(bind=engine).begin() as db:
                    user=models.User(username='virtual',password_hash='synthetic-only',role='admin',admin_level='operator',is_active=True)
                    db.add(user);db.flush()
                    db.add(models.CompanyBanner(slot=1,title='虚拟',image_path='/uploads/company_banners/1/'+'a'*32+'.webp',
                        show_copy=True,enabled=False,sort_order=10,revision=1,updated_by_id=user.id,updated_at=utc8_now()))
                with self.assertRaises(RuntimeError):command.downgrade(config,'0021_admin_phone_login')
                assert_database_schema_ready(engine)
                with engine.connect() as c:self.assertEqual(c.execute(text('SELECT COUNT(*) FROM company_banners')).scalar(),1)
            finally:engine.dispose()
