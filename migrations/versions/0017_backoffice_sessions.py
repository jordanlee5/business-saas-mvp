"""Shared, revocable back-office sessions. Existing users and business rows unchanged."""
from alembic import op
import sqlalchemy as sa

revision = "0017_backoffice_sessions"
down_revision = "0016_member_rights"
branch_labels = depends_on = None


def upgrade():
    op.create_table("backoffice_sessions",
        sa.Column("token_hash", sa.String(64), primary_key=True),
        sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("credential_hash", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.BigInteger, nullable=False))
    op.create_index("ix_backoffice_sessions_user_id", "backoffice_sessions", ["user_id"])
    op.create_index("ix_backoffice_sessions_expires_at", "backoffice_sessions", ["expires_at"])


def downgrade():
    # Sessions are disposable credentials, not business evidence. Downgrade logs everyone out.
    op.drop_table("backoffice_sessions")
