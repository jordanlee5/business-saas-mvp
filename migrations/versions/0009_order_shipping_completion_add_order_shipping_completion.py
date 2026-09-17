"""add order shipping and completion evidence

Revision ID: 0009_order_shipping_completion
Revises: 0008_order_reservation
Create Date: 2026-09-17 12:30:00

"""
from typing import Sequence, Union

from alembic import context, op
import sqlalchemy as sa


revision: str = "0009_order_shipping_completion"
down_revision: Union[str, Sequence[str], None] = "0008_order_reservation"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add manual shipping evidence and an order completion timestamp."""
    if not context.is_offline_mode():
        legacy_lifecycle_count = op.get_bind().execute(sa.text(
            "SELECT COUNT(*) FROM orders "
            "WHERE status IN ('SHIPPED', 'COMPLETED')"
        )).scalar_one()
        if legacy_lifecycle_count:
            raise RuntimeError(
                "存在缺少物流证据的已发货或已完成订单，"
                "不能自动升级到 0009_order_shipping_completion"
            )

    with op.batch_alter_table("orders", schema=None) as batch_op:
        batch_op.add_column(sa.Column(
            "shipping_carrier",
            sa.String(length=100),
            nullable=True,
        ))
        batch_op.add_column(sa.Column(
            "tracking_number",
            sa.String(length=100),
            nullable=True,
        ))
        batch_op.add_column(sa.Column(
            "shipped_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ))
        batch_op.add_column(sa.Column(
            "completed_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ))
        batch_op.create_check_constraint(
            "ck_orders_shipping_evidence_complete",
            "(shipping_carrier IS NULL AND tracking_number IS NULL "
            "AND shipped_at IS NULL) OR "
            "(shipping_carrier IS NOT NULL AND "
            "length(trim(shipping_carrier)) > 0 AND "
            "tracking_number IS NOT NULL AND "
            "length(trim(tracking_number)) > 0 AND "
            "shipped_at IS NOT NULL)",
        )
        batch_op.create_check_constraint(
            "ck_orders_shipped_status_has_evidence",
            "status NOT IN ('SHIPPED', 'COMPLETED') OR "
            "shipped_at IS NOT NULL",
        )
        batch_op.create_check_constraint(
            "ck_orders_shipping_evidence_matches_status",
            "shipping_carrier IS NULL OR "
            "status IN ('SHIPPED', 'COMPLETED')",
        )
        batch_op.create_check_constraint(
            "ck_orders_completed_status_has_time",
            "status != 'COMPLETED' OR completed_at IS NOT NULL",
        )
        batch_op.create_check_constraint(
            "ck_orders_completion_matches_status",
            "completed_at IS NULL OR status = 'COMPLETED'",
        )
        batch_op.create_check_constraint(
            "ck_orders_completion_requires_shipping",
            "completed_at IS NULL OR shipped_at IS NOT NULL",
        )
        batch_op.create_check_constraint(
            "ck_orders_completion_time_order",
            "completed_at IS NULL OR completed_at >= shipped_at",
        )
        batch_op.create_unique_constraint(
            "uq_orders_carrier_tracking",
            ["shipping_carrier", "tracking_number"],
        )
        batch_op.create_index(
            "ix_orders_tracking_number",
            ["tracking_number"],
            unique=False,
        )
        batch_op.create_index(
            "ix_orders_shipped_at",
            ["shipped_at"],
            unique=False,
        )
        batch_op.create_index(
            "ix_orders_completed_at",
            ["completed_at"],
            unique=False,
        )


def downgrade() -> None:
    """Remove lifecycle fields only when no shipping evidence would be lost."""
    lifecycle_count = op.get_bind().execute(sa.text(
        "SELECT COUNT(*) FROM orders WHERE "
        "status IN ('SHIPPED', 'COMPLETED') OR "
        "shipping_carrier IS NOT NULL OR tracking_number IS NOT NULL OR "
        "shipped_at IS NOT NULL OR completed_at IS NOT NULL"
    )).scalar_one()
    if lifecycle_count:
        raise RuntimeError(
            "存在订单发货或完成证据，"
            "不能安全降级到 0008_order_reservation"
        )

    with op.batch_alter_table("orders", schema=None) as batch_op:
        batch_op.drop_index("ix_orders_completed_at")
        batch_op.drop_index("ix_orders_shipped_at")
        batch_op.drop_index("ix_orders_tracking_number")
        batch_op.drop_constraint(
            "uq_orders_carrier_tracking",
            type_="unique",
        )
        for constraint_name in (
            "ck_orders_completion_time_order",
            "ck_orders_completion_requires_shipping",
            "ck_orders_completion_matches_status",
            "ck_orders_completed_status_has_time",
            "ck_orders_shipping_evidence_matches_status",
            "ck_orders_shipped_status_has_evidence",
            "ck_orders_shipping_evidence_complete",
        ):
            batch_op.drop_constraint(constraint_name, type_="check")
        batch_op.drop_column("completed_at")
        batch_op.drop_column("shipped_at")
        batch_op.drop_column("tracking_number")
        batch_op.drop_column("shipping_carrier")
