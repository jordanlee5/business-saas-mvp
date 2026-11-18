"""Immutable institution ownership for points batches and orders."""
from alembic import context, op
import sqlalchemy as sa

revision = '0025_institution_isolation'
down_revision = '0024_client_price_rules'
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
    25: ('institution_grants','institution_orders'),
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
    op.create_table('institution_grants',
        sa.Column('grant_id',sa.Integer(),sa.ForeignKey('points_grants.id'),primary_key=True),
        sa.Column('organization_id',sa.Integer(),sa.ForeignKey('client_organizations.id'),nullable=False),
        sa.Column('organization_code',sa.String(32),nullable=False),
        sa.Column('organization_name',sa.String(120),nullable=False),
        sa.Column('created_at',sa.DateTime(timezone=True),nullable=False))
    op.create_index('ix_institution_grants_organization_id','institution_grants',['organization_id'])
    op.create_table('institution_orders',
        sa.Column('order_id',sa.Integer(),sa.ForeignKey('orders.id'),primary_key=True),
        sa.Column('organization_id',sa.Integer(),sa.ForeignKey('client_organizations.id'),nullable=False),
        sa.Column('organization_code',sa.String(32),nullable=False),
        sa.Column('organization_name',sa.String(120),nullable=False),
        sa.Column('ratio_bps',sa.Integer(),nullable=False),
        sa.Column('rule_organization_id',sa.Integer(),sa.ForeignKey('client_organizations.id')),
        sa.Column('rule_revision',sa.Integer(),nullable=False),
        sa.Column('pricing_context',sa.String(64),nullable=False),
        sa.Column('created_at',sa.DateTime(timezone=True),nullable=False),
        sa.CheckConstraint('ratio_bps >= 100 AND ratio_bps <= 30000',name='ck_institution_order_ratio'),
        sa.CheckConstraint('rule_revision >= 0',name='ck_institution_order_revision'))
    op.create_index('ix_institution_orders_organization_id','institution_orders',['organization_id'])
    # Only already verified immutable batch ownership is evidence. No name,
    # uploader account or geography inference; original rows are never updated.
    op.execute(sa.text("""INSERT INTO institution_grants
        (grant_id, organization_id, organization_code, organization_name, created_at)
        SELECT g.id, b.organization_id, b.organization_code, b.organization_name, g.created_at
        FROM points_grants g JOIN business_records r ON r.id=g.business_record_id
        JOIN client_batch_ownerships b ON b.batch_id=r.batch_id"""))
    # Before this revision prices were base-price only. Preserve all paid item
    # prices, and attach an institution only if EVERY allocation proves the same one.
    op.execute(sa.text("""INSERT INTO institution_orders
        (order_id, organization_id, organization_code, organization_name, ratio_bps,
         rule_organization_id, rule_revision, pricing_context, created_at)
        SELECT o.id, MIN(g.organization_id), MIN(g.organization_code), MIN(g.organization_name),
               10000, NULL, 0, '0000000000000000000000000000000000000000000000000000000000000000', o.created_at
        FROM orders o JOIN order_points_grant_allocations a ON a.order_id=o.id
        LEFT JOIN institution_grants g ON g.grant_id=a.points_grant_id
        GROUP BY o.id, o.created_at
        HAVING COUNT(*)=COUNT(g.grant_id) AND COUNT(DISTINCT g.organization_id)=1"""))


def downgrade():
    if context.is_offline_mode():
        raise RuntimeError('机构隔离回退必须在线检查')
    check_downgrade_path()
    op.drop_table('institution_orders')
    op.drop_table('institution_grants')
