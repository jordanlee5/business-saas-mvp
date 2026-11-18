"""Backoffice organization management and scoped member-list templates."""
from io import BytesIO
import secrets
from urllib.parse import urlencode
from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from .. import models as m
from ..mall import client_organizations as service
from ..excel_service import REQUIRED_COLUMNS, ORGANIZATION_COLUMNS

router=APIRouter()
COOKIE='client_organizations_csrf'


def actor(request, *, manage=False):
    from ..main import get_current_user
    user=get_current_user(request)
    if not user:raise HTTPException(401,'请先登录')
    if not user.is_active or user.must_change_password:raise HTTPException(403,'账号不可用')
    if manage and not service.manager(user):raise HTTPException(403,'无客户机构管理权限')
    return user


def csrf(request, token):
    cookie=request.cookies.get(COOKIE,'')
    if len(cookie)!=43 or not secrets.compare_digest(cookie.encode(),token.encode()):
        raise HTTPException(403,'表单已失效，请刷新后重试')


@router.get('/mall-clients')
def page(request: Request, message: str='', error: str=''):
    from ..main import SessionLocal, templates, add_base_context
    actor(request,manage=True)
    token=request.cookies.get(COOKIE,'')
    if len(token)!=43:token=secrets.token_urlsafe(32)
    with SessionLocal() as db:
        organizations=db.query(m.ClientOrganization).order_by(m.ClientOrganization.code).all()
        partners=db.query(m.User).filter_by(role='partner').order_by(m.User.username).all()
        scopes=db.query(m.ClientUploadScope).order_by(m.ClientUploadScope.user_id,m.ClientUploadScope.organization_id).all()
    response=templates.TemplateResponse(request=request,name='client_organizations.html',
        context=add_base_context(request,dict(request=request,active_page='mall_clients',page_title='客户与分支机构',
            organizations=organizations,partners=partners,scopes=scopes,csrf=token,message=message[:200],error=error[:200])))
    response.headers['Cache-Control']='no-store, private'
    response.set_cookie(COOKIE,token,httponly=True,samesite='strict',secure=request.url.scheme=='https',path='/mall-clients')
    return response


@router.post('/mall-clients')
def change(request: Request, action: str=Form(...), csrf_token: str=Form(''),
    organization_id: int=Form(0), user_id: int=Form(0), revision: int=Form(0),
    code: str=Form(''), name: str=Form(''), region: str=Form(''), parent_id: int=Form(0), enabled: bool=Form(False)):
    from ..main import SessionLocal
    user=actor(request,manage=True);csrf(request,csrf_token)
    if action not in ('create','toggle','scope'):raise HTTPException(400,'不支持的操作')
    try:
        with SessionLocal.begin() as db:
            if action=='create':service.create(db,user.id,code=code,name=name,region=region,parent_id=parent_id or None)
            elif action=='toggle':service.toggle(db,user.id,organization_id,revision,enabled)
            else:service.set_scope(db,user.id,user_id=user_id,organization_id=organization_id,revision=revision,enabled=enabled)
    except (ValueError,PermissionError,SQLAlchemyError) as exc:
        error=('编号或授权已存在，请刷新核对' if isinstance(exc,IntegrityError) else
               '保存结果未确认，请刷新核对后再操作' if isinstance(exc,SQLAlchemyError) else str(exc))
        return RedirectResponse('/mall-clients?'+urlencode(dict(error=error)),status_code=303)
    return RedirectResponse('/mall-clients?'+urlencode(dict(message='已保存；历史清单归属保持原记录')),status_code=303)


@router.get('/upload-excel/mall-template/{organization_id}')
def template(request: Request, organization_id: int):
    from ..main import SessionLocal
    user=actor(request)
    with SessionLocal() as db:
        current=db.get(m.User,user.id)
        org=next((row for row in service.available(db,current) if row.id==organization_id),None)
        if not org:raise HTTPException(404,'没有可下载的机构模板')
        code,name,region=org.code,org.name,org.region
    book=Workbook();sheet=book.active;sheet.title='会员积分清单'
    sheet.append(REQUIRED_COLUMNS+ORGANIZATION_COLUMNS)
    sheet.append(['','','','','',code,name,region])
    for row in sheet:
        for cell in row:
            cell.number_format='@'
            if isinstance(cell.value,str):cell.data_type='s'
    for cell in sheet[1]:
        cell.font=Font(bold=True,color='FFFFFF');cell.fill=PatternFill('solid',fgColor='164E46')
    for key,width in {'A':16,'B':20,'C':16,'D':16,'E':24,'F':22,'G':42,'H':26}.items():
        sheet.column_dimensions[key].width=width
    sheet.freeze_panes='A2'
    notes=book.create_sheet('填写说明')
    for line in ('每份清单只属于一个机构，不同分支分别上传。',
        '每行必须填写机构编号、分支机构名称和分支归属地，并与本模板登记一致。',
        '新增行时复制机构三列；不要合并单元格或更改机构信息。',
        '姓名、手机号、车牌号、积分金额为必填；商城清单银行卡号可留空。',
        '任何一行不合格均整份拒绝，不会只导入部分会员。'):
        notes.append([line])
    notes.column_dimensions['A'].width=90
    output=BytesIO();book.save(output);book.close()
    return Response(output.getvalue(),media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        headers={'Content-Disposition':f'attachment; filename="mall-members-{code}.xlsx"','Cache-Control':'no-store, private'})
