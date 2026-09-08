"""add catalog foundation

Revision ID: 0004_catalog_foundation
Revises: 0003_member_activation_security
Create Date: 2026-09-08 18:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0004_catalog_foundation"
down_revision: Union[str, Sequence[str], None] = (
    "0003_member_activation_security"
)
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create category, supplier, product and SKU foundations."""
    op.create_table(
        "product_categories",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("slug", sa.String(length=80), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
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
            "length(trim(name)) > 0",
            name="ck_product_categories_name_nonblank",
        ),
        sa.CheckConstraint(
            "length(trim(slug)) > 0",
            name="ck_product_categories_slug_nonblank",
        ),
        sa.CheckConstraint(
            "sort_order >= 0",
            name="ck_product_categories_sort_nonnegative",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table(
        "product_categories",
        schema=None,
    ) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_product_categories_id"),
            ["id"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_product_categories_name"),
            ["name"],
            unique=True,
        )
        batch_op.create_index(
            batch_op.f("ix_product_categories_slug"),
            ["slug"],
            unique=True,
        )
        batch_op.create_index(
            batch_op.f("ix_product_categories_sort_order"),
            ["sort_order"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_product_categories_is_active"),
            ["is_active"],
            unique=False,
        )

    op.create_table(
        "suppliers",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column(
            "supplier_public_id",
            sa.String(length=32),
            nullable=False,
        ),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("contact_name", sa.String(length=80), nullable=True),
        sa.Column("contact_phone", sa.String(length=40), nullable=True),
        sa.Column("remark", sa.Text(), nullable=True),
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
        sa.CheckConstraint(
            "length(trim(supplier_public_id)) > 0",
            name="ck_suppliers_public_id_nonblank",
        ),
        sa.CheckConstraint(
            "length(trim(name)) > 0",
            name="ck_suppliers_name_nonblank",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table(
        "suppliers",
        schema=None,
    ) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_suppliers_id"),
            ["id"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_suppliers_supplier_public_id"),
            ["supplier_public_id"],
            unique=True,
        )
        batch_op.create_index(
            batch_op.f("ix_suppliers_name"),
            ["name"],
            unique=True,
        )
        batch_op.create_index(
            batch_op.f("ix_suppliers_is_active"),
            ["is_active"],
            unique=False,
        )

    op.create_table(
        "products",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column(
            "product_public_id",
            sa.String(length=32),
            nullable=False,
        ),
        sa.Column("category_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("subtitle", sa.String(length=300), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "status",
            sa.String(length=30),
            server_default="DRAFT",
            nullable=False,
        ),
        sa.Column(
            "sort_order",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "published_at",
            sa.DateTime(timezone=True),
            nullable=True,
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
            "length(trim(product_public_id)) > 0",
            name="ck_products_public_id_nonblank",
        ),
        sa.CheckConstraint(
            "length(trim(name)) > 0",
            name="ck_products_name_nonblank",
        ),
        sa.CheckConstraint(
            "status <> 'PUBLISHED' OR published_at IS NOT NULL",
            name="ck_products_published_time",
        ),
        sa.CheckConstraint(
            "sort_order >= 0",
            name="ck_products_sort_nonnegative",
        ),
        sa.CheckConstraint(
            "status IN ('DRAFT', 'PUBLISHED', 'UNPUBLISHED')",
            name="ck_products_status",
        ),
        sa.ForeignKeyConstraint(
            ["category_id"],
            ["product_categories.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("products", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_products_id"),
            ["id"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_products_product_public_id"),
            ["product_public_id"],
            unique=True,
        )
        batch_op.create_index(
            batch_op.f("ix_products_category_id"),
            ["category_id"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_products_name"),
            ["name"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_products_status"),
            ["status"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_products_sort_order"),
            ["sort_order"],
            unique=False,
        )

    op.create_table(
        "product_skus",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=False),
        sa.Column("supplier_id", sa.Integer(), nullable=False),
        sa.Column("sku_code", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column(
            "supplier_sku_code",
            sa.String(length=100),
            nullable=True,
        ),
        sa.Column(
            "points_price",
            sa.Numeric(precision=18, scale=2),
            nullable=False,
        ),
        sa.Column(
            "cost_price",
            sa.Numeric(precision=18, scale=2),
            nullable=False,
        ),
        sa.Column(
            "low_stock_threshold",
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
        sa.Column(
            "sort_order",
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
            "length(trim(sku_code)) > 0",
            name="ck_product_skus_code_nonblank",
        ),
        sa.CheckConstraint(
            "cost_price >= 0",
            name="ck_product_skus_cost_price_nonnegative",
        ),
        sa.CheckConstraint(
            "length(trim(name)) > 0",
            name="ck_product_skus_name_nonblank",
        ),
        sa.CheckConstraint(
            "points_price > 0",
            name="ck_product_skus_points_price_positive",
        ),
        sa.CheckConstraint(
            "sort_order >= 0",
            name="ck_product_skus_sort_nonnegative",
        ),
        sa.CheckConstraint(
            "low_stock_threshold >= 0",
            name="ck_product_skus_threshold_nonnegative",
        ),
        sa.ForeignKeyConstraint(
            ["product_id"],
            ["products.id"],
        ),
        sa.ForeignKeyConstraint(
            ["supplier_id"],
            ["suppliers.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "product_id",
            "name",
            name="uq_product_skus_product_name",
        ),
        sa.UniqueConstraint(
            "supplier_id",
            "supplier_sku_code",
            name="uq_product_skus_supplier_code",
        ),
    )
    with op.batch_alter_table("product_skus", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_product_skus_id"),
            ["id"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_product_skus_product_id"),
            ["product_id"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_product_skus_supplier_id"),
            ["supplier_id"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_product_skus_sku_code"),
            ["sku_code"],
            unique=True,
        )
        batch_op.create_index(
            batch_op.f("ix_product_skus_is_active"),
            ["is_active"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_product_skus_sort_order"),
            ["sort_order"],
            unique=False,
        )


def downgrade() -> None:
    """Remove the empty catalog foundation on disposable databases."""
    with op.batch_alter_table("product_skus", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_product_skus_sort_order"))
        batch_op.drop_index(batch_op.f("ix_product_skus_is_active"))
        batch_op.drop_index(batch_op.f("ix_product_skus_sku_code"))
        batch_op.drop_index(batch_op.f("ix_product_skus_supplier_id"))
        batch_op.drop_index(batch_op.f("ix_product_skus_product_id"))
        batch_op.drop_index(batch_op.f("ix_product_skus_id"))
    op.drop_table("product_skus")

    with op.batch_alter_table("products", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_products_sort_order"))
        batch_op.drop_index(batch_op.f("ix_products_status"))
        batch_op.drop_index(batch_op.f("ix_products_name"))
        batch_op.drop_index(batch_op.f("ix_products_category_id"))
        batch_op.drop_index(batch_op.f("ix_products_product_public_id"))
        batch_op.drop_index(batch_op.f("ix_products_id"))
    op.drop_table("products")

    with op.batch_alter_table("suppliers", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_suppliers_is_active"))
        batch_op.drop_index(batch_op.f("ix_suppliers_name"))
        batch_op.drop_index(
            batch_op.f("ix_suppliers_supplier_public_id")
        )
        batch_op.drop_index(batch_op.f("ix_suppliers_id"))
    op.drop_table("suppliers")

    with op.batch_alter_table(
        "product_categories",
        schema=None,
    ) as batch_op:
        batch_op.drop_index(
            batch_op.f("ix_product_categories_is_active")
        )
        batch_op.drop_index(
            batch_op.f("ix_product_categories_sort_order")
        )
        batch_op.drop_index(
            batch_op.f("ix_product_categories_slug")
        )
        batch_op.drop_index(
            batch_op.f("ix_product_categories_name")
        )
        batch_op.drop_index(
            batch_op.f("ix_product_categories_id")
        )
    op.drop_table("product_categories")
