"""Reserved admin phone authentication state; no phone or OTP plaintext."""
from alembic import context, op
import sqlalchemy as sa

revision = '0021_admin_phone_login'
down_revision = '0020_home_banners'
branch_labels = depends_on = None


def upgrade():
    op.create_table('admin_phone_bindings',
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id'), primary_key=True),
        sa.Column('phone_digest', sa.String(64), unique=True),
        sa.Column('revision', sa.Integer(), nullable=False),
        sa.Column('updated_by_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('updated_at', sa.BigInteger(), nullable=False),
        sa.CheckConstraint('revision >= 1', name='ck_admin_phone_binding_revision'))
    op.create_table('admin_phone_challenges',
        sa.Column('challenge_id', sa.String(64), primary_key=True),
        sa.Column('phone_digest', sa.String(64), nullable=False),
        sa.Column('browser_digest', sa.String(64), nullable=False),
        sa.Column('captcha_digest', sa.String(64), nullable=False, unique=True),
        sa.Column('code_digest', sa.String(64), nullable=False),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id')),
        sa.Column('binding_revision', sa.Integer()),
        sa.Column('credential_digest', sa.String(64)),
        sa.Column('attempts', sa.Integer(), nullable=False),
        sa.Column('state', sa.String(10), nullable=False),
        sa.Column('created_at', sa.BigInteger(), nullable=False),
        sa.Column('expires_at', sa.BigInteger(), nullable=False),
        sa.CheckConstraint("state IN ('pending','ready','used','failed')", name='ck_admin_phone_challenge_state'),
        sa.CheckConstraint('attempts BETWEEN 0 AND 5', name='ck_admin_phone_challenge_attempts'))
    for column in ('phone_digest','created_at'):
        op.create_index('ix_admin_phone_challenges_'+column,'admin_phone_challenges',[column])
    op.create_table('admin_phone_limits',
        sa.Column('key_digest', sa.String(64), primary_key=True),
        sa.Column('count', sa.Integer(), nullable=False),
        sa.Column('expires_at', sa.BigInteger(), nullable=False),
        sa.CheckConstraint('count >= 1', name='ck_admin_phone_limit_count'))
    op.create_index('ix_admin_phone_limits_expires_at','admin_phone_limits',['expires_at'])


def downgrade():
    if context.is_offline_mode():
        raise RuntimeError('手机号登录状态降级必须在线核对')
    for table in ('admin_phone_bindings','admin_phone_challenges','admin_phone_limits'):
        if op.get_bind().execute(sa.text(f'SELECT 1 FROM {table} LIMIT 1')).first():
            raise RuntimeError('存在绑定或登录状态，拒绝删除；请保留数据')
    for table in ('admin_phone_limits','admin_phone_challenges','admin_phone_bindings'):
        op.drop_table(table)
