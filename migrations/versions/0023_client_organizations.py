"""Client hierarchy, uploader scopes, immutable per-batch organization snapshots."""
from alembic import context, op
import sqlalchemy as sa

revision = '0023_client_organizations'
down_revision = '0022_company_banners'
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
    op.create_table('client_organizations',
        sa.Column('id',sa.Integer(),primary_key=True),
        sa.Column('parent_id',sa.Integer(),sa.ForeignKey('client_organizations.id')),
        sa.Column('code',sa.String(32),nullable=False,unique=True),
        sa.Column('name',sa.String(120),nullable=False),
        sa.Column('region',sa.String(80),nullable=False),
        sa.Column('is_active',sa.Boolean(),nullable=False),
        sa.Column('revision',sa.Integer(),nullable=False),
        sa.Column('updated_by_id',sa.Integer(),sa.ForeignKey('users.id'),nullable=False),
        sa.Column('created_at',sa.DateTime(timezone=True),nullable=False),
        sa.Column('updated_at',sa.DateTime(timezone=True),nullable=False),
        sa.CheckConstraint('parent_id IS NULL OR parent_id <> id',name='ck_client_org_parent'),
        sa.CheckConstraint('revision >= 1',name='ck_client_org_revision'))
    op.create_index('ix_client_organizations_parent_id','client_organizations',['parent_id'])
    op.create_table('client_upload_scopes',
        sa.Column('user_id',sa.Integer(),sa.ForeignKey('users.id'),primary_key=True),
        sa.Column('organization_id',sa.Integer(),sa.ForeignKey('client_organizations.id'),primary_key=True),
        sa.Column('is_active',sa.Boolean(),nullable=False),
        sa.Column('revision',sa.Integer(),nullable=False),
        sa.Column('updated_by_id',sa.Integer(),sa.ForeignKey('users.id'),nullable=False),
        sa.Column('updated_at',sa.DateTime(timezone=True),nullable=False),
        sa.CheckConstraint('revision >= 1',name='ck_client_scope_revision'))
    op.create_table('client_batch_ownerships',
        sa.Column('batch_id',sa.Integer(),sa.ForeignKey('upload_batches.id'),primary_key=True),
        sa.Column('organization_id',sa.Integer(),sa.ForeignKey('client_organizations.id'),nullable=False),
        sa.Column('organization_code',sa.String(32),nullable=False),
        sa.Column('organization_name',sa.String(120),nullable=False),
        sa.Column('organization_region',sa.String(80),nullable=False),
        sa.Column('parent_code',sa.String(32)),sa.Column('parent_name',sa.String(120)),
        sa.Column('uploaded_by_id',sa.Integer(),sa.ForeignKey('users.id'),nullable=False),
        sa.Column('created_at',sa.DateTime(timezone=True),nullable=False))
    op.create_index('ix_client_batch_ownerships_organization_id','client_batch_ownerships',['organization_id'])


def downgrade():
    tables = ('client_batch_ownerships','client_upload_scopes','client_organizations')
    if context.is_offline_mode():raise RuntimeError('机构归属降级必须在线检查')
    check_downgrade_path()
    for table in tables:
        if op.get_bind().execute(sa.text('SELECT 1 FROM '+table+' LIMIT 1')).first():
            raise RuntimeError('已有客户或清单归属，拒绝丢弃数据降级；请使用独立恢复点')
    for table in tables:op.drop_table(table)
