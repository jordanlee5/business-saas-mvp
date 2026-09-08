import unittest
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory

from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from app.migration_baseline import ALEMBIC_CONFIG_PATH
from app.schema_readiness import CURRENT_SCHEMA_REVISION


PREVIOUS_REVISION = "0003_member_activation_security"
CATALOG_TABLES = {
    "product_categories",
    "suppliers",
    "products",
    "product_skus",
}


def build_config(database_url: str) -> Config:
    config = Config(str(ALEMBIC_CONFIG_PATH))
    config.attributes["database_url"] = database_url
    return config


def current_revision(database_url: str) -> str | None:
    engine = create_engine(database_url)
    try:
        with engine.connect() as connection:
            return MigrationContext.configure(
                connection
            ).get_current_revision()
    finally:
        engine.dispose()


class CatalogFoundationMigrationTests(unittest.TestCase):
    def test_upgrade_schema_and_round_trip(self):
        with TemporaryDirectory() as temporary_directory:
            database_path = Path(temporary_directory) / "catalog.db"
            database_url = f"sqlite:///{database_path.as_posix()}"
            config = build_config(database_url)
            command.upgrade(config, PREVIOUS_REVISION)
            command.upgrade(config, "head")
            command.check(config)

            self.assertEqual(
                current_revision(database_url),
                CURRENT_SCHEMA_REVISION,
            )
            engine = create_engine(database_url)
            try:
                inspector = inspect(engine)
                self.assertTrue(
                    CATALOG_TABLES.issubset(inspector.get_table_names())
                )
                sku_columns = {
                    column["name"]
                    for column in inspector.get_columns("product_skus")
                }
                self.assertTrue(
                    {
                        "product_id",
                        "supplier_id",
                        "sku_code",
                        "points_price",
                        "cost_price",
                        "low_stock_threshold",
                    }.issubset(sku_columns)
                )
                product_targets = {
                    foreign_key["referred_table"]
                    for foreign_key in inspector.get_foreign_keys(
                        "products"
                    )
                }
                sku_targets = {
                    foreign_key["referred_table"]
                    for foreign_key in inspector.get_foreign_keys(
                        "product_skus"
                    )
                }
                self.assertEqual(
                    product_targets,
                    {"product_categories"},
                )
                self.assertEqual(
                    sku_targets,
                    {"products", "suppliers"},
                )
                sku_indexes = {
                    index["name"]: index
                    for index in inspector.get_indexes("product_skus")
                }
                self.assertTrue(
                    sku_indexes["ix_product_skus_sku_code"]["unique"]
                )
            finally:
                engine.dispose()

            command.downgrade(config, PREVIOUS_REVISION)
            engine = create_engine(database_url)
            try:
                self.assertTrue(
                    CATALOG_TABLES.isdisjoint(
                        inspect(engine).get_table_names()
                    )
                )
            finally:
                engine.dispose()
            command.upgrade(config, "head")
            self.assertEqual(
                current_revision(database_url),
                CURRENT_SCHEMA_REVISION,
            )

    def test_database_rejects_invalid_catalog_values(self):
        with TemporaryDirectory() as temporary_directory:
            database_path = Path(temporary_directory) / "constraints.db"
            database_url = f"sqlite:///{database_path.as_posix()}"
            command.upgrade(build_config(database_url), "head")
            engine = create_engine(database_url)
            now = datetime(2026, 9, 8, 18, 0, 0)
            try:
                with engine.begin() as connection:
                    connection.execute(
                        text(
                            "INSERT INTO product_categories "
                            "(id, name, slug, created_at, updated_at) "
                            "VALUES (1, '车载用品', 'car-accessories', "
                            ":now, :now)"
                        ),
                        {"now": now},
                    )
                    connection.execute(
                        text(
                            "INSERT INTO suppliers "
                            "(id, supplier_public_id, name, created_at, "
                            "updated_at) VALUES "
                            "(1, 'SUP-0001', '测试供应商', :now, :now)"
                        ),
                        {"now": now},
                    )
                    connection.execute(
                        text(
                            "INSERT INTO products "
                            "(id, product_public_id, category_id, name, "
                            "status, created_at, updated_at) VALUES "
                            "(1, 'PROD-0001', 1, '车载急救包', "
                            "'DRAFT', :now, :now)"
                        ),
                        {"now": now},
                    )

                invalid_statements = (
                    (
                        "blank category",
                        "INSERT INTO product_categories "
                        "(name, slug, created_at, updated_at) VALUES "
                        "(' ', 'blank', :now, :now)",
                    ),
                    (
                        "negative category sort",
                        "INSERT INTO product_categories "
                        "(name, slug, sort_order, created_at, updated_at) "
                        "VALUES ('异常分类', 'bad-sort', -1, :now, :now)",
                    ),
                    (
                        "published without time",
                        "INSERT INTO products "
                        "(product_public_id, category_id, name, status, "
                        "created_at, updated_at) VALUES "
                        "('PROD-0002', 1, '无发布时间商品', 'PUBLISHED', "
                        ":now, :now)",
                    ),
                    (
                        "unknown product status",
                        "INSERT INTO products "
                        "(product_public_id, category_id, name, status, "
                        "created_at, updated_at) VALUES "
                        "('PROD-0003', 1, '未知状态商品', 'ARCHIVED', "
                        ":now, :now)",
                    ),
                )
                for label, statement in invalid_statements:
                    with self.subTest(label=label):
                        with self.assertRaises(IntegrityError):
                            with engine.begin() as connection:
                                connection.execute(
                                    text(statement),
                                    {"now": now},
                                )

                sku_statement = (
                    "INSERT INTO product_skus "
                    "(product_id, supplier_id, sku_code, name, "
                    "points_price, cost_price, low_stock_threshold, "
                    "sort_order, created_at, updated_at) VALUES "
                    "(1, 1, :sku_code, '标准款', :points_price, "
                    ":cost_price, :threshold, :sort_order, :now, :now)"
                )
                for field, values in (
                    (
                        "points_price",
                        {
                            "points_price": 0,
                            "cost_price": 10,
                            "threshold": 0,
                            "sort_order": 0,
                        },
                    ),
                    (
                        "cost_price",
                        {
                            "points_price": 100,
                            "cost_price": -1,
                            "threshold": 0,
                            "sort_order": 0,
                        },
                    ),
                    (
                        "low_stock_threshold",
                        {
                            "points_price": 100,
                            "cost_price": 10,
                            "threshold": -1,
                            "sort_order": 0,
                        },
                    ),
                    (
                        "sort_order",
                        {
                            "points_price": 100,
                            "cost_price": 10,
                            "threshold": 0,
                            "sort_order": -1,
                        },
                    ),
                ):
                    with self.subTest(field=field):
                        parameters = {
                            **values,
                            "sku_code": f"SKU-BAD-{field}",
                            "now": now,
                        }
                        with self.assertRaises(IntegrityError):
                            with engine.begin() as connection:
                                connection.execute(
                                    text(sku_statement),
                                    parameters,
                                )
            finally:
                engine.dispose()


if __name__ == "__main__":
    unittest.main()
