import unittest
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from app.migration_baseline import ALEMBIC_CONFIG_PATH
from app.schema_readiness import CURRENT_SCHEMA_REVISION


PREVIOUS_REVISION = "0004_catalog_foundation"


def build_config(database_url: str) -> Config:
    config = Config(str(ALEMBIC_CONFIG_PATH))
    config.attributes["database_url"] = database_url
    return config


class ProductMediaMigrationTests(unittest.TestCase):
    def test_upgrade_creates_portable_media_schema_and_round_trip(self):
        with TemporaryDirectory() as temporary_directory:
            database_path = Path(temporary_directory) / "media.db"
            database_url = f"sqlite:///{database_path.as_posix()}"
            config = build_config(database_url)
            command.upgrade(config, PREVIOUS_REVISION)
            command.upgrade(config, "head")
            command.check(config)

            engine = create_engine(database_url)
            try:
                inspector = inspect(engine)
                self.assertIn("product_media", inspector.get_table_names())
                columns = {
                    column["name"]
                    for column in inspector.get_columns("product_media")
                }
                self.assertTrue(
                    {
                        "product_id",
                        "media_role",
                        "image_path",
                        "alt_text",
                        "sort_order",
                        "is_active",
                        "uploaded_by_id",
                    }.issubset(columns)
                )
                targets = {
                    foreign_key["referred_table"]
                    for foreign_key in inspector.get_foreign_keys(
                        "product_media"
                    )
                }
                self.assertEqual(targets, {"products", "users"})
                indexes = {
                    index["name"]: index
                    for index in inspector.get_indexes("product_media")
                }
                self.assertTrue(
                    indexes["uq_product_media_one_main_per_product"]["unique"]
                )
                with engine.connect() as connection:
                    revision = connection.execute(
                        text("SELECT version_num FROM alembic_version")
                    ).scalar_one()
                self.assertEqual(revision, CURRENT_SCHEMA_REVISION)
            finally:
                engine.dispose()

            command.downgrade(config, PREVIOUS_REVISION)
            engine = create_engine(database_url)
            try:
                self.assertNotIn("product_media", inspect(engine).get_table_names())
            finally:
                engine.dispose()

    def test_constraints_allow_many_secondary_images_but_one_main(self):
        with TemporaryDirectory() as temporary_directory:
            database_path = Path(temporary_directory) / "constraints.db"
            database_url = f"sqlite:///{database_path.as_posix()}"
            command.upgrade(build_config(database_url), "head")
            engine = create_engine(database_url)
            now = datetime(2026, 9, 10, 12, 0, 0)
            try:
                with engine.begin() as connection:
                    connection.execute(text(
                        "INSERT INTO users (id, username, password_hash, role, "
                        "is_active, must_change_password, service_rate_mode, "
                        "upstream_cost_rate_mode) VALUES "
                        "(1, 'operator', 'x', 'admin', 1, 0, 'external', 'external')"
                    ))
                    connection.execute(text(
                        "INSERT INTO product_categories "
                        "(id, name, slug, created_at, updated_at) VALUES "
                        "(1, '分类', 'category', :now, :now)"
                    ), {"now": now})
                    connection.execute(text(
                        "INSERT INTO products "
                        "(id, product_public_id, category_id, name, status, "
                        "created_at, updated_at) VALUES "
                        "(1, 'PRD-ABC234', 1, '商品', 'DRAFT', :now, :now)"
                    ), {"now": now})
                    for media_id, role in ((1, "MAIN"), (2, "CAROUSEL"), (3, "CAROUSEL")):
                        connection.execute(text(
                            "INSERT INTO product_media "
                            "(id, product_id, media_role, image_path, uploaded_by_id, "
                            "created_at, updated_at) VALUES "
                            "(:id, 1, :role, :path, 1, :now, :now)"
                        ), {
                            "id": media_id,
                            "role": role,
                            "path": f"/uploads/mall_products/PRD-ABC234/{media_id}.webp",
                            "now": now,
                        })

                invalid_rows = (
                    ("duplicate main", "MAIN", 0),
                    ("unknown role", "THUMBNAIL", 0),
                    ("negative sort", "DETAIL", -1),
                )
                for label, role, sort_order in invalid_rows:
                    with self.subTest(label=label):
                        with self.assertRaises(IntegrityError):
                            with engine.begin() as connection:
                                connection.execute(text(
                                    "INSERT INTO product_media "
                                    "(product_id, media_role, image_path, sort_order, "
                                    "uploaded_by_id, created_at, updated_at) VALUES "
                                    "(1, :role, :path, :sort_order, 1, :now, :now)"
                                ), {
                                    "role": role,
                                    "path": f"/uploads/mall_products/PRD-ABC234/{label}.webp",
                                    "sort_order": sort_order,
                                    "now": now,
                                })
            finally:
                engine.dispose()


if __name__ == "__main__":
    unittest.main()
