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

            if get_schema_differences(connection):
                raise BaselineAdoptionError(
                    "数据库结构与初始基线不一致，未写入版本标记"
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
