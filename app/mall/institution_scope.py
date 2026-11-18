"""Authoritative institution identity and separately derived points balances.

The existing account total remains an internal ledger checksum, never a spending
pool. Each immutable grant scope partitions its grant and every ledger entry.
"""
from dataclasses import dataclass
from decimal import Decimal
import hashlib
import json
from sqlalchemy import select
from .. import models as m
from ..time_utils import utc8_now
from .qualification_service import db_time
from .client_pricing import BASE_RATIO_BPS, price_for_member


class ScopeError(ValueError):
    pass


@dataclass(frozen=True)
class InstitutionScope:
    organization_id: int
    code: str
    name: str
    ratio_bps: int
    rule_organization_id: int | None
    rule_revision: int
    context: str


def active_organization(db, organization_id, *, lock=False):
    org = db.get(m.ClientOrganization, organization_id)
    if org is None:
        raise ScopeError('机构归属待核实，请联系服务方')
    ids = [org.parent_id, org.id] if org.parent_id else [org.id]
    rows = []
    for identity in ids:
        query = db.query(m.ClientOrganization).filter_by(id=identity).populate_existing()
        row = (query.with_for_update() if lock else query).one()
        if not row.is_active:
            raise ScopeError('客户机构已停用，请联系服务方')
        rows.append(row)
    return rows[-1], rows[0]


def batch_organization(db, business, *, lock=False):
    ownership = db.get(m.ClientBatchOwnership, business.batch_id) if business.batch_id else None
    if ownership is None:
        raise ScopeError('名单机构归属待核实，请联系服务方')
    org, parent = active_organization(db, ownership.organization_id, lock=lock)
    if org.code != ownership.organization_code:
        raise ScopeError('名单机构归属不一致')
    return org


def bind_grant(db, grant, *, required=False):
    """Freeze only an explicitly verified batch's organization; no guessing."""
    existing = db.get(m.InstitutionGrant, grant.id)
    if existing:
        return existing
    business = db.get(m.BusinessRecord, grant.business_record_id)
    owner = db.get(m.ClientBatchOwnership, business.batch_id) if business and business.batch_id else None
    if owner is None:
        if required:
            raise ScopeError('积分来源机构待核实')
        return None  # Legacy accounting remains intact but cannot fund new orders.
    row = m.InstitutionGrant(grant_id=grant.id, organization_id=owner.organization_id,
        organization_code=owner.organization_code, organization_name=owner.organization_name,
        created_at=db_time(db, utc8_now()))
    db.add(row)
    db.flush()
    return row


def resolve(db, member_id, *, now=None, lock=False):
    current = db_time(db, now or utc8_now())
    member_query = db.query(m.Member).filter_by(id=member_id).populate_existing()
    member = (member_query.with_for_update() if lock else member_query).one_or_none()
    if member is None or not member.is_active:
        raise ScopeError('会员身份不可用')
    query = db.query(m.MemberQualification).filter(
        m.MemberQualification.member_id == member_id, m.MemberQualification.enabled.is_(True),
        m.MemberQualification.valid_from <= current, m.MemberQualification.valid_until > current
    ).order_by(m.MemberQualification.id).populate_existing()
    qualifications = (query.with_for_update() if lock else query).all()
    if not qualifications:
        raise ScopeError('暂无有效商城资格，请联系服务方；本人历史订单仍可查询')
    identities = set()
    for q in qualifications:
        row = (db.query(m.ClientBatchOwnership, m.InstitutionGrant)
            .join(m.BusinessRecord, m.BusinessRecord.batch_id == m.ClientBatchOwnership.batch_id)
            .join(m.PointsGrant, m.PointsGrant.business_record_id == m.BusinessRecord.id)
            .join(m.PointsAccount, m.PointsAccount.id == m.PointsGrant.account_id)
            .join(m.InstitutionGrant, m.InstitutionGrant.grant_id == m.PointsGrant.id)
            .filter(m.BusinessRecord.id == q.business_record_id, m.PointsAccount.member_id == member_id).one_or_none())
        if row is None or row[0].organization_id != row[1].organization_id:
            raise ScopeError('会员名单或积分机构归属待核实，请联系服务方')
        identities.add(row[0].organization_id)
    if len(identities) != 1:
        raise ScopeError('会员存在多个机构的有效资格，请联系服务方核实；积分不能合并使用')
    org, parent = active_organization(db, identities.pop(), lock=lock)
    rules = {r.organization_id:r for r in db.query(m.ClientPriceRule).filter(
        m.ClientPriceRule.organization_id.in_({org.id,parent.id})).populate_existing().all()}
    rule = rules.get(org.id)
    if rule is None or rule.ratio_bps is None:
        rule = rules.get(parent.id) if parent.id != org.id else None
    if rule is not None and rule.ratio_bps is None:
        rule = None
    ratio = rule.ratio_bps if rule else BASE_RATIO_BPS
    payload = [member_id, org.id, org.revision, parent.id, parent.revision,
        [[key, r.revision, r.ratio_bps] for key,r in sorted(rules.items())]]
    context = hashlib.sha256(json.dumps(payload).encode()).hexdigest()
    return InstitutionScope(org.id, org.code, org.name, ratio,
        rule.organization_id if rule else None, rule.revision if rule else 0, context)


def grant_filter(organization_id):
    return m.PointsGrant.id.in_(
        select(m.InstitutionGrant.grant_id).where(
            m.InstitutionGrant.organization_id == organization_id))


def resolve_points_view(db, member_id, *, now=None):
    # A genuinely empty account can display zero before qualification. It never
    # grants catalogue/checkout access and never displays an unassigned balance.
    exists = db.query(m.PointsGrant.id).join(m.PointsAccount).filter(
        m.PointsAccount.member_id == member_id).first()
    if not exists:
        return InstitutionScope(0, '', '', BASE_RATIO_BPS, None, 0, '')
    return resolve(db, member_id, now=now)


def partition_balances(db, member_id, *, now=None):
    """Per-institution balances from original grants; never combine for spending."""
    current = db_time(db, now or utc8_now())
    rows = (db.query(m.PointsGrant, m.InstitutionGrant)
        .join(m.PointsAccount, m.PointsAccount.id == m.PointsGrant.account_id)
        .outerjoin(m.InstitutionGrant, m.InstitutionGrant.grant_id == m.PointsGrant.id)
        .filter(m.PointsAccount.member_id == member_id).all())
    groups = {}
    for grant, owner in rows:
        identity = owner.organization_id if owner else None
        group = groups.setdefault(identity, dict(organization_id=identity,
            code=owner.organization_code if owner else '', name=owner.organization_name if owner else '历史归属待核实',
            available_points=Decimal('0.00'), reserved_points=Decimal('0.00'), recorded_points=Decimal('0.00'), grant_count=0))
        group['grant_count'] += 1
        group['recorded_points'] += Decimal(grant.available_points) + Decimal(grant.reserved_points)
        group['reserved_points'] += Decimal(grant.reserved_points)
        if owner and grant.status == 'ACTIVE' and grant.expires_at > current:
            group['available_points'] += Decimal(grant.available_points)
    return list(groups.values())


def snapshot_order(db, order, scope):
    if db.get(m.InstitutionOrder, order.id):
        raise ScopeError('已有订单归属不能覆盖')
    db.add(m.InstitutionOrder(order_id=order.id, organization_id=scope.organization_id,
        organization_code=scope.code, organization_name=scope.name, ratio_bps=scope.ratio_bps,
        rule_organization_id=scope.rule_organization_id, rule_revision=scope.rule_revision,
        pricing_context=scope.context, created_at=order.created_at))


def assert_order_allocations(db, order, allocations):
    snapshot = db.get(m.InstitutionOrder, order.id)
    owners = {row.grant_id:row.organization_id for row in db.query(m.InstitutionGrant).filter(
        m.InstitutionGrant.grant_id.in_([a.points_grant_id for a in allocations])).all()}
    if snapshot is None:
        # Legacy orders may only return to their original unassigned grants.
        if owners:
            raise RuntimeError('订单缺少机构快照，不能处理已归属机构的积分')
        return
    if any(owners.get(a.points_grant_id) != snapshot.organization_id for a in allocations):
        raise RuntimeError('订单积分跨机构或归属缺失，已阻止处理')


def order_identity(db, order_id):
    row = db.get(m.InstitutionOrder, order_id)
    return {'code':row.organization_code, 'name':row.organization_name} if row else {'code':'', 'name':'历史归属待核实'}


def unit_price(base, scope):
    return price_for_member(base, scope.ratio_bps)
