import argparse
from dataclasses import dataclass
from pathlib import Path

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from alembic.util.exc import CommandError
from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection

from . import models as application_models  # noqa: F401
from .database import (
    Base,
    create_database_engine,
    resolve_database_url,
)


PROJECT_ROOT = Path(__file__).resolve().parent.parent
ALEMBIC_CONFIG_PATH = PROJECT_ROOT / "alembic.ini"
BASELINE_REVISION = "0001_current_schema_baseline"

LEGACY_SQLITE_DEFAULTS = {
    ("admin_action_logs", "created_at"): "CURRENT_TIMESTAMP",
    ("business_records", "record_service_rate"): "0",
    ("business_records", "record_upstream_cost_rate"): "0",
    ("business_records", "created_at"): "CURRENT_TIMESTAMP",
    ("match_reviews", "created_at"): "CURRENT_TIMESTAMP",
    ("upload_batches", "acceptance_status"): "'已承接'",
    ("upload_batches", "created_at"): "CURRENT_TIMESTAMP",
    ("users", "created_at"): "CURRENT_TIMESTAMP",
    ("voucher_records", "voucher_amount"): "0.0",
    ("voucher_records", "created_at"): "CURRENT_TIMESTAMP",
    ("voucher_upload_batches", "created_at"): "CURRENT_TIMESTAMP",
}

LEGACY_SQLITE_TYPE_CHANGES = {
    ("business_records", "public_business_no"):
        ("TEXT", "STRING", 32),
    ("business_records", "record_service_rate_mode"):
        ("TEXT", "STRING", 20),
    ("business_records", "record_upstream_cost_rate_mode"):
        ("TEXT", "STRING", 20),
    ("match_reviews", "primary_review_comment"):
        ("TEXT", "STRING", None),
    ("match_reviews", "secondary_review_comment"):
        ("TEXT", "STRING", None),
    ("upload_batches", "acceptance_status"):
        ("TEXT", "STRING", None),
    ("users", "is_active"):
        ("INTEGER", "BOOLEAN", None),
    ("users", "must_change_password"):
        ("INTEGER", "BOOLEAN", None),
    ("users", "service_rate_mode"):
        ("TEXT", "STRING", 20),
    ("users", "upstream_cost_rate_mode"):
        ("TEXT", "STRING", 20),
}

LEGACY_SQLITE_MISSING_INDEXES = {
    (
        "business_records",
        "ix_business_records_business_no",
        ("business_no",),
        False,
    ),
    (
        "voucher_records",
        "ix_voucher_records_batch_id",
        ("batch_id",),
        False,
    ),
    (
        "voucher_records",
        "ix_voucher_records_file_hash",
        ("file_hash",),
        False,
    ),
}

LEGACY_SQLITE_MISSING_FOREIGN_KEYS = {
    (
        "match_reviews",
        ("primary_reviewer_id",),
        ("users.id",),
    ),
    (
        "match_reviews",
        ("secondary_reviewer_id",),
        ("users.id",),
    ),
    (
        "voucher_records",
        ("batch_id",),
        ("voucher_upload_batches.id",),
    ),
}

LEGACY_SQLITE_INTEGRITY_CHECKS = {
    "primary_reviewer_orphans": """
        SELECT COUNT(*)
        FROM match_reviews AS reviews
        LEFT JOIN users
          ON users.id = reviews.primary_reviewer_id
        WHERE reviews.primary_reviewer_id IS NOT NULL
          AND users.id IS NULL
    """,
    "secondary_reviewer_orphans": """
        SELECT COUNT(*)
        FROM match_reviews AS reviews
        LEFT JOIN users
          ON users.id = reviews.secondary_reviewer_id
        WHERE reviews.secondary_reviewer_id IS NOT NULL
          AND users.id IS NULL
    """,
    "voucher_batch_orphans": """
        SELECT COUNT(*)
        FROM voucher_records AS vouchers
        LEFT JOIN voucher_upload_batches AS batches
          ON batches.id = vouchers.batch_id
        WHERE vouchers.batch_id IS NOT NULL
          AND batches.id IS NULL
    """,
    "invalid_is_active": """
        SELECT COUNT(*)
        FROM users
        WHERE is_active IS NULL OR is_active NOT IN (0, 1)
    """,
    "invalid_must_change_password": """
        SELECT COUNT(*)
        FROM users
        WHERE must_change_password IS NULL
           OR must_change_password NOT IN (0, 1)
    """,
    "invalid_user_rate_modes": """
        SELECT COUNT(*)
        FROM users
        WHERE service_rate_mode NOT IN ('external', 'internal')
           OR upstream_cost_rate_mode NOT IN ('external', 'internal')
    """,
    "invalid_record_rate_modes": """
        SELECT COUNT(*)
        FROM business_records
        WHERE record_service_rate_mode NOT IN ('external', 'internal')
           OR record_upstream_cost_rate_mode NOT IN ('external', 'internal')
    """,
}


class BaselineAdoptionError(RuntimeError):
    """Raised when an existing database cannot safely adopt the baseline."""


@dataclass(frozen=True)
class BaselineAdoptionResult:
    revision: str
    applied: bool
    already_adopted: bool


def build_alembic_config() -> Config:
    return Config(str(ALEMBIC_CONFIG_PATH))


def ensure_baseline_is_only_head(config: Config) -> None:
    heads = tuple(
        ScriptDirectory.from_config(config).get_heads()
    )
    if heads != (BASELINE_REVISION,):
        raise BaselineAdoptionError(
            "基线接入工具仅允许在初始基线是唯一 head 时运行"
        )


def get_database_revisions(
    connection: Connection,
) -> tuple[str, ...]:
    if not inspect(connection).has_table("alembic_version"):
        return ()

    revisions = connection.execute(
        text(
            "SELECT version_num "
            "FROM alembic_version "
            "ORDER BY version_num"
        )
    ).scalars()
    return tuple(revisions)


def get_schema_differences(
    connection: Connection,
) -> tuple[object, ...]:
    migration_context = MigrationContext.configure(
        connection,
        opts={
            "compare_type": True,
            "compare_server_default": True,
        },
    )
    return tuple(
        compare_metadata(migration_context, Base.metadata)
    )


def flatten_schema_differences(
    differences: tuple[object, ...],
) -> tuple[object, ...]:
    flattened: list[object] = []
    for difference in differences:
        if isinstance(difference, list):
            flattened.extend(difference)
        else:
            flattened.append(difference)
    return tuple(flattened)


def strip_redundant_outer_parentheses(value: str) -> str:
    normalized = value.strip()
    while (
        normalized.startswith("(")
        and normalized.endswith(")")
    ):
        depth = 0
        quote: str | None = None
        wraps_entire_expression = True
        index = 0

        while index < len(normalized):
            character = normalized[index]
            if quote is not None:
                if character == quote:
                    if (
                        index + 1 < len(normalized)
                        and normalized[index + 1] == quote
                    ):
                        index += 2
                        continue
                    quote = None
            elif character in ("'", '"'):
                quote = character
            elif character == "(":
                depth += 1
            elif character == ")":
                depth -= 1
                if depth < 0:
                    wraps_entire_expression = False
                    break
                if depth == 0 and index != len(normalized) - 1:
                    wraps_entire_expression = False
                    break
            index += 1

        if (
            not wraps_entire_expression
            or depth != 0
            or quote is not None
        ):
            break
        normalized = normalized[1:-1].strip()

    return normalized


def normalize_server_default(value: object) -> str | None:
    if value is None or value is False:
        return None
    return strip_redundant_outer_parentheses(
        str(getattr(value, "arg", value))
    )


def get_type_signature(column_type: object) -> tuple[str, int | None]:
    return (
        type(column_type).__name__.upper(),
        getattr(column_type, "length", None),
    )


def is_compatible_legacy_type_change(
    difference: tuple[object, ...],
) -> bool:
    if len(difference) < 7:
        return False
    _, schema, table_name, column_name = difference[:4]
    expected = LEGACY_SQLITE_TYPE_CHANGES.get(
        (table_name, column_name)
    )
    if schema is not None or expected is None:
        return False

    existing_type = difference[-2]
    metadata_type = difference[-1]
    existing_name, _ = get_type_signature(existing_type)
    metadata_name, metadata_length = get_type_signature(
        metadata_type
    )
    return (
        existing_name,
        metadata_name,
        metadata_length,
    ) == expected


def is_compatible_legacy_default_change(
    difference: tuple[object, ...],
) -> bool:
    if len(difference) < 7:
        return False
    _, schema, table_name, column_name = difference[:4]
    expected = LEGACY_SQLITE_DEFAULTS.get(
        (table_name, column_name)
    )
    if schema is not None or expected is None:
        return False
    return (
        normalize_server_default(difference[-2]) == expected
        and normalize_server_default(difference[-1]) is None
    )


def get_index_signature(index: object) -> tuple[object, ...]:
    table = getattr(index, "table", None)
    columns = getattr(index, "columns", ())
    return (
        getattr(table, "name", None),
        getattr(index, "name", None),
        tuple(column.name for column in columns),
        bool(getattr(index, "unique", False)),
    )


def get_foreign_key_signature(
    constraint: object,
) -> tuple[object, ...]:
    table = getattr(constraint, "table", None)
    elements = getattr(constraint, "elements", ())
    return (
        getattr(table, "name", None),
        tuple(element.parent.name for element in elements),
        tuple(element.target_fullname for element in elements),
    )


def is_compatible_legacy_schema_difference(
    difference: tuple[object, ...],
) -> bool:
    if not difference:
        return False
    operation = difference[0]
    if operation == "modify_type":
        return is_compatible_legacy_type_change(difference)
    if operation == "modify_default":
        return is_compatible_legacy_default_change(difference)
    if operation == "add_index" and len(difference) == 2:
        return (
            get_index_signature(difference[1])
            in LEGACY_SQLITE_MISSING_INDEXES
        )
    if operation == "add_fk" and len(difference) == 2:
        return (
            get_foreign_key_signature(difference[1])
            in LEGACY_SQLITE_MISSING_FOREIGN_KEYS
        )
    return False


def get_unexpected_schema_differences(
    connection: Connection,
    differences: tuple[object, ...],
) -> tuple[object, ...]:
    flattened = flatten_schema_differences(differences)
    if connection.dialect.name != "sqlite":
        return flattened
    return tuple(
        difference
        for difference in flattened
        if (
            not isinstance(difference, tuple)
            or not is_compatible_legacy_schema_difference(
                difference
            )
        )
    )


def get_legacy_sqlite_integrity_violations(
    connection: Connection,
) -> tuple[tuple[str, int], ...]:
    violations = []
    for name, sql in LEGACY_SQLITE_INTEGRITY_CHECKS.items():
        count = int(connection.execute(text(sql)).scalar_one())
        if count:
            violations.append((name, count))
    return tuple(violations)


def validate_and_adopt_baseline(
    database_url: str,
    *,
    apply: bool = False,
) -> BaselineAdoptionResult:
    """Validate an existing schema and optionally stamp the baseline."""
    config = build_alembic_config()
    ensure_baseline_is_only_head(config)
    engine = create_database_engine(database_url)

    try:
        with engine.begin() as connection:
            config.attributes["connection"] = connection

            existing_revisions = get_database_revisions(
                connection
            )
            if existing_revisions not in (
                (),
                (BASELINE_REVISION,),
            ):
                raise BaselineAdoptionError(
                    "数据库已经包含其他 Alembic 版本，拒绝覆盖"
                )

            differences = get_schema_differences(connection)
            if get_unexpected_schema_differences(
                connection,
                differences,
            ):
                raise BaselineAdoptionError(
                    "数据库结构与初始基线不一致，未写入版本标记"
                )

            if connection.dialect.name == "sqlite":
                violations = (
                    get_legacy_sqlite_integrity_violations(
                        connection
                    )
                )
                if violations:
                    details = ", ".join(
                        f"{name}={count}"
                        for name, count in violations
                    )
                    raise BaselineAdoptionError(
                        "历史 SQLite 数据完整性检查未通过："
                        f"{details}"
                    )

            already_adopted = (
                existing_revisions == (BASELINE_REVISION,)
            )
            applied = apply and not already_adopted
            if applied:
                try:
                    command.stamp(config, BASELINE_REVISION)
                except CommandError as exc:
                    raise BaselineAdoptionError(
                        "结构检查通过，但版本标记写入失败"
                    ) from exc

            return BaselineAdoptionResult(
                revision=BASELINE_REVISION,
                applied=applied,
                already_adopted=already_adopted,
            )
    finally:
        engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "检查现有数据库是否能安全接入 Alembic 初始基线"
        )
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="结构检查通过后写入初始基线版本标记",
    )
    args = parser.parse_args(argv)

    try:
        result = validate_and_adopt_baseline(
            resolve_database_url(),
            apply=args.apply,
        )
    except BaselineAdoptionError as exc:
        parser.exit(1, f"基线接入失败：{exc}\n")

    if result.already_adopted:
        print("数据库已经接入初始基线，无需重复写入")
    elif result.applied:
        print("基线接入完成：仅写入 Alembic 版本标记")
    else:
        print("结构检查通过：只读模式，未写入版本标记")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
