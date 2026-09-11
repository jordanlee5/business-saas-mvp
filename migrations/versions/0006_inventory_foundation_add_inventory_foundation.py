"""add inventory foundation

Revision ID: 0006_inventory_foundation
Revises: 0005_product_media
Create Date: 2026-09-11 12:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0006_inventory_foundation"
down_revision: Union[str, Sequence[str], None] = "0005_product_media"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create per-SKU balances and append-only inventory movements."""
    op.create_table(
        "inventory_balances",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("sku_id", sa.Integer(), nullable=False),
        sa.Column(
            "on_hand_quantity",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "reserved_quantity",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "version",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.CheckConstraint(
            "on_hand_quantity >= 0",
            name="ck_inventory_balances_on_hand_nonnegative",
        ),
        sa.CheckConstraint(
            "reserved_quantity >= 0",
            name="ck_inventory_balances_reserved_nonnegative",
        ),
        sa.CheckConstraint(
            "reserved_quantity <= on_hand_quantity",
            name="ck_inventory_balances_reserved_within_on_hand",
        ),
        sa.CheckConstraint(
            "version >= 0",
            name="ck_inventory_balances_version_nonnegative",
        ),
        sa.ForeignKeyConstraint(["sku_id"], ["product_skus.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("inventory_balances", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_inventory_balances_id"),
            ["id"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_inventory_balances_sku_id"),
            ["sku_id"],
            unique=True,
        )

    op.create_table(
        "inventory_movements",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column(
            "movement_public_id",
            sa.String(length=32),
            nullable=False,
        ),
        sa.Column("sku_id", sa.Integer(), nullable=False),
        sa.Column(
            "movement_type",
            sa.String(length=30),
            server_default="ADJUSTMENT",
            nullable=False,
        ),
        sa.Column("quantity_delta", sa.Integer(), nullable=False),
        sa.Column("quantity_before", sa.Integer(), nullable=False),
        sa.Column("quantity_after", sa.Integer(), nullable=False),
        sa.Column("balance_version", sa.Integer(), nullable=False),
        sa.Column(
            "idempotency_key",
            sa.String(length=128),
            nullable=False,
        ),
        sa.Column("reason", sa.String(length=500), nullable=False),
        sa.Column("actor_admin_id", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.CheckConstraint(
            "movement_type IN ('RECEIPT', 'ADJUSTMENT')",
            name="ck_inventory_movements_type",
        ),
        sa.CheckConstraint(
            "quantity_delta <> 0",
            name="ck_inventory_movements_delta_nonzero",
        ),
        sa.CheckConstraint(
            "quantity_before >= 0",
            name="ck_inventory_movements_before_nonnegative",
        ),
        sa.CheckConstraint(
            "quantity_after >= 0",
            name="ck_inventory_movements_after_nonnegative",
        ),
        sa.CheckConstraint(
            "quantity_after = quantity_before + quantity_delta",
            name="ck_inventory_movements_arithmetic",
        ),
        sa.CheckConstraint(
            "balance_version > 0",
            name="ck_inventory_movements_version_positive",
        ),
        sa.CheckConstraint(
            "length(trim(idempotency_key)) > 0",
            name="ck_inventory_movements_idempotency_nonblank",
        ),
        sa.CheckConstraint(
            "length(trim(reason)) > 0",
            name="ck_inventory_movements_reason_nonblank",
        ),
        sa.ForeignKeyConstraint(["actor_admin_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["sku_id"], ["product_skus.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "sku_id",
            "balance_version",
            name="uq_inventory_movements_sku_version",
        ),
    )
    with op.batch_alter_table("inventory_movements", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_inventory_movements_id"),
            ["id"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_inventory_movements_movement_public_id"),
            ["movement_public_id"],
            unique=True,
        )
        batch_op.create_index(
            batch_op.f("ix_inventory_movements_sku_id"),
            ["sku_id"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_inventory_movements_movement_type"),
            ["movement_type"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_inventory_movements_idempotency_key"),
            ["idempotency_key"],
            unique=True,
        )
        batch_op.create_index(
            batch_op.f("ix_inventory_movements_actor_admin_id"),
            ["actor_admin_id"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_inventory_movements_created_at"),
            ["created_at"],
            unique=False,
        )


def downgrade() -> None:
    """Remove the empty inventory foundation on disposable databases."""
    with op.batch_alter_table("inventory_movements", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_inventory_movements_created_at"))
        batch_op.drop_index(
            batch_op.f("ix_inventory_movements_actor_admin_id")
        )
        batch_op.drop_index(
            batch_op.f("ix_inventory_movements_idempotency_key")
        )
        batch_op.drop_index(
            batch_op.f("ix_inventory_movements_movement_type")
        )
        batch_op.drop_index(batch_op.f("ix_inventory_movements_sku_id"))
        batch_op.drop_index(
            batch_op.f("ix_inventory_movements_movement_public_id")
        )
        batch_op.drop_index(batch_op.f("ix_inventory_movements_id"))
    op.drop_table("inventory_movements")

    with op.batch_alter_table("inventory_balances", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_inventory_balances_sku_id"))
        batch_op.drop_index(batch_op.f("ix_inventory_balances_id"))
    op.drop_table("inventory_balances")
