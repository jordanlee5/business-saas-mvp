"""add supplier settlement persistence foundation

Revision ID: 0011_supplier_settlement_foundation
Revises: 0010_order_refund_recovery
Create Date: 2026-09-18 12:00:00

"""
from typing import Sequence, Union

from alembic import context, op
import sqlalchemy as sa


revision: str = "0011_supplier_settlement_foundation"
down_revision: Union[str, Sequence[str], None] = (
    "0010_order_refund_recovery"
)
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create empty settlement batch and immutable item snapshot tables."""
    # Alembic creates ``alembic_version.version_num`` as VARCHAR(32) by
    # default.  This revision identifier is longer than 32 characters, and
    # PostgreSQL enforces that limit (unlike SQLite).  Widen the framework
    # metadata column before Alembic records this revision at the end of the
    # migration.  Keeping the revision identifier unchanged also preserves
    # compatibility with SQLite databases that have already reached 0011.
    if op.get_bind().dialect.name == "postgresql":
        op.alter_column(
            "alembic_version",
            "version_num",
            existing_type=sa.String(length=32),
            type_=sa.String(length=64),
            existing_nullable=False,
        )

    op.create_table(
        "supplier_settlement_batches",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column(
            "settlement_public_id", sa.String(length=32), nullable=False
        ),
        sa.Column("supplier_id", sa.Integer(), nullable=False),
        sa.Column(
            "supplier_public_id_snapshot",
            sa.String(length=32),
            nullable=False,
        ),
        sa.Column(
            "supplier_name_snapshot",
            sa.String(length=160),
            nullable=False,
        ),
        sa.Column(
            "period_start", sa.DateTime(timezone=True), nullable=False
        ),
        sa.Column(
            "period_end", sa.DateTime(timezone=True), nullable=False
        ),
        sa.Column(
            "status",
            sa.String(length=30),
            server_default="PENDING_CONFIRMATION",
            nullable=False,
        ),
        sa.Column("order_count", sa.Integer(), nullable=False),
        sa.Column("item_count", sa.Integer(), nullable=False),
        sa.Column("total_quantity", sa.Integer(), nullable=False),
        sa.Column(
            "total_cost_amount", sa.Numeric(18, 2), nullable=False
        ),
        sa.Column(
            "generated_by_admin_id", sa.Integer(), nullable=False
        ),
        sa.Column(
            "generated_at", sa.DateTime(timezone=True), nullable=False
        ),
        sa.Column(
            "confirmed_by_admin_id", sa.Integer(), nullable=True
        ),
        sa.Column(
            "confirmed_at", sa.DateTime(timezone=True), nullable=True
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "length(trim(settlement_public_id)) > 0",
            name="ck_supplier_settlement_batches_public_id_nonblank",
        ),
        sa.CheckConstraint(
            "length(trim(supplier_public_id_snapshot)) > 0",
            name="ck_supplier_settlement_batches_supplier_id_nonblank",
        ),
        sa.CheckConstraint(
            "length(trim(supplier_name_snapshot)) > 0",
            name="ck_supplier_settlement_batches_supplier_name_nonblank",
        ),
        sa.CheckConstraint(
            "status IN ('PENDING_CONFIRMATION', 'CONFIRMED')",
            name="ck_supplier_settlement_batches_status",
        ),
        sa.CheckConstraint(
            "period_end > period_start",
            name="ck_supplier_settlement_batches_period_order",
        ),
        sa.CheckConstraint(
            "generated_at >= period_end",
            name="ck_supplier_settlement_batches_generated_after_period",
        ),
        sa.CheckConstraint(
            "order_count > 0 AND item_count > 0 AND total_quantity > 0",
            name="ck_supplier_settlement_batches_counts_positive",
        ),
        sa.CheckConstraint(
            "order_count <= item_count AND item_count <= total_quantity",
            name="ck_supplier_settlement_batches_counts_consistent",
        ),
        sa.CheckConstraint(
            "total_cost_amount >= 0",
            name="ck_supplier_settlement_batches_cost_nonnegative",
        ),
        sa.CheckConstraint(
            "(status = 'PENDING_CONFIRMATION' AND "
            "confirmed_by_admin_id IS NULL AND confirmed_at IS NULL) OR "
            "(status = 'CONFIRMED' AND "
            "confirmed_by_admin_id IS NOT NULL AND confirmed_at IS NOT NULL)",
            name="ck_supplier_settlement_batches_confirmation_evidence",
        ),
        sa.CheckConstraint(
            "confirmed_at IS NULL OR confirmed_at >= generated_at",
            name="ck_supplier_settlement_batches_confirmation_time_order",
        ),
        sa.ForeignKeyConstraint(["supplier_id"], ["suppliers.id"]),
        sa.ForeignKeyConstraint(
            ["generated_by_admin_id"], ["users.id"]
        ),
        sa.ForeignKeyConstraint(
            ["confirmed_by_admin_id"], ["users.id"]
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    for column, unique in (
        ("id", False),
        ("settlement_public_id", True),
        ("supplier_id", False),
        ("period_start", False),
        ("period_end", False),
        ("status", False),
        ("generated_by_admin_id", False),
        ("generated_at", False),
        ("confirmed_by_admin_id", False),
        ("confirmed_at", False),
    ):
        op.create_index(
            f"ix_supplier_settlement_batches_{column}",
            "supplier_settlement_batches",
            [column],
            unique=unique,
        )

    op.create_table(
        "supplier_settlement_items",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("settlement_batch_id", sa.Integer(), nullable=False),
        sa.Column("supplier_id", sa.Integer(), nullable=False),
        sa.Column("order_id", sa.Integer(), nullable=False),
        sa.Column("order_item_id", sa.Integer(), nullable=False),
        sa.Column(
            "order_public_id_snapshot",
            sa.String(length=32),
            nullable=False,
        ),
        sa.Column(
            "order_completed_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "product_public_id_snapshot",
            sa.String(length=32),
            nullable=False,
        ),
        sa.Column(
            "product_name_snapshot",
            sa.String(length=200),
            nullable=False,
        ),
        sa.Column(
            "sku_code_snapshot", sa.String(length=64), nullable=False
        ),
        sa.Column(
            "sku_name_snapshot", sa.String(length=160), nullable=False
        ),
        sa.Column(
            "supplier_public_id_snapshot",
            sa.String(length=32),
            nullable=False,
        ),
        sa.Column(
            "supplier_name_snapshot",
            sa.String(length=160),
            nullable=False,
        ),
        sa.Column(
            "supplier_sku_code_snapshot",
            sa.String(length=100),
            nullable=True,
        ),
        sa.Column("unit_cost_price", sa.Numeric(18, 2), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("line_cost_amount", sa.Numeric(18, 2), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "length(trim(order_public_id_snapshot)) > 0",
            name="ck_supplier_settlement_items_order_id_nonblank",
        ),
        sa.CheckConstraint(
            "length(trim(product_public_id_snapshot)) > 0",
            name="ck_supplier_settlement_items_product_id_nonblank",
        ),
        sa.CheckConstraint(
            "length(trim(product_name_snapshot)) > 0",
            name="ck_supplier_settlement_items_product_name_nonblank",
        ),
        sa.CheckConstraint(
            "length(trim(sku_code_snapshot)) > 0 AND "
            "length(trim(sku_name_snapshot)) > 0",
            name="ck_supplier_settlement_items_sku_snapshot_nonblank",
        ),
        sa.CheckConstraint(
            "length(trim(supplier_public_id_snapshot)) > 0 AND "
            "length(trim(supplier_name_snapshot)) > 0",
            name="ck_supplier_settlement_items_supplier_snapshot_nonblank",
        ),
        sa.CheckConstraint(
            "unit_cost_price >= 0",
            name="ck_supplier_settlement_items_unit_cost_nonnegative",
        ),
        sa.CheckConstraint(
            "quantity > 0",
            name="ck_supplier_settlement_items_quantity_positive",
        ),
        sa.CheckConstraint(
            "line_cost_amount = unit_cost_price * quantity",
            name="ck_supplier_settlement_items_cost_arithmetic",
        ),
        sa.ForeignKeyConstraint(
            ["settlement_batch_id"], ["supplier_settlement_batches.id"]
        ),
        sa.ForeignKeyConstraint(["supplier_id"], ["suppliers.id"]),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"]),
        sa.ForeignKeyConstraint(["order_item_id"], ["order_items.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    for column, unique in (
        ("id", False),
        ("settlement_batch_id", False),
        ("supplier_id", False),
        ("order_id", False),
        ("order_item_id", True),
        ("order_completed_at", False),
    ):
        op.create_index(
            f"ix_supplier_settlement_items_{column}",
            "supplier_settlement_items",
            [column],
            unique=unique,
        )


def downgrade() -> None:
    """Drop settlement tables only when no settlement evidence exists."""
    if not context.is_offline_mode():
        connection = op.get_bind()
        item_count = connection.execute(sa.text(
            "SELECT COUNT(*) FROM supplier_settlement_items"
        )).scalar_one()
        batch_count = connection.execute(sa.text(
            "SELECT COUNT(*) FROM supplier_settlement_batches"
        )).scalar_one()
        if item_count or batch_count:
            raise RuntimeError(
                "存在供应商结算批次或明细证据，"
                "不能安全降级到 0010_order_refund_recovery"
            )

    op.drop_table("supplier_settlement_items")
    op.drop_table("supplier_settlement_batches")
