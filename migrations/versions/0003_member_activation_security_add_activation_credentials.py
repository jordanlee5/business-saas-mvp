"""add member activation security foundation

Revision ID: 0003_member_activation_security
Revises: 0002_mall_core_foundation
Create Date: 2026-09-03 17:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0003_member_activation_security"
down_revision: Union[str, Sequence[str], None] = (
    "0002_mall_core_foundation"
)
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add hashed, one-time activation credentials for mall records."""
    op.create_table(
        "member_activation_credentials",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column(
            "business_record_id",
            sa.Integer(),
            nullable=False,
        ),
        sa.Column(
            "security_method",
            sa.String(length=30),
            server_default="ONE_TIME_CODE",
            nullable=False,
        ),
        sa.Column(
            "secret_algorithm",
            sa.String(length=30),
            server_default="PBKDF2_SHA256",
            nullable=False,
        ),
        sa.Column(
            "secret_iterations",
            sa.Integer(),
            server_default="600000",
            nullable=False,
        ),
        sa.Column(
            "secret_salt",
            sa.String(length=64),
            nullable=False,
        ),
        sa.Column(
            "secret_digest",
            sa.String(length=64),
            nullable=False,
        ),
        sa.Column(
            "failed_attempts",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "max_attempts",
            sa.Integer(),
            server_default="5",
            nullable=False,
        ),
        sa.Column(
            "issue_version",
            sa.Integer(),
            server_default="1",
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.String(length=20),
            server_default="ACTIVE",
            nullable=False,
        ),
        sa.Column(
            "issued_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "expires_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "used_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "locked_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "revoked_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.CheckConstraint(
            "failed_attempts >= 0 AND failed_attempts <= max_attempts",
            name="ck_member_activation_credentials_attempts",
        ),
        sa.CheckConstraint(
            "expires_at > issued_at",
            name="ck_member_activation_credentials_expiry",
        ),
        sa.CheckConstraint(
            "secret_iterations >= 100000 "
            "AND secret_iterations <= 2000000",
            name="ck_member_activation_credentials_iterations",
        ),
        sa.CheckConstraint(
            "length(secret_salt) = 32 "
            "AND length(secret_digest) = 64",
            name="ck_member_activation_credentials_hash_shape",
        ),
        sa.CheckConstraint(
            "issue_version >= 1",
            name="ck_member_activation_credentials_issue_version",
        ),
        sa.CheckConstraint(
            "max_attempts >= 1 AND max_attempts <= 10",
            name="ck_member_activation_credentials_max_attempts",
        ),
        sa.CheckConstraint(
            "security_method IN ('ONE_TIME_CODE', 'SMS_OTP')",
            name="ck_member_activation_credentials_method",
        ),
        sa.CheckConstraint(
            "status IN "
            "('ACTIVE', 'USED', 'LOCKED', 'EXPIRED', 'REVOKED')",
            name="ck_member_activation_credentials_status",
        ),
        sa.CheckConstraint(
            "status <> 'USED' OR used_at IS NOT NULL",
            name="ck_member_activation_credentials_used_time",
        ),
        sa.CheckConstraint(
            "status <> 'LOCKED' OR locked_at IS NOT NULL",
            name="ck_member_activation_credentials_locked_time",
        ),
        sa.CheckConstraint(
            "status <> 'REVOKED' OR revoked_at IS NOT NULL",
            name="ck_member_activation_credentials_revoked_time",
        ),
        sa.ForeignKeyConstraint(
            ["business_record_id"],
            ["business_records.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table(
        "member_activation_credentials",
        schema=None,
    ) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_member_activation_credentials_id"),
            ["id"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f(
                "ix_member_activation_credentials_business_record_id"
            ),
            ["business_record_id"],
            unique=True,
        )
        batch_op.create_index(
            batch_op.f(
                "ix_member_activation_credentials_security_method"
            ),
            ["security_method"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_member_activation_credentials_status"),
            ["status"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_member_activation_credentials_expires_at"),
            ["expires_at"],
            unique=False,
        )


def downgrade() -> None:
    """Remove activation credentials on disposable databases only."""
    with op.batch_alter_table(
        "member_activation_credentials",
        schema=None,
    ) as batch_op:
        batch_op.drop_index(
            batch_op.f("ix_member_activation_credentials_expires_at")
        )
        batch_op.drop_index(
            batch_op.f("ix_member_activation_credentials_status")
        )
        batch_op.drop_index(
            batch_op.f(
                "ix_member_activation_credentials_security_method"
            )
        )
        batch_op.drop_index(
            batch_op.f(
                "ix_member_activation_credentials_business_record_id"
            )
        )
        batch_op.drop_index(
            batch_op.f("ix_member_activation_credentials_id")
        )
    op.drop_table("member_activation_credentials")
