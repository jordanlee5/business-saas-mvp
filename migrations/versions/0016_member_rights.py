"""Persist member rights applications and attributable handling history."""
from alembic import context, op
import sqlalchemy as sa

revision = "0016_member_rights"
down_revision = "0015_order_parcels"
branch_labels = depends_on = None


def upgrade():
    op.create_table("member_rights_requests",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("public_id", sa.String(36), nullable=False, unique=True),
        sa.Column("member_id", sa.Integer, sa.ForeignKey("members.id"), nullable=False),
        sa.Column("request_key", sa.String(32), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("description", sa.String(800), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("version", sa.Integer, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("member_id", "request_key", name="uq_rights_member_key"),
    )
    op.create_index("ix_member_rights_requests_member_id", "member_rights_requests", ["member_id"])
    op.create_table("member_rights_events",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("request_id", sa.Integer, sa.ForeignKey("member_rights_requests.id"), nullable=False),
        sa.Column("actor_id", sa.Integer, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("action", sa.String(20), nullable=False),
        sa.Column("note", sa.String(800), nullable=False),
        sa.Column("verification_method", sa.String(30), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_member_rights_events_request_id", "member_rights_events", ["request_id"])


def downgrade():
    if context.is_offline_mode() or any(op.get_bind().execute(sa.text(query)).first() for query in (
            "select 1 from member_rights_events limit 1", "select 1 from member_rights_requests limit 1")):
        raise RuntimeError("权益申请记录无法确认清空，禁止降级")
    op.drop_table("member_rights_events")
    op.drop_table("member_rights_requests")
