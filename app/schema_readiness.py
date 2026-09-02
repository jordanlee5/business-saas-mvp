from dataclasses import dataclass

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine


CURRENT_SCHEMA_REVISION = "0002_mall_core_foundation"

REQUIRED_MALL_CORE_TABLES = frozenset(
    {
        "members",
        "member_wechat_bindings",
        "points_accounts",
        "points_grants",
        "points_ledger_entries",
    }
)

REQUIRED_APPLICATION_TABLES = REQUIRED_MALL_CORE_TABLES | frozenset(
    {
        "users",
        "upload_batches",
        "business_records",
        "voucher_upload_batches",
        "voucher_records",
        "match_reviews",
        "admin_action_logs",
        "notifications",
        "promotion_pages",
        "promotion_page_images",
    }
)

REQUIRED_MALL_CORE_COLUMNS = {
    "upload_batches": frozenset(
        {
            "redemption_mode",
            "claim_deadline",
        }
    ),
    "business_records": frozenset(
        {
            "redemption_mode",
            "claim_status",
        }
    ),
}


class DatabaseSchemaNotReadyError(RuntimeError):
    """Raised when the application database is not at the required head."""


@dataclass(frozen=True)
class SchemaReadinessResult:
    revision: str
    checked_tables: frozenset[str]


def assert_database_schema_ready(
    engine: Engine,
) -> SchemaReadinessResult:
    """Fail closed unless the database is at the audited current schema."""
    with engine.connect() as connection:
        inspector = inspect(connection)
        table_names = frozenset(inspector.get_table_names())

        if "alembic_version" not in table_names:
            raise DatabaseSchemaNotReadyError(
                "数据库尚未接入 Alembic 迁移；请停止应用并先完成"
                "备份、副本演练和数据库升级"
            )

        revisions = tuple(
            connection.execute(
                text(
                    "SELECT version_num FROM alembic_version "
                    "ORDER BY version_num"
                )
            ).scalars()
        )
        if revisions != (CURRENT_SCHEMA_REVISION,):
            actual = ", ".join(revisions) if revisions else "无版本标记"
            raise DatabaseSchemaNotReadyError(
                "数据库结构版本不满足当前应用要求："
                f"当前为 {actual}，要求为 {CURRENT_SCHEMA_REVISION}；"
                "请停止应用并先完成副本演练，再运行 "
                "python -m alembic -c alembic.ini upgrade head"
            )

        missing_tables = REQUIRED_APPLICATION_TABLES - table_names
        if missing_tables:
            raise DatabaseSchemaNotReadyError(
                "数据库版本标记与实际结构不一致，缺少表："
                + ", ".join(sorted(missing_tables))
            )

        missing_columns: list[str] = []
        for table_name, required_columns in (
            REQUIRED_MALL_CORE_COLUMNS.items()
        ):
            if table_name not in table_names:
                missing_columns.append(f"{table_name}.*")
                continue
            actual_columns = {
                column["name"]
                for column in inspector.get_columns(table_name)
            }
            missing_columns.extend(
                f"{table_name}.{column_name}"
                for column_name in sorted(
                    required_columns - actual_columns
                )
            )

        if missing_columns:
            raise DatabaseSchemaNotReadyError(
                "数据库版本标记与实际结构不一致，缺少字段："
                + ", ".join(missing_columns)
            )

    return SchemaReadinessResult(
        revision=CURRENT_SCHEMA_REVISION,
        checked_tables=REQUIRED_APPLICATION_TABLES,
    )
