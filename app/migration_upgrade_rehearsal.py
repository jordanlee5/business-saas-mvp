import argparse
import hashlib
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from alembic import command
from alembic.config import Config

from .database import create_database_engine, resolve_database_url
from .migration_baseline import (
    ALEMBIC_CONFIG_PATH,
    BASELINE_REVISION,
    BaselineAdoptionError,
    get_schema_differences,
    get_unexpected_schema_differences,
    validate_and_adopt_baseline,
)
from .migration_rehearsal import (
    MigrationRehearsalError,
    build_sqlite_database_url,
    create_sqlite_snapshot,
    get_business_fingerprint,
    get_current_revision,
    get_sqlite_database_path,
    open_read_only_database,
)
from .schema_readiness import (
    CURRENT_SCHEMA_REVISION,
    assert_database_schema_ready,
)
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_UPGRADE_REHEARSAL_DIRECTORY = (
    PROJECT_ROOT
    / "database_backups"
    / "mall_core_upgrade_rehearsals"
)


class MigrationUpgradeRehearsalError(RuntimeError):
    """Raised when a supported-revision copy rehearsal is unsafe."""


MALL_CORE_FOUNDATION_REVISION = "0002_mall_core_foundation"
SUPPORTED_UPGRADE_SOURCE_REVISIONS = frozenset(
    {
        BASELINE_REVISION,
        MALL_CORE_FOUNDATION_REVISION,
    }
)
MALL_CORE_FOUNDATION_TABLES = frozenset(
    {
        "members",
        "member_wechat_bindings",
        "points_accounts",
        "points_grants",
        "points_ledger_entries",
    }
)
MALL_CORE_FOUNDATION_COLUMNS = {
    "upload_batches": frozenset(
        {"redemption_mode", "claim_deadline"}
    ),
    "business_records": frozenset(
        {"redemption_mode", "claim_status"}
    ),
}


@dataclass(frozen=True)
class LegacyTableSnapshot:
    table_name: str
    columns: tuple[tuple[object, ...], ...]
    row_count: int
    data_fingerprint: str


@dataclass(frozen=True)
class MigrationUpgradeRehearsalResult:
    source_database: Path
    backup_database: Path
    rehearsal_database: Path
    source_revision: str
    target_revision: str
    source_business_fingerprint: str
    legacy_data_fingerprint: str


def quote_sqlite_identifier(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def list_legacy_table_names(
    connection: sqlite3.Connection,
) -> tuple[str, ...]:
    return tuple(
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'table' "
            "AND name NOT LIKE 'sqlite_%' "
            "AND name <> 'alembic_version' "
            "ORDER BY name"
        )
    )


def get_table_column_signatures(
    connection: sqlite3.Connection,
    table_name: str,
) -> tuple[tuple[object, ...], ...]:
    rows = connection.execute(
        "PRAGMA table_info("
        + quote_sqlite_identifier(table_name)
        + ")"
    ).fetchall()
    return tuple(
        (
            row[1],
            row[2],
            row[3],
            row[4],
            row[5],
        )
        for row in rows
    )


def get_table_data_fingerprint(
    connection: sqlite3.Connection,
    table_name: str,
    columns: tuple[tuple[object, ...], ...],
) -> tuple[int, str]:
    column_names = tuple(str(column[0]) for column in columns)
    quoted_columns = ", ".join(
        quote_sqlite_identifier(name) for name in column_names
    )
    primary_key_columns = tuple(
        str(column[0])
        for column in sorted(columns, key=lambda column: column[4])
        if int(column[4]) > 0
    )
    order_columns = primary_key_columns or column_names
    order_clause = ", ".join(
        quote_sqlite_identifier(name) for name in order_columns
    )
    query = (
        f"SELECT {quoted_columns} "
        f"FROM {quote_sqlite_identifier(table_name)} "
        f"ORDER BY {order_clause}"
    )

    digest = hashlib.sha256()
    row_count = 0
    for row in connection.execute(query):
        digest.update(repr(tuple(row)).encode("utf-8"))
        digest.update(b"\n")
        row_count += 1
    return row_count, digest.hexdigest()


def capture_legacy_snapshot(
    database_path: Path,
    *,
    table_names: tuple[str, ...] | None = None,
    expected_columns: dict[
        str,
        tuple[tuple[object, ...], ...],
    ]
    | None = None,
) -> tuple[LegacyTableSnapshot, ...]:
    connection = open_read_only_database(database_path)
    try:
        actual_tables = set(list_legacy_table_names(connection))
        selected_tables = (
            tuple(sorted(actual_tables))
            if table_names is None
            else table_names
        )
        missing_tables = set(selected_tables) - actual_tables
        if missing_tables:
            raise MigrationUpgradeRehearsalError(
                "升级后缺少历史表："
                + ", ".join(sorted(missing_tables))
            )

        snapshots: list[LegacyTableSnapshot] = []
        for table_name in selected_tables:
            current_columns = get_table_column_signatures(
                connection,
                table_name,
            )
            if expected_columns is None:
                selected_columns = current_columns
            else:
                selected_columns = expected_columns[table_name]
                current_by_name = {
                    str(column[0]): column
                    for column in current_columns
                }
                changed_columns = [
                    str(column[0])
                    for column in selected_columns
                    if current_by_name.get(str(column[0])) != column
                ]
                if changed_columns:
                    raise MigrationUpgradeRehearsalError(
                        f"历史表 {table_name} 的原字段定义发生变化："
                        + ", ".join(changed_columns)
                    )

            row_count, fingerprint = get_table_data_fingerprint(
                connection,
                table_name,
                selected_columns,
            )
            snapshots.append(
                LegacyTableSnapshot(
                    table_name=table_name,
                    columns=selected_columns,
                    row_count=row_count,
                    data_fingerprint=fingerprint,
                )
            )
        return tuple(snapshots)
    finally:
        connection.close()


def fingerprint_legacy_snapshot(
    snapshot: tuple[LegacyTableSnapshot, ...],
) -> str:
    digest = hashlib.sha256()
    for table in snapshot:
        digest.update(repr(table).encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def build_upgrade_rehearsal_paths(
    source_path: Path,
    backup_directory: Path,
) -> tuple[Path, Path]:
    suffix = source_path.suffix or ".db"
    run_id = (
        datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        + "_"
        + uuid4().hex[:8]
    )
    backup_path = backup_directory / (
        f"{source_path.stem}.before_mall_core_upgrade_"
        f"{run_id}{suffix}"
    )
    rehearsal_path = backup_directory / (
        f"{source_path.stem}.mall_core_upgrade_rehearsal_"
        f"{run_id}{suffix}"
    )
    return backup_path, rehearsal_path


def build_alembic_config(database_url: str) -> Config:
    config = Config(str(ALEMBIC_CONFIG_PATH))
    config.attributes["database_url"] = database_url
    return config


def validate_mall_core_foundation_source(
    database_path: Path,
) -> None:
    """Fail closed when a database only claims to be at revision 0002."""
    connection = open_read_only_database(database_path)
    try:
        tables = set(list_legacy_table_names(connection))
        missing_tables = MALL_CORE_FOUNDATION_TABLES - tables
        if missing_tables:
            raise MigrationUpgradeRehearsalError(
                "0002 源库缺少商城核心表："
                + ", ".join(sorted(missing_tables))
            )
        if "member_activation_credentials" in tables:
            raise MigrationUpgradeRehearsalError(
                "0002 源库已存在未登记的激活凭据表"
            )
        missing_columns: list[str] = []
        for table_name, required_columns in (
            MALL_CORE_FOUNDATION_COLUMNS.items()
        ):
            columns = {
                str(column[0])
                for column in get_table_column_signatures(
                    connection,
                    table_name,
                )
            }
            missing_columns.extend(
                f"{table_name}.{column_name}"
                for column_name in sorted(required_columns - columns)
            )
        if missing_columns:
            raise MigrationUpgradeRehearsalError(
                "0002 源库缺少商城核心字段："
                + ", ".join(missing_columns)
            )
    finally:
        connection.close()


def rehearse_mall_core_upgrade(
    database_url: str,
    *,
    backup_directory: Path = DEFAULT_UPGRADE_REHEARSAL_DIRECTORY,
) -> MigrationUpgradeRehearsalResult:
    """Upgrade only a copy and prove that all legacy values survive."""
    source_path = get_sqlite_database_path(database_url)
    source_revision = get_current_revision(source_path)
    if source_revision not in SUPPORTED_UPGRADE_SOURCE_REVISIONS:
        actual = source_revision or "无版本标记"
        raise MigrationUpgradeRehearsalError(
            "升级副本演练要求源库位于受支持的历史版本："
            f"当前为 {actual}，支持 "
            + "、".join(sorted(SUPPORTED_UPGRADE_SOURCE_REVISIONS))
        )

    if source_revision == BASELINE_REVISION:
        try:
            validate_and_adopt_baseline(database_url)
        except BaselineAdoptionError as exc:
            raise MigrationUpgradeRehearsalError(
                "源库未通过 0001 结构与数据完整性预检："
                f"{exc}"
            ) from exc
    else:
        validate_mall_core_foundation_source(source_path)

    source_business_fingerprint = get_business_fingerprint(source_path)
    source_snapshot = capture_legacy_snapshot(source_path)
    legacy_data_fingerprint = fingerprint_legacy_snapshot(
        source_snapshot
    )
    table_names = tuple(
        table.table_name for table in source_snapshot
    )
    expected_columns = {
        table.table_name: table.columns for table in source_snapshot
    }

    backup_directory = Path(backup_directory).resolve()
    backup_path, rehearsal_path = build_upgrade_rehearsal_paths(
        source_path,
        backup_directory,
    )
    create_sqlite_snapshot(source_path, backup_path)
    create_sqlite_snapshot(backup_path, rehearsal_path)

    backup_snapshot = capture_legacy_snapshot(backup_path)
    if backup_snapshot != source_snapshot:
        raise MigrationUpgradeRehearsalError(
            "原始快照与源数据库的历史结构或数据不一致"
        )

    rehearsal_url = build_sqlite_database_url(rehearsal_path)
    config = build_alembic_config(rehearsal_url)
    try:
        command.upgrade(config, "head")
        command.upgrade(config, "head")
    except Exception as exc:
        raise MigrationUpgradeRehearsalError(
            f"演练副本升级失败：{exc}"
        ) from exc

    if get_current_revision(rehearsal_path) != CURRENT_SCHEMA_REVISION:
        raise MigrationUpgradeRehearsalError(
            "演练副本未稳定升级到当前 head"
        )

    upgraded_snapshot = capture_legacy_snapshot(
        rehearsal_path,
        table_names=table_names,
        expected_columns=expected_columns,
    )
    if upgraded_snapshot != source_snapshot:
        raise MigrationUpgradeRehearsalError(
            "升级后历史表的原字段数据发生变化"
        )

    rehearsal_engine = create_database_engine(rehearsal_url)
    try:
        with rehearsal_engine.connect() as connection:
            differences = get_schema_differences(
                connection,
                include_post_baseline_objects=True,
            )
            unexpected_differences = (
                get_unexpected_schema_differences(
                    connection,
                    differences,
                )
            )
            if unexpected_differences:
                raise MigrationUpgradeRehearsalError(
                    "演练副本存在未获批准的结构漂移："
                    + repr(unexpected_differences)
                )
        assert_database_schema_ready(rehearsal_engine)
    finally:
        rehearsal_engine.dispose()

    if get_business_fingerprint(source_path) != source_business_fingerprint:
        raise MigrationUpgradeRehearsalError(
            "演练期间源数据库发生变化，结果无效"
        )

    return MigrationUpgradeRehearsalResult(
        source_database=source_path,
        backup_database=backup_path,
        rehearsal_database=rehearsal_path,
        source_revision=source_revision,
        target_revision=CURRENT_SCHEMA_REVISION,
        source_business_fingerprint=source_business_fingerprint,
        legacy_data_fingerprint=legacy_data_fingerprint,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="在 SQLite 副本上演练受支持历史版本到当前 head 的升级"
    )
    parser.add_argument(
        "--backup-dir",
        type=Path,
        default=DEFAULT_UPGRADE_REHEARSAL_DIRECTORY,
        help="升级前快照和演练副本的保存目录",
    )
    args = parser.parse_args(argv)

    try:
        result = rehearse_mall_core_upgrade(
            resolve_database_url(),
            backup_directory=args.backup_dir,
        )
    except (
        MigrationUpgradeRehearsalError,
        MigrationRehearsalError,
        OSError,
        sqlite3.Error,
    ) as exc:
        parser.exit(1, f"商城核心升级副本演练失败：{exc}\n")

    print("商城核心升级副本演练通过：源数据库未修改")
    print(f"源数据库：{result.source_database}")
    print(f"升级前快照：{result.backup_database}")
    print(f"演练副本：{result.rehearsal_database}")
    print(
        f"版本：{result.source_revision} -> {result.target_revision}"
    )
    print(f"源库业务指纹：{result.source_business_fingerprint}")
    print(f"历史字段数据指纹：{result.legacy_data_fingerprint}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
