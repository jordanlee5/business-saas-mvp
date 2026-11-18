"""Virtual HTTP uploads, authoritative organizations, and lossless additive migration."""
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from alembic import command
from alembic.config import Config
from openpyxl import Workbook, load_workbook
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import OperationalError
from app import main, models as m
from app.auth import get_password_hash
from app.excel_service import REQUIRED_COLUMNS, ORGANIZATION_COLUMNS, parse_business_excel
from app.mall import client_organizations as service
from app.mall.channel_service import correct_pending_batch_channel
from app.schema_readiness import assert_database_schema_ready
import test_browser_sessions as browser


class ClientOrganizationsTests(unittest.TestCase):
    def setUp(self):
        browser.BrowserSessionTests.setUp(self)
        self.temp=TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.enterContext(patch.object(main,'BUSINESS_EXCEL_DIR',self.temp.name))
        browser.BrowserSessionTests.login(self)
        with self.database.begin() as db:
            self.parent=service.create(db,self.user_id,code='VIRTUAL',name='虚拟保险总公司',region='虚拟总部').id
            self.hrb=service.create(db,self.user_id,code='VIRTUAL-HRB',name='虚拟哈尔滨分公司',region='黑龙江省哈尔滨市',parent_id=self.parent).id
            self.gx=service.create(db,self.user_id,code='VIRTUAL-GX',name='虚拟广西分公司',region='广西壮族自治区',parent_id=self.parent).id
            partner=m.User(username='virtual-partner',password_hash=get_password_hash('virtual-test-only-password'),role='partner',is_active=True,must_change_password=False)
            db.add(partner);db.flush();self.partner=partner.id
            service.set_scope(db,self.user_id,user_id=self.partner,organization_id=self.hrb,revision=0,enabled=True)

    def login_partner(self):
        self.client.get('/logout')
        self.assertEqual(self.client.post('/login',data={'username':'virtual-partner','password':'virtual-test-only-password'}).status_code,302)

    def form(self, **data):
        self.client.get('/mall-clients')
        data.setdefault('csrf_token',self.client.cookies.get('client_organizations_csrf'))
        return self.client.post('/mall-clients',data=data)

    def sheet(self, rows=None, *, headers=None):
        if rows is None:rows=[['虚拟会员','13800000001','测A00001',100,'','VIRTUAL-HRB','虚拟哈尔滨分公司','黑龙江省哈尔滨市']]
        book=Workbook();book.active.append(headers or REQUIRED_COLUMNS+ORGANIZATION_COLUMNS)
        for row in rows:book.active.append(row)
        out=BytesIO();book.save(out);book.close();return out.getvalue()

    def upload(self, data=None, mode='MALL_REDEMPTION'):
        return self.client.post('/upload-excel',data={'redemption_mode':mode,'claim_deadline':'2099-12-31' if mode=='MALL_REDEMPTION' else ''},
            files={'file':('virtual.xlsx',data or self.sheet(),'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')})

    def counts(self):
        with self.database() as db:return tuple(db.query(model).count() for model in (m.UploadBatch,m.BusinessRecord,m.ClientBatchOwnership,m.Notification))

    def test_operator_page_create_root_branch_and_parent_validation(self):
        response=self.client.get('/mall-clients');self.assertEqual(response.status_code,200)
        self.assertEqual(response.headers['cache-control'],'no-store, private')
        self.assertIn('客户与分支机构',response.text)
        result=self.form(action='create',code='NEW',name='虚拟新总公司',region='测试地')
        self.assertNotIn('error=',result.headers['location'])
        for values in (dict(code='NEW',name='重复',region='测试'),dict(code='ABC',name='三级',region='测试',parent_id=self.hrb),dict(code='BAD',name=' ',region='测试')):
            self.assertIn('error=',self.form(action='create',**values).headers['location'])
        with self.database() as db:self.assertEqual(db.query(m.ClientOrganization).count(),4)

    def test_scope_and_toggle_reject_stale_versions(self):
        response=self.form(action='toggle',organization_id=self.hrb,revision=1,enabled='false')
        self.assertNotIn('error=',response.headers['location'])
        self.assertIn('error=',self.form(action='toggle',organization_id=self.hrb,revision=1,enabled='true').headers['location'])
        self.assertNotIn('error=',self.form(action='scope',organization_id=self.hrb,user_id=self.partner,revision=1,enabled='false').headers['location'])
        self.assertIn('error=',self.form(action='scope',organization_id=self.hrb,user_id=self.partner,revision=1,enabled='true').headers['location'])
        with self.database() as db:
            self.assertFalse(db.get(m.ClientOrganization,self.hrb).is_active)
            self.assertFalse(db.get(m.ClientUploadScope,(self.partner,self.hrb)).is_active)

    def test_partner_cannot_manage_or_download_other_branch(self):
        self.login_partner()
        self.assertEqual(self.client.get('/mall-clients').status_code,403)
        self.assertEqual(self.client.post('/mall-clients',data={'action':'create'}).status_code,403)
        self.assertEqual(self.client.get(f'/upload-excel/mall-template/{self.gx}').status_code,404)
        page=self.client.get('/upload-excel');self.assertIn('VIRTUAL-HRB',page.text);self.assertNotIn('VIRTUAL-GX',page.text)

    def test_csrf_and_reviewer_cannot_write(self):
        self.assertEqual(self.form(action='toggle',organization_id=self.hrb,revision=1,csrf_token='bad').status_code,403)
        with self.database.begin() as db:db.get(m.User,self.user_id).admin_level='primary_reviewer'
        self.assertEqual(self.client.get('/mall-clients').status_code,403)
        self.assertEqual(self.client.get(f'/upload-excel/mall-template/{self.hrb}').status_code,404)

    def test_template_download_fill_upload_and_visible_snapshot(self):
        self.login_partner()
        response=self.client.get(f'/upload-excel/mall-template/{self.hrb}')
        self.assertEqual(response.status_code,200);self.assertEqual(response.headers['cache-control'],'no-store, private')
        book=load_workbook(BytesIO(response.content));sheet=book.active
        self.assertEqual([c.value for c in sheet[1]],REQUIRED_COLUMNS+ORGANIZATION_COLUMNS)
        self.assertEqual(sheet['F2'].value,'VIRTUAL-HRB')
        for cell,value in zip(('A2','B2','C2','D2'),('虚拟会员','13800000001','测A00001',100)):sheet[cell]=value
        output=BytesIO();book.save(output);book.close()
        self.assertEqual(self.upload(output.getvalue()).status_code,200)
        with self.database() as db:
            owned=db.query(m.ClientBatchOwnership).one()
            self.assertEqual((owned.organization_id,owned.organization_code,owned.organization_region),(self.hrb,'VIRTUAL-HRB','黑龙江省哈尔滨市'))
            self.assertEqual(owned.parent_code,'VIRTUAL')
            self.assertEqual(db.query(m.BusinessRecord).one().batch_id,owned.batch_id)
            self.assertEqual(db.query(m.PointsGrant).count(),0)
        history=self.client.get('/upload-batches')
        self.assertIn('VIRTUAL-HRB',history.text)
        self.assertNotIn('VIRTUAL-GX',history.text)

    def test_missing_branch_column_or_blank_row_rejects_entire_sheet(self):
        self.login_partner()
        invalid=[self.sheet([['虚拟会员','13800000001','测A00001',100,'']],headers=REQUIRED_COLUMNS),
            self.sheet([['虚拟会员','13800000001','测A00001',100,'','VIRTUAL-HRB','','黑龙江省哈尔滨市']])]
        before=self.counts()
        for payload in invalid:
            self.assertEqual(self.upload(payload).status_code,400);self.assertEqual(self.counts(),before)
        self.assertEqual(list(Path(self.temp.name).iterdir()),[])

    def test_other_branch_and_forged_name_or_region_rejected(self):
        self.login_partner();before=self.counts()
        for code,name,region in [('VIRTUAL-GX','虚拟广西分公司','广西壮族自治区'),('VIRTUAL-HRB','伪造名称','黑龙江省哈尔滨市'),('VIRTUAL-HRB','虚拟哈尔滨分公司','广西壮族自治区')]:
            self.assertEqual(self.upload(self.sheet([['虚拟会员','13800000001','测A00001',100,'',code,name,region]])).status_code,400)
            self.assertEqual(self.counts(),before)

    def test_mixed_authorized_branches_still_require_separate_files(self):
        with self.database.begin() as db:service.set_scope(db,self.user_id,user_id=self.partner,organization_id=self.gx,revision=0,enabled=True)
        self.login_partner()
        payload=self.sheet([['虚拟甲','13800000001','测A00001',100,'','VIRTUAL-HRB','虚拟哈尔滨分公司','黑龙江省哈尔滨市'],
            ['虚拟乙','13800000002','测A00002',100,'','VIRTUAL-GX','虚拟广西分公司','广西壮族自治区']])
        self.assertEqual(self.upload(payload).status_code,400)
        self.assertEqual(self.counts()[:3],(0,0,0))

    def test_revoked_scope_and_disabled_parent_checked_at_upload(self):
        self.login_partner();payload=self.sheet()
        with self.database.begin() as db:service.toggle(db,self.user_id,self.parent,1,False)
        self.assertEqual(self.upload(payload).status_code,400)
        self.assertEqual(self.client.get(f'/upload-excel/mall-template/{self.hrb}').status_code,404)
        with self.database.begin() as db:
            service.toggle(db,self.user_id,self.parent,2,True)
            service.set_scope(db,self.user_id,user_id=self.partner,organization_id=self.hrb,revision=1,enabled=False)
        self.assertEqual(self.upload(payload).status_code,400)
        self.assertEqual(self.counts()[:3],(0,0,0))

    def test_authorization_change_preserves_existing_ownership(self):
        self.login_partner();self.assertEqual(self.upload().status_code,200)
        with self.database() as db:
            old=db.query(m.ClientBatchOwnership).one()
            before=(old.batch_id,old.organization_id,old.organization_code,old.parent_code)
        with self.database.begin() as db:
            service.set_scope(db,self.user_id,user_id=self.partner,organization_id=self.hrb,revision=1,enabled=False)
            service.set_scope(db,self.user_id,user_id=self.partner,organization_id=self.gx,revision=0,enabled=True)
        with self.database() as db:
            old=db.query(m.ClientBatchOwnership).one()
            self.assertEqual((old.batch_id,old.organization_id,old.organization_code,old.parent_code),before)
            with self.assertRaises(ValueError):service.bind_batch(db,db.get(m.UploadBatch,old.batch_id),db.get(m.ClientOrganization,self.gx),self.partner)

    def test_failed_transaction_does_not_leave_partial_batch_or_ownership(self):
        self.login_partner()
        with patch.object(main,'create_business_batch_uploaded_notifications',side_effect=OperationalError('synthetic',{},Exception())):
            with self.assertRaises(OperationalError):self.upload()
        self.assertEqual(self.counts()[:3],(0,0,0));self.assertEqual(list(Path(self.temp.name).iterdir()),[])

    def test_cash_legacy_template_works_but_cannot_bypass_mall_ownership(self):
        self.login_partner()
        data=self.sheet([['虚拟会员','13800000001','测A00001',100,'6222000000000000']],headers=REQUIRED_COLUMNS)
        self.assertEqual(self.upload(data,mode='CASH_REBATE').status_code,200)
        with self.database() as db:
            batch=db.query(m.UploadBatch).one()
            with self.assertRaisesRegex(ValueError,'机构归属'):
                correct_pending_batch_channel(db,batch=batch,redemption_mode='MALL_REDEMPTION',claim_deadline_text='2099-12-31')
            self.assertEqual(batch.redemption_mode,'CASH_REBATE')
            self.assertEqual(db.query(m.ClientBatchOwnership).count(),0)

    def test_duplicate_required_column_rejected(self):
        data=self.sheet(headers=REQUIRED_COLUMNS+ORGANIZATION_COLUMNS+['机构编号'])
        records,errors=parse_business_excel(BytesIO(data),bank_card_required=False,organization_required=True)
        self.assertEqual(records,[]);self.assertIn('重复字段',errors[0])

    def test_identical_filename_uploads_do_not_overwrite_prior_file(self):
        self.login_partner();self.assertEqual(self.upload().status_code,200);self.assertEqual(self.upload().status_code,200)
        self.assertEqual(len(list(Path(self.temp.name).iterdir())),2)


class ClientOrganizationMigrationTests(unittest.TestCase):
    def test_additive_upgrade_preserves_old_rows_and_refuses_nonempty_downgrade(self):
        with TemporaryDirectory() as folder:
            url='sqlite:///'+(Path(folder)/'virtual.db').as_posix()
            config=Config('alembic.ini');config.attributes['database_url']=url
            command.upgrade(config,'0022_company_banners')
            engine=create_engine(url)
            try:
                with engine.begin() as c:
                    c.execute(text("INSERT INTO users (id,username,role,password_hash) VALUES (1,'virtual','admin','test-only')"))
                    c.execute(text("INSERT INTO upload_batches (id,user_id,filename,total_rows,success_rows,failed_rows,acceptance_status,redemption_mode) VALUES (1,1,'virtual.xlsx',1,1,0,'待承接','MALL_REDEMPTION')"))
                with engine.connect() as c:
                    old={t:c.execute(text('SELECT * FROM '+t)).fetchall() for t in inspect(engine).get_table_names() if t!='alembic_version'}
                command.upgrade(config,'head');command.check(config);assert_database_schema_ready(engine)
                with engine.connect() as c:
                    for table,rows in old.items():self.assertEqual(c.execute(text('SELECT * FROM '+table)).fetchall(),rows)
                    self.assertEqual(c.execute(text('SELECT COUNT(*) FROM client_batch_ownerships')).scalar(),0)
                command.downgrade(config,'0022_company_banners');command.upgrade(config,'head')
                with engine.begin() as c:
                    c.execute(text("INSERT INTO client_organizations (id,code,name,region,is_active,revision,updated_by_id,created_at,updated_at) VALUES (1,'VIRTUAL','虚拟机构','虚拟地区',1,1,1,'2026-11-18','2026-11-18')"))
                with self.assertRaisesRegex(RuntimeError,'拒绝'):command.downgrade(config,'0022_company_banners')
                self.assertIn('client_organizations',inspect(engine).get_table_names())
            finally:engine.dispose()


if __name__=='__main__':unittest.main()
