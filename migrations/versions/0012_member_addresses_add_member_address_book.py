"""Create empty private member address book and minimal event log.

Revision ID: 0012_member_addresses
Revises: 0011_supplier_settlement_foundation
"""
from alembic import context, op
import sqlalchemy as sa

revision = "0012_member_addresses"
down_revision = "0011_supplier_settlement_foundation"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "member_addresses",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("address_public_id", sa.String(37), nullable=False, unique=True),
        sa.Column("member_id", sa.Integer(), sa.ForeignKey("members.id"), nullable=False),
        *(sa.Column(key, sa.String(limit), nullable=False) for key, limit in
          (("recipient_name", 80), ("phone", 16), ("province", 80),
           ("city", 80), ("district", 80), ("detail", 240))),
        sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("length(address_public_id) = 37", name="ck_member_addresses_public_id"),
        *(sa.CheckConstraint(f"length(trim({key})) BETWEEN 1 AND {limit}",
                            name=f"ck_member_addresses_{key}_length") for key, limit in
          (("recipient_name", 80), ("phone", 16), ("province", 80),
           ("city", 80), ("district", 80), ("detail", 240))),
    )
    op.create_index("ix_member_addresses_member_id", "member_addresses", ["member_id"])
    op.create_index("uq_member_addresses_one_default", "member_addresses", ["member_id"],
                    unique=True, sqlite_where=sa.text("is_default = 1"),
                    postgresql_where=sa.text("is_default = true"))
    op.create_table(
        "member_address_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("member_id", sa.Integer(), sa.ForeignKey("members.id"), nullable=False),
        sa.Column("address_public_id", sa.String(37), nullable=False),
        sa.Column("action", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("action IN ('CREATE', 'UPDATE', 'SET_DEFAULT', 'DELETE')",
                           name="ck_member_address_events_action"),
        sa.CheckConstraint("length(address_public_id) = 37", name="ck_member_address_events_public_id"),
    )
    op.create_index("ix_member_address_events_member_id", "member_address_events", ["member_id"])
    op.create_index("ix_member_address_events_address_public_id", "member_address_events", ["address_public_id"])


def downgrade():
    # Do not destroy real addresses or audit evidence just to roll back code.
    if context.is_offline_mode():
        raise RuntimeError("离线模式无法确认地址表为空，禁止生成降级删除脚本")
    for name in ("member_addresses", "member_address_events"):
        if op.get_bind().execute(sa.text(f"SELECT count(*) FROM {name}")).scalar():
            raise RuntimeError("地址或审计表非空，禁止自动降级；请先制定数据保留方案")
    op.drop_table("member_address_events")
    op.drop_table("member_addresses")
