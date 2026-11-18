"""Client identity and immutable upload ownership; pricing is a later phase."""
import re
from sqlalchemy.orm import aliased
from .. import models as m
from ..admin_permissions import can_manage_partners
from ..time_utils import utc8_now


def manager(user):
    return bool(user and getattr(user,'is_active',False) and not getattr(user,'must_change_password',True) and can_manage_partners(user))


def require_manager(db, actor_id):
    actor = db.query(m.User).filter_by(id=actor_id).populate_existing().with_for_update().one_or_none()
    if not manager(actor):
        raise PermissionError('仅启用的运营管理员或超级管理员可管理客户机构')
    return actor


def audit(db, actor_id, action, target_id, description):
    db.add(m.AdminActionLog(admin_id=actor_id, action_type=action,
        target_type='client_organization', target_id=target_id, description=description))


def clean(value, limit, label):
    value = value.strip() if isinstance(value, str) else ''
    if not value or len(value) > limit or any(ord(c) < 32 for c in value):
        raise ValueError(f'{label}须填写1至{limit}个字符，不能含换行或控制字符')
    return value


def create(db, actor_id, *, code, name, region, parent_id=None):
    require_manager(db, actor_id)
    code = clean(code, 32, '机构编号').upper()
    if not re.fullmatch(r'[A-Z][A-Z0-9_-]{1,31}', code):
        raise ValueError('机构编号须为2至32位大写字母、数字、横线或下划线，且以字母开头')
    name, region = clean(name,120,'机构全称'), clean(region,80,'机构归属地')
    if db.query(m.ClientOrganization.id).filter_by(code=code).first():
        raise ValueError('机构编号已存在，请使用既有机构')
    if parent_id is not None:
        parent = db.query(m.ClientOrganization).filter_by(id=parent_id).with_for_update().one_or_none()
        if not parent or parent.parent_id is not None or not parent.is_active:
            raise ValueError('请选择启用中的总公司；当前只支持总公司与分支两级')
    row = m.ClientOrganization(code=code,name=name,region=region,parent_id=parent_id,
        is_active=True,revision=1,updated_by_id=actor_id,created_at=utc8_now(),updated_at=utc8_now())
    db.add(row);db.flush()
    audit(db,actor_id,'client_organization_create',row.id,f'创建机构 {code}；上级机构ID {parent_id or 0}')
    return row


def toggle(db, actor_id, organization_id, revision, enabled):
    require_manager(db,actor_id)
    if revision < 1:raise ValueError('机构版本无效，请刷新')
    count = db.query(m.ClientOrganization).filter_by(id=organization_id,revision=revision).update(
        dict(is_active=bool(enabled),revision=revision+1,updated_by_id=actor_id,updated_at=utc8_now()),
        synchronize_session=False)
    if count != 1:raise ValueError('机构已被更新或不存在，请刷新后重试')
    audit(db,actor_id,'client_organization_toggle',organization_id,'启用机构' if enabled else '停用机构')


def set_scope(db, actor_id, *, user_id, organization_id, revision, enabled):
    require_manager(db,actor_id)
    user = db.query(m.User).filter_by(id=user_id,role='partner').with_for_update().one_or_none()
    org = db.get(m.ClientOrganization,organization_id)
    if not user or not org:raise ValueError('上传账号或机构不存在')
    if enabled and (not user.is_active or not org.is_active or
        (org.parent_id and not db.get(m.ClientOrganization,org.parent_id).is_active)):
        raise ValueError('授权前请启用账号、机构及其总公司')
    row = db.query(m.ClientUploadScope).filter_by(user_id=user_id,organization_id=organization_id).with_for_update().one_or_none()
    if row is None:
        if revision != 0 or not enabled:raise ValueError('授权已变化，请刷新')
        row = m.ClientUploadScope(user_id=user_id,organization_id=organization_id,is_active=True,
            revision=1,updated_by_id=actor_id,updated_at=utc8_now())
        db.add(row)
    else:
        count = db.query(m.ClientUploadScope).filter_by(user_id=user_id,organization_id=organization_id,
            revision=revision).update(dict(is_active=bool(enabled),revision=revision+1,
            updated_by_id=actor_id,updated_at=utc8_now()),synchronize_session=False)
        if count != 1:raise ValueError('授权已变化，请刷新')
    audit(db,actor_id,'client_upload_scope_change',organization_id,
        f'上传账号ID {user_id}；' + ('授权' if enabled else '撤销授权'))


def available(db, user):
    if not user or not user.is_active or user.must_change_password:return []
    parent = aliased(m.ClientOrganization)
    query = db.query(m.ClientOrganization).outerjoin(parent,parent.id==m.ClientOrganization.parent_id).filter(
        m.ClientOrganization.is_active.is_(True),
        (m.ClientOrganization.parent_id.is_(None)) | parent.is_active.is_(True))
    if not manager(user):
        if user.role != 'partner':return []
        query = query.join(m.ClientUploadScope,m.ClientUploadScope.organization_id==m.ClientOrganization.id).filter(
            m.ClientUploadScope.user_id==user.id,m.ClientUploadScope.is_active.is_(True))
    return query.order_by(m.ClientOrganization.code).all()


def validate_upload(db, user_id, records):
    """Recheck authority in the transaction that creates the batch, before writes."""
    user = db.query(m.User).filter_by(id=user_id).populate_existing().with_for_update().one_or_none()
    if not user or not user.is_active or user.must_change_password:
        raise PermissionError('上传账号不可用，请重新登录')
    if not records:raise ValueError('清单没有会员记录')
    identities = {(r.get('organization_code'),r.get('organization_name'),r.get('organization_region')) for r in records}
    if len(identities)!=1 or any(not v for v in next(iter(identities))):
        raise ValueError('每份商城清单必须完整填写同一个机构；不同分支请分别上传')
    code,name,region = next(iter(identities))
    org = db.query(m.ClientOrganization).filter_by(code=code).one_or_none()
    if not org:raise ValueError('机构信息未获授权或与登记不符，请下载本账号的机构模板')
    # Stable parent identity; lock parent then child to coordinate with stop/enable.
    if org.parent_id:
        parent = db.query(m.ClientOrganization).filter_by(id=org.parent_id).populate_existing().with_for_update().one()
        if not parent.is_active:raise ValueError('所属总公司已停用，请联系运营')
    org = db.query(m.ClientOrganization).filter_by(id=org.id).populate_existing().with_for_update().one()
    if not org.is_active or org.name != name or org.region != region:
        raise ValueError('机构信息未获授权或与登记不符，请下载本账号的机构模板')
    if not manager(user):
        scope = db.query(m.ClientUploadScope).filter_by(user_id=user.id,organization_id=org.id,
            is_active=True).populate_existing().with_for_update().one_or_none()
        if user.role != 'partner' or scope is None:raise PermissionError('该账号未获此机构的清单上传授权')
    return org


def bind_batch(db, batch, org, actor_id):
    """Called only for a new validated upload; never reassign existing history."""
    if db.get(m.ClientBatchOwnership,batch.id):raise ValueError('已有清单归属不能覆盖')
    parent = db.get(m.ClientOrganization,org.parent_id) if org.parent_id else None
    db.add(m.ClientBatchOwnership(batch_id=batch.id,organization_id=org.id,
        organization_code=org.code,organization_name=org.name,organization_region=org.region,
        parent_code=parent.code if parent else None,parent_name=parent.name if parent else None,
        uploaded_by_id=actor_id,created_at=utc8_now()))


def require_batch_for_mall(db, batch):
    ownership = db.get(m.ClientBatchOwnership,batch.id)
    if ownership is None:
        raise ValueError('该清单没有已核验的机构归属，请按商城机构模板重新上传；原清单保持不变')
    validate_upload(db,batch.user_id,[dict(organization_code=ownership.organization_code,
        organization_name=ownership.organization_name,organization_region=ownership.organization_region)])
