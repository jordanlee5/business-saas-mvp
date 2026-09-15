"""add order reservation

Revision ID: 0008_order_reservation
Revises: 0007_order_foundation
Create Date: 2026-09-15 16:30:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0008_order_reservation"
down_revision: Union[str, Sequence[str], None] = "0007_order_foundation"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _backfill_order_idempotency_keys() -> None:
    # SQLite 与 PostgreSQL 均支持 ``||`` 字符串拼接；单条 SQL 也能
    # 在 Alembic 离线模式生成可执行迁移脚本。
    op.execute(sa.text(
        "UPDATE orders SET idempotency_key = "
        "'legacy-order:' || CAST(id AS VARCHAR(32)) "
        "WHERE idempotency_key IS NULL"
    ))


def upgrade() -> None:
    """Add order idempotency and auditable reserved-stock movements."""
    with op.batch_alter_table("order_items", schema=None) as batch_op:
        batch_op.add_column(sa.Column(
            "product_image_path_snapshot",
            sa.String(length=500),
            nullable=True,
        ))

    with op.batch_alter_table("orders", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("idempotency_key", sa.String(length=128), nullable=True)
        )
    _backfill_order_idempotency_keys()
    with op.batch_alter_table("orders", schema=None) as batch_op:
        batch_op.alter_column(
            "idempotency_key",
            existing_type=sa.String(length=128),
            nullable=False,
        )
        batch_op.create_check_constraint(
            "ck_orders_idempotency_key_nonblank",
            "length(trim(idempotency_key)) > 0",
        )
        batch_op.create_index(
            "ix_orders_idempotency_key",
            ["idempotency_key"],
            unique=True,
        )

    with op.batch_alter_table("inventory_movements", schema=None) as batch_op:
        batch_op.add_column(sa.Column(
            "reserved_quantity_delta",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ))
        batch_op.add_column(sa.Column(
            "reserved_quantity_before",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ))
        batch_op.add_column(sa.Column(
            "reserved_quantity_after",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ))
        batch_op.add_column(sa.Column(
            "actor_member_id",
            sa.Integer(),
            nullable=True,
        ))
        batch_op.add_column(sa.Column(
            "reference_type",
            sa.String(length=50),
            nullable=True,
        ))
        batch_op.add_column(sa.Column(
            "reference_id",
            sa.String(length=64),
            nullable=True,
        ))
        batch_op.alter_column(
            "actor_admin_id",
            existing_type=sa.Integer(),
            nullable=True,
        )
        batch_op.drop_constraint(
            "ck_inventory_movements_type",
            type_="check",
        )
        batch_op.drop_constraint(
            "ck_inventory_movements_delta_nonzero",
            type_="check",
        )
        batch_op.create_check_constraint(
            "ck_inventory_movements_type",
            "movement_type IN ('RECEIPT', 'ADJUSTMENT', 'RESERVE', "
            "'RELEASE', 'OUTBOUND', 'RETURN')",
        )
        batch_op.create_check_constraint(
            "ck_inventory_movements_delta_nonzero",
            "quantity_delta <> 0 OR reserved_quantity_delta <> 0",
        )
        batch_op.create_check_constraint(
            "ck_inventory_movements_reserved_before_nonnegative",
            "reserved_quantity_before >= 0",
        )
        batch_op.create_check_constraint(
            "ck_inventory_movements_reserved_after_nonnegative",
            "reserved_quantity_after >= 0",
        )
        batch_op.create_check_constraint(
            "ck_inventory_movements_reserved_arithmetic",
            "reserved_quantity_after = reserved_quantity_before + "
            "reserved_quantity_delta",
        )
        batch_op.create_check_constraint(
            "ck_inventory_movements_reserved_within_on_hand",
            "reserved_quantity_before <= quantity_before AND "
            "reserved_quantity_after <= quantity_after",
        )
        batch_op.create_check_constraint(
            "ck_inventory_movements_single_actor",
            "(actor_admin_id IS NOT NULL AND actor_member_id IS NULL) OR "
            "(actor_admin_id IS NULL AND actor_member_id IS NOT NULL)",
        )
        batch_op.create_check_constraint(
            "ck_inventory_movements_order_reference",
            "movement_type IN ('RECEIPT', 'ADJUSTMENT') OR "
            "(reference_type IS NOT NULL AND "
            "length(trim(reference_type)) > 0 AND reference_id IS NOT NULL "
            "AND length(trim(reference_id)) > 0)",
        )
        batch_op.create_foreign_key(
            "fk_inventory_movements_actor_member_id_members",
            "members",
            ["actor_member_id"],
            ["id"],
        )
        batch_op.create_index(
            "ix_inventory_movements_actor_member_id",
            ["actor_member_id"],
            unique=False,
        )


def downgrade() -> None:
    """Return to 0007 only when no order-reservation movement exists."""
    reservation_count = op.get_bind().execute(sa.text(
        "SELECT COUNT(*) FROM inventory_movements "
        "WHERE actor_member_id IS NOT NULL "
        "OR reserved_quantity_delta <> 0 "
        "OR movement_type NOT IN ('RECEIPT', 'ADJUSTMENT')"
    )).scalar_one()
    if reservation_count:
        raise RuntimeError(
            "存在订单库存预占流水，不能安全降级到 0007_order_foundation"
        )

    with op.batch_alter_table("inventory_movements", schema=None) as batch_op:
        batch_op.drop_index("ix_inventory_movements_actor_member_id")
        batch_op.drop_constraint(
            "fk_inventory_movements_actor_member_id_members",
            type_="foreignkey",
        )
        for constraint_name in (
            "ck_inventory_movements_order_reference",
            "ck_inventory_movements_single_actor",
            "ck_inventory_movements_reserved_within_on_hand",
            "ck_inventory_movements_reserved_arithmetic",
            "ck_inventory_movements_reserved_after_nonnegative",
            "ck_inventory_movements_reserved_before_nonnegative",
            "ck_inventory_movements_delta_nonzero",
            "ck_inventory_movements_type",
        ):
            batch_op.drop_constraint(constraint_name, type_="check")
        batch_op.create_check_constraint(
            "ck_inventory_movements_type",
            "movement_type IN ('RECEIPT', 'ADJUSTMENT')",
        )
        batch_op.create_check_constraint(
            "ck_inventory_movements_delta_nonzero",
            "quantity_delta <> 0",
        )
        batch_op.alter_column(
            "actor_admin_id",
            existing_type=sa.Integer(),
            nullable=False,
        )
        batch_op.drop_column("reference_id")
        batch_op.drop_column("reference_type")
        batch_op.drop_column("actor_member_id")
        batch_op.drop_column("reserved_quantity_after")
        batch_op.drop_column("reserved_quantity_before")
        batch_op.drop_column("reserved_quantity_delta")

    with op.batch_alter_table("orders", schema=None) as batch_op:
        batch_op.drop_index("ix_orders_idempotency_key")
        batch_op.drop_constraint(
            "ck_orders_idempotency_key_nonblank",
            type_="check",
        )
        batch_op.drop_column("idempotency_key")

    with op.batch_alter_table("order_items", schema=None) as batch_op:
        batch_op.drop_column("product_image_path_snapshot")
