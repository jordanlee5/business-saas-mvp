"""add order foundation

Revision ID: 0007_order_foundation
Revises: 0006_inventory_foundation
Create Date: 2026-09-14 12:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0007_order_foundation"
down_revision: Union[str, Sequence[str], None] = "0006_inventory_foundation"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create empty order, item snapshot and points allocation tables."""
    op.create_table(
        "orders",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("order_public_id", sa.String(length=32), nullable=False),
        sa.Column("member_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=30), server_default="CREATED", nullable=False),
        sa.Column("total_points", sa.Numeric(18, 2), nullable=False),
        sa.Column("total_cost_amount", sa.Numeric(18, 2), nullable=False),
        sa.Column("total_quantity", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("length(trim(order_public_id)) > 0", name="ck_orders_public_id_nonblank"),
        sa.CheckConstraint("status IN ('CREATED', 'CANCELLED', 'FULFILLING', 'SHIPPED', 'COMPLETED', 'REFUNDED')", name="ck_orders_status"),
        sa.CheckConstraint("total_points > 0", name="ck_orders_total_points_positive"),
        sa.CheckConstraint("total_cost_amount >= 0", name="ck_orders_total_cost_nonnegative"),
        sa.CheckConstraint("total_quantity > 0", name="ck_orders_total_quantity_positive"),
        sa.ForeignKeyConstraint(["member_id"], ["members.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_orders_id", "orders", ["id"])
    op.create_index("ix_orders_order_public_id", "orders", ["order_public_id"], unique=True)
    op.create_index("ix_orders_member_id", "orders", ["member_id"])
    op.create_index("ix_orders_status", "orders", ["status"])
    op.create_index("ix_orders_created_at", "orders", ["created_at"])

    op.create_table(
        "order_items",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("order_id", sa.Integer(), nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=False),
        sa.Column("sku_id", sa.Integer(), nullable=False),
        sa.Column("supplier_id", sa.Integer(), nullable=False),
        sa.Column("product_public_id_snapshot", sa.String(length=32), nullable=False),
        sa.Column("product_name_snapshot", sa.String(length=200), nullable=False),
        sa.Column("sku_code_snapshot", sa.String(length=64), nullable=False),
        sa.Column("sku_name_snapshot", sa.String(length=160), nullable=False),
        sa.Column("supplier_public_id_snapshot", sa.String(length=32), nullable=False),
        sa.Column("supplier_name_snapshot", sa.String(length=160), nullable=False),
        sa.Column("supplier_sku_code_snapshot", sa.String(length=100), nullable=True),
        sa.Column("unit_points_price", sa.Numeric(18, 2), nullable=False),
        sa.Column("unit_cost_price", sa.Numeric(18, 2), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("line_points", sa.Numeric(18, 2), nullable=False),
        sa.Column("line_cost_amount", sa.Numeric(18, 2), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("quantity > 0", name="ck_order_items_quantity_positive"),
        sa.CheckConstraint("unit_points_price > 0", name="ck_order_items_points_price_positive"),
        sa.CheckConstraint("unit_cost_price >= 0", name="ck_order_items_cost_price_nonnegative"),
        sa.CheckConstraint("line_points = unit_points_price * quantity", name="ck_order_items_points_arithmetic"),
        sa.CheckConstraint("line_cost_amount = unit_cost_price * quantity", name="ck_order_items_cost_arithmetic"),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"]),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"]),
        sa.ForeignKeyConstraint(["sku_id"], ["product_skus.id"]),
        sa.ForeignKeyConstraint(["supplier_id"], ["suppliers.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("order_id", "sku_id", name="uq_order_items_order_sku"),
    )
    for column in ("id", "order_id", "product_id", "sku_id", "supplier_id"):
        op.create_index(f"ix_order_items_{column}", "order_items", [column])

    op.create_table(
        "order_points_grant_allocations",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("order_id", sa.Integer(), nullable=False),
        sa.Column("points_grant_id", sa.Integer(), nullable=False),
        sa.Column("allocated_points", sa.Numeric(18, 2), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("allocated_points > 0", name="ck_order_points_allocations_positive"),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"]),
        sa.ForeignKeyConstraint(["points_grant_id"], ["points_grants.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("order_id", "points_grant_id", name="uq_order_points_allocations_order_grant"),
    )
    op.create_index("ix_order_points_grant_allocations_id", "order_points_grant_allocations", ["id"])
    op.create_index("ix_order_points_grant_allocations_order_id", "order_points_grant_allocations", ["order_id"])
    op.create_index("ix_order_points_grant_allocations_points_grant_id", "order_points_grant_allocations", ["points_grant_id"])


def downgrade() -> None:
    """Remove the empty order foundation from disposable databases."""
    op.drop_table("order_points_grant_allocations")
    op.drop_table("order_items")
    op.drop_table("orders")
