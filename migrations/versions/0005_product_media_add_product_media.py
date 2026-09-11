"""add product media

Revision ID: 0005_product_media
Revises: 0004_catalog_foundation
Create Date: 2026-09-10 12:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0005_product_media"
down_revision: Union[str, Sequence[str], None] = (
    "0004_catalog_foundation"
)
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create separately stored product main/carousel/detail images."""
    op.create_table(
        "product_media",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=False),
        sa.Column(
            "media_role",
            sa.String(length=30),
            server_default="DETAIL",
            nullable=False,
        ),
        sa.Column(
            "image_path",
            sa.String(length=500),
            nullable=False,
        ),
        sa.Column("alt_text", sa.String(length=200), nullable=True),
        sa.Column(
            "sort_order",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "is_active",
            sa.Boolean(),
            server_default=sa.true(),
            nullable=False,
        ),
        sa.Column("uploaded_by_id", sa.Integer(), nullable=False),
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
            "length(trim(image_path)) > 0",
            name="ck_product_media_path_nonblank",
        ),
        sa.CheckConstraint(
            "media_role IN ('MAIN', 'CAROUSEL', 'DETAIL')",
            name="ck_product_media_role",
        ),
        sa.CheckConstraint(
            "sort_order >= 0",
            name="ck_product_media_sort_nonnegative",
        ),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"]),
        sa.ForeignKeyConstraint(["uploaded_by_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("product_media", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_product_media_id"),
            ["id"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_product_media_product_id"),
            ["product_id"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_product_media_media_role"),
            ["media_role"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_product_media_image_path"),
            ["image_path"],
            unique=True,
        )
        batch_op.create_index(
            batch_op.f("ix_product_media_sort_order"),
            ["sort_order"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_product_media_is_active"),
            ["is_active"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_product_media_uploaded_by_id"),
            ["uploaded_by_id"],
            unique=False,
        )
    op.create_index(
        "uq_product_media_one_main_per_product",
        "product_media",
        ["product_id"],
        unique=True,
        sqlite_where=sa.text("media_role = 'MAIN'"),
        postgresql_where=sa.text("media_role = 'MAIN'"),
    )


def downgrade() -> None:
    """Remove the empty product media foundation on disposable databases."""
    op.drop_index(
        "uq_product_media_one_main_per_product",
        table_name="product_media",
    )
    with op.batch_alter_table("product_media", schema=None) as batch_op:
        batch_op.drop_index(
            batch_op.f("ix_product_media_uploaded_by_id")
        )
        batch_op.drop_index(batch_op.f("ix_product_media_is_active"))
        batch_op.drop_index(batch_op.f("ix_product_media_sort_order"))
        batch_op.drop_index(batch_op.f("ix_product_media_image_path"))
        batch_op.drop_index(batch_op.f("ix_product_media_media_role"))
        batch_op.drop_index(batch_op.f("ix_product_media_product_id"))
        batch_op.drop_index(batch_op.f("ix_product_media_id"))
    op.drop_table("product_media")
