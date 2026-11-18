"""Backoffice-only pricing configuration. Not connected to member prices yet."""
from datetime import timedelta
from decimal import Decimal, ROUND_HALF_UP
import hashlib
import json
import re
import secrets

from .. import models as m
from ..time_utils import UTC8_TIMEZONE, utc8_now
from . import client_organizations as organizations

MIN_RATIO_BPS = 100  # 1% of base price; one basis point is 0.01%.
MAX_RATIO_BPS = 30000
BASE_RATIO_BPS = 10000
MAX_PRICE = Decimal('9999999999999999.99')  # Existing Numeric(18, 2).
PREVIEW_LIFETIME = timedelta(minutes=10)


def rule_now():
    # Legacy helper returns a naive UTC+8 time; persist explicit timezone here.
    value = utc8_now()
    return value.replace(tzinfo=UTC8_TIMEZONE) if value.tzinfo is None else value


def parse_ratio(mode, percent):
    if mode == 'inherit':
        return None
    if mode != 'custom' or not re.fullmatch(r'[0-9]{1,3}(?:\.[0-9]{1,2})?', str(percent).strip()):
        raise ValueError('请填写1%至300%的售价比例，最多两位小数')
    ratio = int(Decimal(str(percent).strip()) * 100)
    if not MIN_RATIO_BPS <= ratio <= MAX_RATIO_BPS:
        raise ValueError('售价比例须在1%至300%之间')
    return ratio


def percent_text(ratio):
    return format(Decimal(ratio) / 100, '.2f') + '%'


def ratio_label(ratio):
    if ratio is None:
        return '沿用上级 / 无上级时按原价'
    if ratio == BASE_RATIO_BPS:
        return '按原价（明确100%）'
    delta = abs(ratio - BASE_RATIO_BPS)
    return ('优惠' if ratio < BASE_RATIO_BPS else '加价') + percent_text(delta)


def price_for_ratio(base, ratio):
    if isinstance(ratio, bool) or not isinstance(ratio, int) or not MIN_RATIO_BPS <= ratio <= MAX_RATIO_BPS:
        raise ValueError('售价比例无效')
    base = Decimal(str(base))
    if not base.is_finite() or base <= 0 or base > MAX_PRICE:
        raise ValueError('商品基准售价无效')
    value = (base * Decimal(ratio) / BASE_RATIO_BPS).quantize(Decimal('.01'), rounding=ROUND_HALF_UP)
    if value <= 0 or value > MAX_PRICE:
        raise ValueError('调整后的单价四舍五入后为零或超出金额范围，请调整比例或商品基准价')
    return value


def preview_effective(org, rules, proposed=None):
    """Administrative preview only; callers separately display disabled status."""
    for identity in (org.id, org.parent_id):
        if identity is None:
            continue
        rule = rules.get(identity)
        ratio = rule.ratio_bps if rule else None
        if proposed is not None and identity == proposed[0]:
            ratio = proposed[1]
        if ratio is not None:
            return ratio, identity
    return BASE_RATIO_BPS, None


def _state(db, organization_id, *, lock=False):
    selected = db.get(m.ClientOrganization, organization_id)
    if not selected:
        raise ValueError('客户机构不存在')
    root_id = selected.parent_id or selected.id
    # All rule writes for one company acquire its root first, then branches.
    root_query = db.query(m.ClientOrganization).filter_by(id=root_id).populate_existing()
    root = (root_query.with_for_update() if lock else root_query).one()
    query = db.query(m.ClientOrganization).filter_by(parent_id=root_id).order_by(m.ClientOrganization.id).populate_existing()
    family = [root] + (query.with_for_update() if lock else query).all()
    selected = next(row for row in family if row.id == organization_id)
    if not root.is_active or not selected.is_active:
        raise ValueError('机构或所属总公司已停用，不能修改价格规则')
    rule_query = db.query(m.ClientPriceRule).filter(m.ClientPriceRule.organization_id.in_([row.id for row in family])).populate_existing()
    rules = {row.organization_id: row for row in rule_query.all()}
    # Include inactive/unpublished SKUs: the rule applies to the full catalogue.
    sku_query = db.query(m.ProductSku).order_by(m.ProductSku.id).populate_existing()
    skus = (sku_query.with_for_update() if lock else sku_query).all()
    products = {row.id: row for row in db.query(m.Product).populate_existing().all()}
    payload = {
        'organizations': [[o.id, o.parent_id, o.code, o.name, o.is_active, o.revision] for o in family],
        'rules': [[r.organization_id, r.ratio_bps, r.revision] for r in sorted(rules.values(), key=lambda r: r.organization_id)],
        'skus': [[s.id, s.product_id, s.sku_code, s.name, str(s.points_price), str(s.cost_price), s.is_active] for s in skus],
        'products': [[p.id, p.name, p.status] for p in sorted(products.values(), key=lambda p: p.id)],
    }
    digest = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    return selected, family, rules, skus, products, digest


def _view(state, ratio):
    selected, family, rules, skus, products, digest = state
    proposed = (selected.id, ratio)
    before_ratio, _ = preview_effective(selected, rules)
    after_ratio, after_source = preview_effective(selected, rules, proposed)
    impacts = []
    for org in family:
        if selected.parent_id and org.id != selected.id:
            continue
        before, source_before = preview_effective(org, rules)
        after, source_after = preview_effective(org, rules, proposed)
        own = rules.get(org.id)
        affected = org.id == selected.id or not (own and own.ratio_bps is not None)
        impacts.append(dict(name=org.name, code=org.code, active=org.is_active,
            before=percent_text(before), after=percent_text(after), affected=affected,
            source='本机构' if source_after == org.id else '总公司' if source_after else '商品基准价'))
    rows = []
    for sku in skus:
        # Validate every SKU, not just the displayed sample. Cost is never written.
        after = price_for_ratio(sku.points_price, after_ratio)
        if len(rows) < 20:
            try:
                before = str(price_for_ratio(sku.points_price, before_ratio))
            except ValueError:
                before = '原规则金额异常'
            rows.append(dict(code=sku.sku_code, name=sku.name, product=products[sku.product_id].name,
                base=str(sku.points_price), before=before, after=str(after), cost=str(sku.cost_price)))
    current = rules.get(selected.id)
    return dict(organization=selected, ratio=ratio, ratio_label=ratio_label(ratio),
        effective_percent=percent_text(after_ratio), effective_label=ratio_label(after_ratio),
        example=str(price_for_ratio(100, after_ratio)), impacts=impacts, products=len(products), skus=len(skus),
        sample=rows, state_digest=digest, current_revision=current.revision if current else 0,
        scope_count=sum(1 for row in impacts if row['affected']))


def prepare(db, actor_id, organization_id, mode, percent):
    organizations.require_manager(db, actor_id)
    ratio = parse_ratio(mode, percent)
    view = _view(_state(db, organization_id, lock=True), ratio)
    now = rule_now()
    token = secrets.token_urlsafe(32)
    db.add(m.ClientPricePreview(token_digest=hashlib.sha256(token.encode()).hexdigest(),
        actor_id=actor_id, organization_id=organization_id, ratio_bps=ratio,
        state_digest=view['state_digest'], created_at=now, expires_at=now + PREVIEW_LIFETIME))
    view['preview_token'] = token
    return view


def save(db, actor_id, preview_token):
    organizations.require_manager(db, actor_id)
    if not isinstance(preview_token, str) or len(preview_token) != 43:
        raise ValueError('请先重新预览，再确认保存')
    preview = db.query(m.ClientPricePreview).filter_by(
        token_digest=hashlib.sha256(preview_token.encode()).hexdigest(), actor_id=actor_id
    ).populate_existing().with_for_update().one_or_none()
    if not preview:
        raise ValueError('预览无效，请重新预览')
    if preview.applied_revision is not None:
        return db.query(m.ClientPriceRuleEvent).filter_by(organization_id=preview.organization_id,
            revision=preview.applied_revision).one(), False
    now = rule_now()
    # SQLite does not retain timezone metadata; app stores all timestamps in UTC+8.
    expires = preview.expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=now.tzinfo)
    if expires <= now:
        raise ValueError('预览已超过10分钟，请重新预览')
    state = _state(db, preview.organization_id, lock=True)
    if state[-1] != preview.state_digest:
        raise ValueError('机构、比例或商品信息已变化，请重新预览后保存')
    _view(state, preview.ratio_bps)  # Repeat amount bounds before committing.
    selected, _, rules, _, _, _ = state
    previous = rules.get(selected.id)
    revision = previous.revision + 1 if previous else 1
    before = previous.ratio_bps if previous else None
    effective_at = rule_now()
    if previous:
        changed = db.query(m.ClientPriceRule).filter_by(organization_id=selected.id,
            revision=previous.revision).update(dict(ratio_bps=preview.ratio_bps, revision=revision,
                updated_by_id=actor_id, effective_at=effective_at), synchronize_session=False)
        if changed != 1:
            raise ValueError('价格规则已变化，请重新预览')
    else:
        db.add(m.ClientPriceRule(organization_id=selected.id, ratio_bps=preview.ratio_bps,
            revision=revision, updated_by_id=actor_id, effective_at=effective_at))
    event = m.ClientPriceRuleEvent(organization_id=selected.id, revision=revision,
        old_ratio_bps=before, ratio_bps=preview.ratio_bps, actor_id=actor_id, effective_at=effective_at)
    db.add(event)
    organizations.audit(db, actor_id, 'client_price_rule_change', selected.id,
        f'价格规则第{revision}版：{ratio_label(before)} → {ratio_label(preview.ratio_bps)}')
    preview.applied_revision = revision
    db.flush()
    return event, True


def overview(db):
    orgs = db.query(m.ClientOrganization).order_by(m.ClientOrganization.code).all()
    rules = {r.organization_id:r for r in db.query(m.ClientPriceRule).all()}
    names = {o.id:o.name for o in orgs}
    rows = []
    for org in orgs:
        rule = rules.get(org.id)
        ratio, source = preview_effective(org, rules)
        parent = next((o for o in orgs if o.id == org.parent_id), None)
        rows.append(dict(org=org, rule=rule, own=ratio_label(rule.ratio_bps if rule else None),
            effective=percent_text(ratio), label=ratio_label(ratio), source=names.get(source, '商品基准价'),
            effective_rule=rules.get(source), inherited=source is not None and source != org.id,
            available=org.is_active and (parent is None or parent.is_active)))
    return rows
