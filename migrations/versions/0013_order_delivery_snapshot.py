"""Independent immutable checkout delivery snapshots; no historical backfill."""
from alembic import op, context
import sqlalchemy as sa
revision = '0013_order_delivery_snapshot'
down_revision = '0012_member_addresses'
branch_labels = depends_on = None
FIELDS = (('recipient_name',80),('phone',16),('province',80),('city',80),('district',80),('detail',240))
def upgrade():
    op.create_table('order_delivery_snapshots',
        sa.Column('order_id',sa.Integer(),sa.ForeignKey('orders.id'),primary_key=True),
        sa.Column('address_public_id',sa.String(37),nullable=False),
        sa.Column('request_digest',sa.String(64),nullable=False),
        *(sa.Column(key,sa.String(limit),nullable=False) for key,limit in FIELDS),
        *(sa.CheckConstraint(f'length(trim({key})) BETWEEN 1 AND {limit}',name=f'ck_order_delivery_{key}') for key,limit in FIELDS),
        sa.CheckConstraint('length(request_digest) = 64',name='ck_order_delivery_digest'))
def downgrade():
    if context.is_offline_mode() or op.get_bind().execute(sa.text('select count(*) from order_delivery_snapshots')).scalar():
        raise RuntimeError('订单收货快照非空或无法确认，禁止自动降级')
    op.drop_table('order_delivery_snapshots')
