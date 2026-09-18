"""add order refund recovery evidence

Revision ID: 0010_order_refund_recovery
Revises: 0009_order_shipping_completion
Create Date: 2026-09-17 17:30:00

"""
from typing import Sequence, Union

from alembic import context, op
import sqlalchemy as sa


revision: str = "0010_order_refund_recovery"
down_revision: Union[str, Sequence[str], None] = (
    "0009_order_shipping_completion"
)
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _drop_lifecycle_constraints(batch_op) -> None:
    for constraint_name in (
        "ck_orders_completion_matches_status",
        "ck_orders_completed_status_has_time",
        "ck_orders_shipping_evidence_matches_status",
        "ck_orders_shipped_status_has_evidence",
    ):
        batch_op.drop_constraint(constraint_name, type_="check")


def _create_refund_aware_lifecycle_constraints(batch_op) -> None:
    batch_op.create_check_constraint(
        "ck_orders_shipped_status_has_evidence",
        "status NOT IN ('SHIPPED', 'COMPLETED', 'REFUNDED') OR "
        "shipped_at IS NOT NULL",
    )
    batch_op.create_check_constraint(
        "ck_orders_shipping_evidence_matches_status",
        "shipping_carrier IS NULL OR "
        "status IN ('SHIPPED', 'COMPLETED', 'REFUNDED')",
    )
    batch_op.create_check_constraint(
        "ck_orders_completed_status_has_time",
        "status NOT IN ('COMPLETED', 'REFUNDED') OR "
        "completed_at IS NOT NULL",
    )
    batch_op.create_check_constraint(
        "ck_orders_completion_matches_status",
        "completed_at IS NULL OR status IN ('COMPLETED', 'REFUNDED')",
    )


def _create_pre_refund_lifecycle_constraints(batch_op) -> None:
    batch_op.create_check_constraint(
        "ck_orders_shipped_status_has_evidence",
        "status NOT IN ('SHIPPED', 'COMPLETED') OR shipped_at IS NOT NULL",
    )
    batch_op.create_check_constraint(
        "ck_orders_shipping_evidence_matches_status",
        "shipping_carrier IS NULL OR status IN ('SHIPPED', 'COMPLETED')",
    )
    batch_op.create_check_constraint(
        "ck_orders_completed_status_has_time",
        "status != 'COMPLETED' OR completed_at IS NOT NULL",
    )
    batch_op.create_check_constraint(
        "ck_orders_completion_matches_status",
        "completed_at IS NULL OR status = 'COMPLETED'",
    )


def upgrade() -> None:
    """Add auditable evidence for completed-order refund recovery."""
    if not context.is_offline_mode():
        legacy_refund_count = op.get_bind().execute(sa.text(
            "SELECT COUNT(*) FROM orders WHERE status = 'REFUNDED'"
        )).scalar_one()
        if legacy_refund_count:
            raise RuntimeError(
                "存在缺少积分、库存及退款审计证据的已退款订单，"
                "不能自动升级到 0010_order_refund_recovery"
            )

    with op.batch_alter_table("orders", schema=None) as batch_op:
        _drop_lifecycle_constraints(batch_op)
        batch_op.add_column(sa.Column(
            "refund_reason",
            sa.String(length=500),
            nullable=True,
        ))
        batch_op.add_column(sa.Column(
            "refunded_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ))
        _create_refund_aware_lifecycle_constraints(batch_op)
        batch_op.create_check_constraint(
            "ck_orders_refund_evidence_complete",
            "(refund_reason IS NULL AND refunded_at IS NULL) OR "
            "(refund_reason IS NOT NULL AND "
            "length(trim(refund_reason)) > 0 AND refunded_at IS NOT NULL)",
        )
        batch_op.create_check_constraint(
            "ck_orders_refunded_status_has_time",
            "status != 'REFUNDED' OR refunded_at IS NOT NULL",
        )
        batch_op.create_check_constraint(
            "ck_orders_refund_evidence_matches_status",
            "refund_reason IS NULL OR status = 'REFUNDED'",
        )
        batch_op.create_check_constraint(
            "ck_orders_refund_requires_completion",
            "refunded_at IS NULL OR completed_at IS NOT NULL",
        )
        batch_op.create_check_constraint(
            "ck_orders_refund_time_order",
            "refunded_at IS NULL OR refunded_at >= completed_at",
        )
        batch_op.create_index(
            "ix_orders_refunded_at",
            ["refunded_at"],
            unique=False,
        )


def downgrade() -> None:
    """Remove refund fields only when no recovery evidence would be lost."""
    refund_count = op.get_bind().execute(sa.text(
        "SELECT COUNT(*) FROM orders WHERE "
        "status = 'REFUNDED' OR refund_reason IS NOT NULL "
        "OR refunded_at IS NOT NULL"
    )).scalar_one()
    if refund_count:
        raise RuntimeError(
            "存在订单退款及资源恢复证据，"
            "不能安全降级到 0009_order_shipping_completion"
        )

    with op.batch_alter_table("orders", schema=None) as batch_op:
        batch_op.drop_index("ix_orders_refunded_at")
        for constraint_name in (
            "ck_orders_refund_time_order",
            "ck_orders_refund_requires_completion",
            "ck_orders_refund_evidence_matches_status",
            "ck_orders_refunded_status_has_time",
            "ck_orders_refund_evidence_complete",
        ):
            batch_op.drop_constraint(constraint_name, type_="check")
        _drop_lifecycle_constraints(batch_op)
        batch_op.drop_column("refunded_at")
        batch_op.drop_column("refund_reason")
        _create_pre_refund_lifecycle_constraints(batch_op)
