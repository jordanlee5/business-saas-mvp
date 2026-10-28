"""Order erasure receipts and published fulfillment notices; no data backfill."""
from alembic import op, context
import sqlalchemy as sa
revision = '0014_order_privacy'
down_revision = '0013_order_delivery_snapshot'
branch_labels = depends_on = None


def upgrade():
    op.create_table('order_privacy_receipts',
        sa.Column('order_id', sa.Integer(), sa.ForeignKey('orders.id'), primary_key=True),
        sa.Column('request_digest', sa.String(64), nullable=False),
        sa.Column('policy_version', sa.String(80), nullable=False),
        sa.Column('cleared_at', sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint('length(request_digest) = 64', name='ck_order_privacy_digest'))
    op.create_table('order_fulfillment_notices',
        sa.Column('order_id', sa.Integer(), sa.ForeignKey('orders.id'), primary_key=True),
        sa.Column('digest', sa.String(64), nullable=False),
        sa.Column('payload', sa.Text(), nullable=False),
        sa.CheckConstraint('length(digest) = 64', name='ck_order_notice_digest'))


def downgrade():
    if context.is_offline_mode():
        raise RuntimeError('无法确认隐私证据为空，禁止降级')
    for table in ('order_privacy_receipts', 'order_fulfillment_notices'):
        if op.get_bind().execute(sa.text('select count(*) from '+table)).scalar():
            raise RuntimeError('隐私或发货告知证据非空，禁止降级')
    op.drop_table('order_fulfillment_notices')
    op.drop_table('order_privacy_receipts')
