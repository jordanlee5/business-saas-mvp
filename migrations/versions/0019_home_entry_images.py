"""Operator-managed images for eight fixed homepage entrances."""
from alembic import context, op
import sqlalchemy as sa

revision = '0019_home_entry_images'
down_revision = '0018_member_qualifications'
branch_labels = depends_on = None


def upgrade():
    op.create_table('home_entry_images',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('entry_key', sa.String(16), nullable=False, unique=True),
        sa.Column('image_path', sa.String(500)),
        sa.Column('revision', sa.Integer(), nullable=False),
        sa.Column('updated_by_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("entry_key IN ('home','food','fresh','bags','digital','beauty','kitchen','kids')", name='ck_home_entry_key'),
        sa.CheckConstraint('revision >= 1', name='ck_home_entry_revision'))


def downgrade():
    if context.is_offline_mode():
        raise RuntimeError('首页素材降级必须在线检查数据')
    if op.get_bind().execute(sa.text('SELECT 1 FROM home_entry_images LIMIT 1')).first():
        raise RuntimeError('已有首页素材配置，拒绝删除；请保留数据和素材')
    op.drop_table('home_entry_images')
