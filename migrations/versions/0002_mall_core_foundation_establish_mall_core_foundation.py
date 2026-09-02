"""establish mall core foundation

Revision ID: 0002_mall_core_foundation
Revises: 0001_current_schema_baseline
Create Date: 2026-09-02 03:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0002_mall_core_foundation"
down_revision: Union[str, Sequence[str], None] = (
    "0001_current_schema_baseline"
)
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add channel snapshots and the confirmed member/points core."""
    with op.batch_alter_table(
        "upload_batches",
        schema=None,
    ) as batch_op:
        batch_op.add_column(
            sa.Column(
                "redemption_mode",
                sa.String(length=30),
                server_default="CASH_REBATE",
                nullable=False,
            )
        )
        batch_op.add_column(
            sa.Column(
                "claim_deadline",
                sa.DateTime(timezone=True),
                nullable=True,
            )
        )
        batch_op.create_check_constraint(
            "ck_upload_batches_redemption_mode",
            "redemption_mode IN "
            "('CASH_REBATE', 'MALL_REDEMPTION')",
        )
        batch_op.create_index(
            batch_op.f(
                "ix_upload_batches_redemption_mode"
            ),
            ["redemption_mode"],
            unique=False,
        )

    with op.batch_alter_table(
        "business_records",
        schema=None,
    ) as batch_op:
        batch_op.add_column(
            sa.Column(
                "redemption_mode",
                sa.String(length=30),
                server_default="CASH_REBATE",
                nullable=False,
            )
        )
        batch_op.add_column(
            sa.Column(
                "claim_status",
                sa.String(length=30),
                nullable=True,
            )
        )
        batch_op.create_check_constraint(
            "ck_business_records_redemption_mode",
            "redemption_mode IN "
            "('CASH_REBATE', 'MALL_REDEMPTION')",
        )
        batch_op.create_check_constraint(
            "ck_business_records_claim_status",
            "(redemption_mode = 'CASH_REBATE' "
            "AND claim_status IS NULL) OR "
            "(redemption_mode = 'MALL_REDEMPTION' "
            "AND claim_status IS NOT NULL "
            "AND claim_status IN "
            "('PENDING_ACTIVATION', 'ACTIVATED', "
            "'EXPIRED', 'FROZEN'))",
        )
        batch_op.create_index(
            batch_op.f(
                "ix_business_records_redemption_mode"
            ),
            ["redemption_mode"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f(
                "ix_business_records_claim_status"
            ),
            ["claim_status"],
            unique=False,
        )

    op.create_table(
        "members",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column(
            "member_public_id",
            sa.String(length=32),
            nullable=False,
        ),
        sa.Column(
            "is_active",
            sa.Boolean(),
            server_default=sa.true(),
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
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table(
        "members",
        schema=None,
    ) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_members_id"),
            ["id"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_members_is_active"),
            ["is_active"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_members_member_public_id"),
            ["member_public_id"],
            unique=True,
        )

    op.create_table(
        "member_wechat_bindings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("member_id", sa.Integer(), nullable=False),
        sa.Column(
            "wechat_app_id",
            sa.String(length=64),
            nullable=False,
        ),
        sa.Column(
            "openid",
            sa.String(length=128),
            nullable=False,
        ),
        sa.Column(
            "unionid",
            sa.String(length=128),
            nullable=True,
        ),
        sa.Column(
            "bound_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "last_login_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.ForeignKeyConstraint(
            ["member_id"],
            ["members.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "wechat_app_id",
            "openid",
            name="uq_member_wechat_bindings_app_openid",
        ),
    )
    with op.batch_alter_table(
        "member_wechat_bindings",
        schema=None,
    ) as batch_op:
        batch_op.create_index(
            batch_op.f(
                "ix_member_wechat_bindings_id"
            ),
            ["id"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f(
                "ix_member_wechat_bindings_member_id"
            ),
            ["member_id"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f(
                "ix_member_wechat_bindings_unionid"
            ),
            ["unionid"],
            unique=False,
        )

    op.create_table(
        "points_accounts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("member_id", sa.Integer(), nullable=False),
        sa.Column(
            "available_points",
            sa.Numeric(precision=18, scale=2),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "reserved_points",
            sa.Numeric(precision=18, scale=2),
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
            "available_points >= 0",
            name="ck_points_accounts_available_nonnegative",
        ),
        sa.CheckConstraint(
            "reserved_points >= 0",
            name="ck_points_accounts_reserved_nonnegative",
        ),
        sa.CheckConstraint(
            "version >= 0",
            name="ck_points_accounts_version_nonnegative",
        ),
        sa.ForeignKeyConstraint(
            ["member_id"],
            ["members.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table(
        "points_accounts",
        schema=None,
    ) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_points_accounts_id"),
            ["id"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f(
                "ix_points_accounts_member_id"
            ),
            ["member_id"],
            unique=True,
        )

    op.create_table(
        "points_grants",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column(
            "business_record_id",
            sa.Integer(),
            nullable=False,
        ),
        sa.Column(
            "granted_points",
            sa.Numeric(precision=18, scale=2),
            nullable=False,
        ),
        sa.Column(
            "available_points",
            sa.Numeric(precision=18, scale=2),
            nullable=False,
        ),
        sa.Column(
            "reserved_points",
            sa.Numeric(precision=18, scale=2),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "activated_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "expires_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.String(length=30),
            server_default="ACTIVE",
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
            "available_points >= 0",
            name="ck_points_grants_available_nonnegative",
        ),
        sa.CheckConstraint(
            "available_points + reserved_points <= granted_points",
            name="ck_points_grants_balance_within_grant",
        ),
        sa.CheckConstraint(
            "expires_at > activated_at",
            name="ck_points_grants_expiry_after_activation",
        ),
        sa.CheckConstraint(
            "granted_points > 0",
            name="ck_points_grants_granted_positive",
        ),
        sa.CheckConstraint(
            "reserved_points >= 0",
            name="ck_points_grants_reserved_nonnegative",
        ),
        sa.CheckConstraint(
            "status IN "
            "('ACTIVE', 'EXHAUSTED', 'EXPIRED', 'FROZEN')",
            name="ck_points_grants_status",
        ),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["points_accounts.id"],
        ),
        sa.ForeignKeyConstraint(
            ["business_record_id"],
            ["business_records.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table(
        "points_grants",
        schema=None,
    ) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_points_grants_account_id"),
            ["account_id"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f(
                "ix_points_grants_business_record_id"
            ),
            ["business_record_id"],
            unique=True,
        )
        batch_op.create_index(
            batch_op.f("ix_points_grants_expires_at"),
            ["expires_at"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_points_grants_id"),
            ["id"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_points_grants_status"),
            ["status"],
            unique=False,
        )

    op.create_table(
        "points_ledger_entries",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("grant_id", sa.Integer(), nullable=False),
        sa.Column(
            "entry_type",
            sa.String(length=30),
            nullable=False,
        ),
        sa.Column(
            "available_points_delta",
            sa.Numeric(precision=18, scale=2),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "reserved_points_delta",
            sa.Numeric(precision=18, scale=2),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "idempotency_key",
            sa.String(length=100),
            nullable=False,
        ),
        sa.Column(
            "reference_type",
            sa.String(length=50),
            nullable=True,
        ),
        sa.Column(
            "reference_id",
            sa.String(length=64),
            nullable=True,
        ),
        sa.Column(
            "actor_admin_id",
            sa.Integer(),
            nullable=True,
        ),
        sa.Column(
            "reason",
            sa.String(length=500),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.CheckConstraint(
            "entry_type <> 'ADJUST' OR "
            "(actor_admin_id IS NOT NULL AND reason IS NOT NULL "
            "AND length(trim(reason)) > 0)",
            name="ck_points_ledger_entries_adjustment_audit",
        ),
        sa.CheckConstraint(
            "available_points_delta <> 0 "
            "OR reserved_points_delta <> 0",
            name="ck_points_ledger_entries_nonzero_delta",
        ),
        sa.CheckConstraint(
            "entry_type IN "
            "('GRANT', 'RESERVE', 'RELEASE', 'CONSUME', "
            "'REFUND', 'EXPIRE', 'ADJUST')",
            name="ck_points_ledger_entries_type",
        ),
        sa.ForeignKeyConstraint(
            ["actor_admin_id"],
            ["users.id"],
        ),
        sa.ForeignKeyConstraint(
            ["grant_id"],
            ["points_grants.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table(
        "points_ledger_entries",
        schema=None,
    ) as batch_op:
        batch_op.create_index(
            batch_op.f(
                "ix_points_ledger_entries_actor_admin_id"
            ),
            ["actor_admin_id"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f(
                "ix_points_ledger_entries_entry_type"
            ),
            ["entry_type"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f(
                "ix_points_ledger_entries_grant_id"
            ),
            ["grant_id"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_points_ledger_entries_id"),
            ["id"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f(
                "ix_points_ledger_entries_idempotency_key"
            ),
            ["idempotency_key"],
            unique=True,
        )


def downgrade() -> None:
    """Remove the mall core; use only on disposable databases."""
    with op.batch_alter_table(
        "points_ledger_entries",
        schema=None,
    ) as batch_op:
        batch_op.drop_index(
            batch_op.f(
                "ix_points_ledger_entries_idempotency_key"
            )
        )
        batch_op.drop_index(
            batch_op.f("ix_points_ledger_entries_id")
        )
        batch_op.drop_index(
            batch_op.f(
                "ix_points_ledger_entries_grant_id"
            )
        )
        batch_op.drop_index(
            batch_op.f(
                "ix_points_ledger_entries_entry_type"
            )
        )
        batch_op.drop_index(
            batch_op.f(
                "ix_points_ledger_entries_actor_admin_id"
            )
        )
    op.drop_table("points_ledger_entries")

    with op.batch_alter_table(
        "points_grants",
        schema=None,
    ) as batch_op:
        batch_op.drop_index(
            batch_op.f("ix_points_grants_status")
        )
        batch_op.drop_index(
            batch_op.f("ix_points_grants_id")
        )
        batch_op.drop_index(
            batch_op.f("ix_points_grants_expires_at")
        )
        batch_op.drop_index(
            batch_op.f(
                "ix_points_grants_business_record_id"
            )
        )
        batch_op.drop_index(
            batch_op.f("ix_points_grants_account_id")
        )
    op.drop_table("points_grants")

    with op.batch_alter_table(
        "points_accounts",
        schema=None,
    ) as batch_op:
        batch_op.drop_index(
            batch_op.f("ix_points_accounts_member_id")
        )
        batch_op.drop_index(
            batch_op.f("ix_points_accounts_id")
        )
    op.drop_table("points_accounts")

    with op.batch_alter_table(
        "member_wechat_bindings",
        schema=None,
    ) as batch_op:
        batch_op.drop_index(
            batch_op.f(
                "ix_member_wechat_bindings_unionid"
            )
        )
        batch_op.drop_index(
            batch_op.f(
                "ix_member_wechat_bindings_member_id"
            )
        )
        batch_op.drop_index(
            batch_op.f(
                "ix_member_wechat_bindings_id"
            )
        )
    op.drop_table("member_wechat_bindings")

    with op.batch_alter_table(
        "members",
        schema=None,
    ) as batch_op:
        batch_op.drop_index(
            batch_op.f("ix_members_member_public_id")
        )
        batch_op.drop_index(
            batch_op.f("ix_members_is_active")
        )
        batch_op.drop_index(
            batch_op.f("ix_members_id")
        )
    op.drop_table("members")

    with op.batch_alter_table(
        "business_records",
        schema=None,
    ) as batch_op:
        batch_op.drop_index(
            batch_op.f(
                "ix_business_records_claim_status"
            )
        )
        batch_op.drop_index(
            batch_op.f(
                "ix_business_records_redemption_mode"
            )
        )
        batch_op.drop_constraint(
            "ck_business_records_claim_status",
            type_="check",
        )
        batch_op.drop_constraint(
            "ck_business_records_redemption_mode",
            type_="check",
        )
        batch_op.drop_column("claim_status")
        batch_op.drop_column("redemption_mode")

    with op.batch_alter_table(
        "upload_batches",
        schema=None,
    ) as batch_op:
        batch_op.drop_index(
            batch_op.f(
                "ix_upload_batches_redemption_mode"
            )
        )
        batch_op.drop_constraint(
            "ck_upload_batches_redemption_mode",
            type_="check",
        )
        batch_op.drop_column("claim_deadline")
        batch_op.drop_column("redemption_mode")
