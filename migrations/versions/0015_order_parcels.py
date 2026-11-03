"""Add parcel manifests without guessing the contents of historical waybills."""
from alembic import context, op
import sqlalchemy as sa

revision = "0015_order_parcels"
down_revision = "0014_order_privacy"
branch_labels = depends_on = None


def upgrade():
    op.create_table("order_parcels",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("order_id", sa.Integer, sa.ForeignKey("orders.id"), nullable=False),
        sa.Column("carrier", sa.String(100), nullable=False),
        sa.Column("tracking_number", sa.String(100), nullable=False),
        sa.Column("actor_id", sa.Integer, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("shipped_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("carrier", "tracking_number", name="uq_parcel_waybill"))
    op.create_index("ix_order_parcels_order_id", "order_parcels", ["order_id"])
    op.create_table("order_parcel_items",
        sa.Column("parcel_id", sa.Integer, sa.ForeignKey("order_parcels.id"), primary_key=True),
        sa.Column("item_id", sa.Integer, sa.ForeignKey("order_items.id"), primary_key=True),
        sa.Column("quantity", sa.Integer, nullable=False),
        sa.CheckConstraint("quantity > 0", name="ck_parcel_item_positive"))


def downgrade():
    if context.is_offline_mode() or any(op.get_bind().execute(sa.text(query)).first() for query in (
            "select 1 from order_parcels limit 1", "select 1 from order_parcel_items limit 1")):
        raise RuntimeError("包裹资料无法确认清空，禁止降级")
    op.drop_table("order_parcel_items")
    op.drop_table("order_parcels")
