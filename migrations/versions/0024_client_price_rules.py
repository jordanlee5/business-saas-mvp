"""Backoffice client pricing rules, append-only events, and bound previews."""
from alembic import context, op
import sqlalchemy as sa

revision = '0024_client_price_rules'
down_revision = '0023_client_organizations'
branch_labels = depends_on = None

DROPPED_TABLES_BY_REVISION = {
    1: ('users', 'admin_action_logs', 'promotion_pages', 'upload_batches', 'voucher_upload_batches', 'business_records', 'notifications', 'promotion_page_images', 'voucher_records', 'match_reviews'),
    2: ('members', 'member_wechat_bindings', 'points_accounts', 'points_grants', 'points_ledger_entries'),
    3: ('member_activation_credentials',),
    4: ('product_categories', 'suppliers', 'products', 'product_skus'),
    5: ('product_media',),
    6: ('inventory_balances', 'inventory_movements'),
    7: ('orders', 'order_items', 'order_points_grant_allocations'),
    11: ('supplier_settlement_batches', 'supplier_settlement_items'),
    12: ('member_addresses', 'member_address_events'),
    13: ('order_delivery_snapshots',),
    14: ('order_privacy_receipts', 'order_fulfillment_notices'),
    15: ('order_parcels', 'order_parcel_items'),
    16: ('member_rights_requests', 'member_rights_events'),
    17: ('backoffice_sessions',),
    18: ('member_qualifications', 'member_qualification_events'),
    19: ('home_entry_images',),
    20: ('home_banners',),
    21: ('admin_phone_bindings', 'admin_phone_challenges', 'admin_phone_limits'),
    22: ('company_banners',),
    23: ('client_organizations', 'client_upload_scopes', 'client_batch_ownerships'),
    24: ('client_price_rules', 'client_price_rule_events', 'client_price_previews'),
}


def check_downgrade_path():
    # SQLite commits each Alembic step. Refuse before dropping even an empty
    # new table if a later step would encounter data that must be retained.
    target = context.get_revision_argument()
    if target is None:
        target_number = 0
    elif isinstance(target, str) and target[:4].isdigit():
        target_number = int(target[:4])
    else:
        raise RuntimeError('跨版本回退请指定完整目标版本，以便先核对保留数据')
    for number, names in DROPPED_TABLES_BY_REVISION.items():
        if number <= target_number:
            continue
        for name in names:
            if op.get_bind().execute(sa.text('SELECT 1 FROM ' + name + ' LIMIT 1')).first():
                raise RuntimeError('回退路径包含已有数据，拒绝删除留痕或业务结构；请使用独立恢复点')


def upgrade():
    op.create_table('client_price_rules',
        sa.Column('organization_id',sa.Integer(),sa.ForeignKey('client_organizations.id'),primary_key=True),
        sa.Column('ratio_bps',sa.Integer()),sa.Column('revision',sa.Integer(),nullable=False),
        sa.Column('updated_by_id',sa.Integer(),sa.ForeignKey('users.id'),nullable=False),
        sa.Column('effective_at',sa.DateTime(timezone=True),nullable=False),
        sa.CheckConstraint('ratio_bps IS NULL OR (ratio_bps >= 100 AND ratio_bps <= 30000)',name='ck_client_price_ratio'),
        sa.CheckConstraint('revision >= 1',name='ck_client_price_revision'))
    op.create_table('client_price_rule_events',
        sa.Column('id',sa.Integer(),primary_key=True),
        sa.Column('organization_id',sa.Integer(),sa.ForeignKey('client_organizations.id'),nullable=False),
        sa.Column('revision',sa.Integer(),nullable=False),sa.Column('old_ratio_bps',sa.Integer()),
        sa.Column('ratio_bps',sa.Integer()),
        sa.Column('actor_id',sa.Integer(),sa.ForeignKey('users.id'),nullable=False),
        sa.Column('effective_at',sa.DateTime(timezone=True),nullable=False),
        sa.UniqueConstraint('organization_id','revision',name='uq_client_price_event_revision'),
        sa.CheckConstraint('ratio_bps IS NULL OR (ratio_bps >= 100 AND ratio_bps <= 30000)',name='ck_client_price_event_ratio'),
        sa.CheckConstraint('old_ratio_bps IS NULL OR (old_ratio_bps >= 100 AND old_ratio_bps <= 30000)',name='ck_client_price_event_old_ratio'),
        sa.CheckConstraint('revision >= 1',name='ck_client_price_event_revision'))
    op.create_table('client_price_previews',
        sa.Column('token_digest',sa.String(64),primary_key=True),
        sa.Column('actor_id',sa.Integer(),sa.ForeignKey('users.id'),nullable=False),
        sa.Column('organization_id',sa.Integer(),sa.ForeignKey('client_organizations.id'),nullable=False),
        sa.Column('ratio_bps',sa.Integer()),sa.Column('state_digest',sa.String(64),nullable=False),
        sa.Column('created_at',sa.DateTime(timezone=True),nullable=False),
        sa.Column('expires_at',sa.DateTime(timezone=True),nullable=False),sa.Column('applied_revision',sa.Integer()),
        sa.CheckConstraint('ratio_bps IS NULL OR (ratio_bps >= 100 AND ratio_bps <= 30000)',name='ck_client_price_preview_ratio'),
        sa.CheckConstraint('applied_revision IS NULL OR applied_revision >= 1',name='ck_client_price_preview_revision'))


def downgrade():
    if context.is_offline_mode():
        raise RuntimeError('价格规则回退必须在线检查')
    check_downgrade_path()
    for name in ('client_price_previews','client_price_rule_events','client_price_rules'):
        op.drop_table(name)
