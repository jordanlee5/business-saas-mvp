"""Three versioned public company carousel slots."""
from alembic import context, op
import sqlalchemy as sa

revision = '0022_company_banners'
down_revision = '0021_admin_phone_login'
branch_labels = depends_on = None


def upgrade():
    op.create_table('company_banners',
        sa.Column('slot', sa.Integer(), primary_key=True, autoincrement=False),
        sa.Column('title', sa.String(80), nullable=False),
        sa.Column('image_path', sa.String(500), nullable=False),
        sa.Column('sort_order', sa.Integer(), nullable=False),
        sa.Column('enabled', sa.Boolean(), nullable=False),
        sa.Column('show_copy', sa.Boolean(), nullable=False),
        sa.Column('revision', sa.Integer(), nullable=False),
        sa.Column('updated_by_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint('slot BETWEEN 1 AND 3', name='ck_company_banner_slot'),
        sa.CheckConstraint('revision >= 1', name='ck_company_banner_revision'),
        sa.CheckConstraint('sort_order BETWEEN 0 AND 999', name='ck_company_banner_sort'),
        sa.CheckConstraint('length(trim(title)) BETWEEN 1 AND 80', name='ck_company_banner_title'),
)


def downgrade():
    if context.is_offline_mode() or op.get_bind().execute(sa.text('SELECT 1 FROM company_banners LIMIT 1')).first():
        raise RuntimeError('必须在线确认轮播配置为空；已有素材时拒绝删表降级')
    op.drop_table('company_banners')
