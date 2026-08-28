import argparse
import hashlib
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from alembic.migration import MigrationContext
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url

from .database import resolve_database_url
from .migration_baseline import (
    BASELINE_REVISION,
    BaselineAdoptionError,
    validate_and_adopt_baseline,
)


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_BACKUP_DIRECTORY = (
    PROJECT_ROOT
    / "database_backups"
    / "migration_baseline_rehearsals"
)


class MigrationRehearsalError(RuntimeError):
    """Raised when a database-copy rehearsal cannot finish safely."""


@dataclass(frozen=True)
class MigrationRehearsalResult:
    source_database: Path
    backup_database: Path
    rehearsal_database: Path
    revision: str
    business_fingerprint: str
    source_was_already_adopted: bool


def get_sqlite_database_path(database_url: str) -> Path:
    parsed_url = make_url(database_url)
    if parsed_url.get_backend_name() != "sqlite":
        raise MigrationRehearsalError(
            "副本演练当前只支持本地 SQLite 数据库"
        )

    database_name = parsed_url.database
    if not database_name or database_name == ":memory:":
        raise MigrationRehearsalError(
            "副本演练需要可定位的 SQLite 数据库文件"
        )

    database_path = Path(database_name).expanduser().resolve()
    if not database_path.is_file():
        raise MigrationRehearsalError(
            f"未找到源数据库文件：{database_path}"
        )
    return database_path


def open_read_only_database(database_path: Path) -> sqlite3.Connection:
    database_path = database_path.resolve()
    if not database_path.is_file():
        raise MigrationRehearsalError(
            f"未找到数据库文件：{database_path}"
        )

    connection = sqlite3.connect(
        f"{database_path.as_uri()}?mode=ro",
        uri=True,
    )
    connection.execute("PRAGMA query_only = ON")
    if connection.execute(
        "PRAGMA query_only"
    ).fetchone()[0] != 1:
        connection.close()
        raise MigrationRehearsalError(
            "数据库只读保护未启用"
        )
    return connection


def create_sqlite_snapshot(
    source_path: Path,
    destination_path: Path,
) -> None:
    destination_path = destination_path.resolve()
    if destination_path.exists():
        raise MigrationRehearsalError(
            f"目标文件已存在，不会覆盖：{destination_path}"
        )

    destination_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )
    source = open_read_only_database(source_path)
    destination = None

    try:
        destination = sqlite3.connect(destination_path)
        source.backup(destination)
        quick_check = destination.execute(
            "PRAGMA quick_check"
        ).fetchone()[0]
        if quick_check != "ok":
            raise MigrationRehearsalError(
                f"数据库副本完整性检查失败：{quick_check}"
            )
    except Exception:
        if destination is not None:
            destination.close()
        source.close()
        if destination_path.exists():
            destination_path.unlink()
        raise

    destination.close()
    source.close()

    if (
        not destination_path.is_file()
        or destination_path.stat().st_size <= 0
    ):
        raise MigrationRehearsalError(
            "数据库副本未正确生成"
        )


def is_alembic_version_dump_statement(statement: str) -> bool:
    normalized = statement.lstrip().upper()
    return normalized.startswith(
        (
            "CREATE TABLE ALEMBIC_VERSION",
            'CREATE TABLE "ALEMBIC_VERSION"',
            "CREATE TABLE 'ALEMBIC_VERSION'",
            "INSERT INTO ALEMBIC_VERSION",
            'INSERT INTO "ALEMBIC_VERSION"',
            "INSERT INTO 'ALEMBIC_VERSION'",
        )
    )


def get_business_fingerprint(database_path: Path) -> str:
    digest = hashlib.sha256()
    connection = open_read_only_database(database_path)

    try:
        quick_check = connection.execute(
            "PRAGMA quick_check"
        ).fetchone()[0]
        if quick_check != "ok":
            raise MigrationRehearsalError(
                f"数据库完整性检查失败：{quick_check}"
            )

        for statement in connection.iterdump():
            if is_alembic_version_dump_statement(statement):
                continue
            digest.update(statement.encode("utf-8"))
            digest.update(b"\n")
    finally:
        connection.close()

    return digest.hexdigest()


def build_sqlite_database_url(database_path: Path) -> str:
    return f"sqlite:///{database_path.resolve().as_posix()}"


def get_current_revision(database_path: Path) -> str | None:
    engine = create_engine(
        build_sqlite_database_url(database_path)
    )
    try:
        with engine.connect() as connection:
            return MigrationContext.configure(
                connection
            ).get_current_revision()
    finally:
        engine.dispose()


def build_rehearsal_paths(
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
        f"{source_path.stem}.before_alembic_baseline_"
        f"{run_id}{suffix}"
    )
    rehearsal_path = backup_directory / (
        f"{source_path.stem}.baseline_rehearsal_"
        f"{run_id}{suffix}"
    )
    return backup_path, rehearsal_path


def rehearse_baseline_adoption(
    database_url: str,
    *,
    backup_directory: Path = DEFAULT_BACKUP_DIRECTORY,
) -> MigrationRehearsalResult:
    """Stamp a copy while proving that the source database is unchanged."""
    source_path = get_sqlite_database_path(database_url)
    backup_directory = Path(backup_directory).resolve()
    source_fingerprint_before = get_business_fingerprint(
        source_path
    )
    backup_path, rehearsal_path = build_rehearsal_paths(
        source_path,
        backup_directory,
    )

    create_sqlite_snapshot(source_path, backup_path)
    create_sqlite_snapshot(backup_path, rehearsal_path)

    backup_fingerprint = get_business_fingerprint(backup_path)
    if backup_fingerprint != source_fingerprint_before:
        raise MigrationRehearsalError(
            "原始快照与源数据库不一致，停止演练"
        )

    rehearsal_url = build_sqlite_database_url(rehearsal_path)
    check_result = validate_and_adopt_baseline(rehearsal_url)
    validate_and_adopt_baseline(
        rehearsal_url,
        apply=True,
    )
    repeat_result = validate_and_adopt_baseline(
        rehearsal_url,
        apply=True,
    )

    if (
        get_current_revision(rehearsal_path)
        != BASELINE_REVISION
        or not repeat_result.already_adopted
    ):
        raise MigrationRehearsalError(
            "演练副本未稳定停留在初始基线"
        )

    rehearsal_fingerprint = get_business_fingerprint(
        rehearsal_path
    )
    source_fingerprint_after = get_business_fingerprint(
        source_path
    )
    if rehearsal_fingerprint != backup_fingerprint:
        raise MigrationRehearsalError(
            "写入版本标记后业务结构或数据发生变化"
        )
    if source_fingerprint_after != source_fingerprint_before:
        raise MigrationRehearsalError(
            "演练期间源数据库发生变化，结果无效"
        )

    return MigrationRehearsalResult(
        source_database=source_path,
        backup_database=backup_path,
        rehearsal_database=rehearsal_path,
        revision=BASELINE_REVISION,
        business_fingerprint=backup_fingerprint,
        source_was_already_adopted=(
            check_result.already_adopted
        ),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "在 SQLite 副本上演练 Alembic 初始基线接入"
        )
    )
    parser.add_argument(
        "--backup-dir",
        type=Path,
        default=DEFAULT_BACKUP_DIRECTORY,
        help="原始快照和演练副本的保存目录",
    )
    args = parser.parse_args(argv)

    try:
        result = rehearse_baseline_adoption(
            resolve_database_url(),
            backup_directory=args.backup_dir,
        )
    except (
        MigrationRehearsalError,
        BaselineAdoptionError,
        OSError,
        sqlite3.Error,
    ) as exc:
        parser.exit(1, f"基线副本演练失败：{exc}\n")

    print("基线副本演练通过：源数据库未修改")
    print(f"源数据库：{result.source_database}")
    print(
        "源库基线状态："
        + (
            "已接入"
            if result.source_was_already_adopted
            else "未接入"
        )
    )
    print(f"原始快照：{result.backup_database}")
    print(f"演练副本：{result.rehearsal_database}")
    print(f"版本标记：{result.revision}")
    print(f"业务指纹：{result.business_fingerprint}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
