"""Synthetic identities and explicit institution provenance, in a NEW package only."""
from datetime import timedelta
from decimal import Decimal
import json
import secrets
import zipfile

PROFILES = {
    'a1':'A1 哈尔滨会员', 'a2':'A2 广西会员', 'b':'B 客户会员',
    'limited':'隔离样例：A1 70 / B 100',
    'sibling':'隔离样例：A1 70 / A2 100',
    'conflict':'异常样例：两机构资格同时有效',
    'history':'历史样例：A1 老订单 / 当前 B',
}


def seed(root, main):
    from app import models as m
    from app.auth import get_password_hash
    from app.time_utils import utc8_now
    from app.mall import client_organizations as orgs, client_pricing as prices, qualification_service as quals
    from app.mall import record_initial_points_grant, receive_inventory, OrderLineRequest
    from app.mall import execute_order_placement, execute_order_fulfillment, execute_order_shipping, execute_order_completion
    from app.mall.member_address_service import create_member_address
    from app.database import engine
    now=utc8_now().replace(tzinfo=None)
    names={'a':'虚拟总公司A','a1':'虚拟哈尔滨分公司A1','a2':'虚拟广西分公司A2','b':'虚拟客户B'}
    regions={'a':'虚拟总部','a1':'黑龙江省哈尔滨市','a2':'广西壮族自治区','b':'虚拟B地区'}
    public={'profiles':PROFILES,'organizations':{},'members':{}}
    with main.SessionLocal.begin() as db:
        operator=m.User(username='pricing-acceptance-operator',password_hash=get_password_hash(secrets.token_urlsafe(24)),
            role='admin',admin_level='operator',is_active=True,must_change_password=False)
        partners={key:m.User(username='pricing-acceptance-'+key+'-uploader',password_hash=get_password_hash(secrets.token_urlsafe(24)),
            role='partner',is_active=True,must_change_password=False) for key in ('a1','a2','b')}
        db.add_all([operator,*partners.values()]);db.flush();operator_id=operator.id
        organizations={}
        for key in names:
            organizations[key]=orgs.create(db,operator.id,code='UAT-'+key.upper(),name=names[key],region=regions[key],
                parent_id=organizations['a'].id if key in ('a1','a2') else None)
            public['organizations'][key]=organizations[key].id
        for key,partner in partners.items():
            orgs.set_scope(db,operator.id,user_id=partner.id,organization_id=organizations[key].id,revision=0,enabled=True)
        category=m.ProductCategory(name='独立验收商品',slug='client-uat',is_active=True)
        supplier=m.Supplier(supplier_public_id='SUP-CLIENT-UAT',name='虚拟验收受托方',is_active=True)
        db.add_all([category,supplier]);db.flush()
        product=m.Product(product_public_id='PRD-CLIENT-UAT',category_id=category.id,name='虚拟验收礼盒',
            status='PUBLISHED',published_at=now)
        db.add(product);db.flush()
        sku=m.ProductSku(product_id=product.id,supplier_id=supplier.id,sku_code='CLIENT-UAT-100',name='标准款',
            points_price=Decimal('100'),cost_price=Decimal('45'),low_stock_threshold=10,is_active=True)
        db.add(sku);db.flush();sku_id=sku.id
        receive_inventory(db,actor_admin_id=operator.id,sku_id=sku.id,quantity=500,
            reason='独立虚拟验收入库',idempotency_key='client-uat-stock',now=now)
        # Initial rule values use the same prepare/save domain services as the admin UI.
        for key,ratio in [('a','90'),('a2','110')]:
            preview=prices.prepare(db,operator.id,organizations[key].id,'custom',ratio)
            db.flush();prices.save(db,operator.id,preview['preview_token']);db.flush()
        history_qualifications=[]
        for profile,label in PROFILES.items():
            member=m.Member(member_public_id='MEM-UAT-'+profile.upper(),is_active=True)
            db.add(member);db.flush()
            binding=m.MemberWechatBinding(member_id=member.id,wechat_app_id='local-client-pricing-acceptance',openid='uat-'+profile)
            account=m.PointsAccount(member_id=member.id)
            db.add_all([binding,account]);db.flush()
            public['members'][profile]=member.id
            sources=([(profile,'1000',True)] if profile in ('a1','a2','b') else
                [('a1','70',True),('a2' if profile=='sibling' else 'b','100',profile=='conflict')])
            if profile=='history':sources=[('a1','1000',True),('b','200',False)]
            for key,amount,enabled in sources:
                partner=partners[key]
                batch=m.UploadBatch(user_id=partner.id,filename=f'虚拟预置-{profile}-{key}.xlsx',total_rows=1,success_rows=1,failed_rows=0,
                    acceptance_status='已承接',redemption_mode='MALL_REDEMPTION',claim_deadline=now+timedelta(days=300))
                db.add(batch);db.flush();orgs.bind_batch(db,batch,organizations[key],operator.id);db.flush()
                business=m.BusinessRecord(user_id=partner.id,batch_id=batch.id,business_no=f'UAT-{profile}-{key}',
                    public_business_no=f'UAT-{profile}-{key}',name=label,phone='13800000001',plate_number='测A00001',
                    points_amount=Decimal(amount),bank_card='',redemption_mode='MALL_REDEMPTION',claim_status='ACTIVATED')
                db.add(business);db.flush()
                grant=m.PointsGrant(account_id=account.id,business_record_id=business.id,granted_points=Decimal(amount),
                    available_points=0,reserved_points=0,activated_at=now,expires_at=now+timedelta(days=365),status='ACTIVE')
                db.add(grant);db.flush()
                record_initial_points_grant(db,grant=grant,idempotency_key=f'client-uat-grant:{grant.id}',
                    reference_type='BUSINESS_RECORD',reference_id=business.public_business_no,now=now)
                quals.confirm(db,actor_id=operator.id,batch_id=batch.id,business_ids=[business.id],
                    valid_from=now-timedelta(days=1),valid_until=now+timedelta(days=365),reason='独立虚拟验收名单',now=now)
                q=db.query(m.MemberQualification).filter_by(business_record_id=business.id).one()
                if not enabled:
                    quals.update(db,actor_id=operator.id,qualification_id=q.id,version=q.version,action='DISABLE',reason='独立虚拟历史样例',now=now)
                if profile=='history':history_qualifications.append(q.id)
            create_member_address(db,member.id,dict(recipient_name='虚拟收件人',phone='13800000001',
                province='测试省',city='测试市',district='测试区',detail='独立验收路1号（虚拟地址，请勿发货）'))
    # Pre-completed historical order: change current qualification AFTER original posting.
    placed=execute_order_placement(engine,member_id=public['members']['history'],lines=(OrderLineRequest(sku_id,1),),
        idempotency_key='client-uat-history',now=now)
    execute_order_fulfillment(engine,actor_admin_id=operator_id,order_public_id=placed.order_public_id,now=now)
    execute_order_shipping(engine,actor_admin_id=operator_id,order_public_id=placed.order_public_id,
        shipping_carrier='虚拟物流（不发货）',tracking_number='UAT-HISTORY-ONLY',now=now)
    execute_order_completion(engine,actor_admin_id=operator_id,order_public_id=placed.order_public_id,now=now)
    with main.SessionLocal.begin() as db:
        for identity,action in zip(history_qualifications,('DISABLE','ENABLE')):
            q=db.get(m.MemberQualification,identity)
            quals.update(db,actor_id=operator_id,qualification_id=q.id,version=q.version,action=action,reason='独立虚拟年度归属变更',now=now)
    public['history_order']=placed.order_public_id
    (root/'data/fixture-index.json').write_text(json.dumps(public,ensure_ascii=False,indent=2),encoding='utf-8')
    samples(root,names,regions)


def samples(root,names,regions):
    from app.excel_service import REQUIRED_COLUMNS, ORGANIZATION_COLUMNS
    from openpyxl import Workbook
    folder=root/'虚拟名单';folder.mkdir(exist_ok=False)
    def row(key,number):return ['虚拟新增会员'+str(number),'13800000009','测A00009',200,'','UAT-'+key.upper(),names[key],regions[key]]
    good=row('a1',1);blank=row('a1',2);blank[-1]=''
    choices={'01-正确-A1.xlsx':[good],'02-缺填归属地.xlsx':[blank],
             '03-混合A1和A2.xlsx':[good,row('a2',3)],'04-越权A2.xlsx':[row('a2',4)]}
    for name,rows in choices.items():
        book=Workbook();sheet=book.active;sheet.title='会员积分清单';sheet.append(REQUIRED_COLUMNS+ORGANIZATION_COLUMNS)
        for values in rows:sheet.append(values)
        for cells in sheet:
            for cell in cells:cell.number_format='@'
        for column in 'ABCDEFGH':sheet.column_dimensions[column].width=24
        book.save(folder/name);book.close()
    with zipfile.ZipFile(root/'虚拟名单.zip','w',zipfile.ZIP_DEFLATED) as archive:
        for path in folder.iterdir():archive.write(path,'虚拟名单/'+path.name)
