"""Confirmed member browsing qualifications and attributable change history."""
from alembic import context, op
import sqlalchemy as sa

revision = "0018_member_qualifications"
down_revision = "0017_backoffice_sessions"
branch_labels = depends_on = None


def upgrade():
    op.create_table("member_qualifications",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("business_record_id", sa.Integer(), sa.ForeignKey("business_records.id"), nullable=False, unique=True),
        sa.Column("member_id", sa.Integer(), sa.ForeignKey("members.id")),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("valid_until", sa.DateTime(timezone=True), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("confirmed_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("bound_at", sa.DateTime(timezone=True)),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("valid_until > valid_from", name="ck_member_qualification_period"),
        sa.CheckConstraint("version >= 1", name="ck_member_qualification_version"),
        sa.CheckConstraint("(member_id IS NULL AND bound_at IS NULL) OR (member_id IS NOT NULL AND bound_at IS NOT NULL)", name="ck_member_qualification_binding"))
    op.create_index("ix_member_qualification_member_enabled", "member_qualifications", ["member_id", "enabled"])
    op.create_table("member_qualification_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("qualification_id", sa.Integer(), sa.ForeignKey("member_qualifications.id"), nullable=False),
        sa.Column("actor_id", sa.Integer(), sa.ForeignKey("users.id")),
        sa.Column("action", sa.String(20), nullable=False),
        sa.Column("reason", sa.String(500), nullable=False),
        sa.Column("before_state", sa.Text(), nullable=False),
        sa.Column("after_state", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("action IN ('CONFIRM','BIND','ENABLE','DISABLE','VALIDITY')", name="ck_member_qualification_action"))
    op.create_index("ix_member_qualification_events_qualification_id", "member_qualification_events", ["qualification_id"])


def downgrade():
    if context.is_offline_mode():
        raise RuntimeError("资格留痕降级必须在线检查数据")
    for table in ("member_qualifications", "member_qualification_events"):
        if op.get_bind().execute(sa.text(f"SELECT 1 FROM {table} LIMIT 1")).first():
            raise RuntimeError("存在会员资格或操作记录，拒绝删除留痕")
    op.drop_table("member_qualification_events")
    op.drop_table("member_qualifications")
