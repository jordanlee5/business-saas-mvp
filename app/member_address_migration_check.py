"""Read-only SQLite source inspection and address migration on a NEW copy only.

Run from the repository root. The output contains a private database copy; never
upload it or put it in Git. This command has no live-upgrade mode.
"""
from contextlib import closing
import argparse
import hashlib
import json
from pathlib import Path
import sqlite3

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from .schema_readiness import (assert_database_schema_ready, CURRENT_SCHEMA_REVISION,
                               REQUIRED_APPLICATION_TABLES, REQUIRED_MALL_CORE_COLUMNS)

PREVIOUS = "0011_supplier_settlement_foundation"
CURRENT = "0012_member_addresses"
ADDED = {"member_addresses", "member_address_events"}
ROOT = Path(__file__).resolve().parents[1]


def file_fingerprints(source):
    return {("database" if not suffix else "wal"): hashlib.sha256(Path(str(source) + suffix).read_bytes()).hexdigest()
            for suffix in ("", "-wal") if Path(str(source) + suffix).is_file()}


def connect_readonly(source):
    return sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)


def snapshot(connection):
    if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
        raise RuntimeError("SQLite完整性检查失败")
    if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise RuntimeError("SQLite存在外键异常")
    names = [row[0] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    revisions = connection.execute("SELECT version_num FROM alembic_version").fetchall()
    if len(revisions) != 1:
        raise RuntimeError("数据库版本标记不唯一")
    tables = {}
    for name in names:
        if name == "alembic_version":
            continue
        quoted = '"' + name.replace('"', '""') + '"'
        # Keep only hashes in reports. Sort row hashes, preserving duplicates.
        hashes = sorted(hashlib.sha256(repr(tuple(row)).encode("utf-8")).hexdigest()
                        for row in connection.execute("SELECT * FROM " + quoted))
        schema = connection.execute(
            "SELECT type,name,sql FROM sqlite_master WHERE tbl_name=? ORDER BY type,name", (name,)
        ).fetchall()
        tables[name] = {"rows": len(hashes),
                       "data_sha256": hashlib.sha256("\n".join(hashes).encode()).hexdigest(),
                       "schema_sha256": hashlib.sha256(repr(schema).encode()).hexdigest()}
    return {"revision": revisions[0][0], "tables": tables}


def assert_preserved(before, after):
    if after["revision"] != CURRENT or set(after["tables"]) != set(before["tables"]) | ADDED:
        raise RuntimeError("迁移后版本或表集合不符")
    if any(after["tables"][name] != value for name, value in before["tables"].items()):
        raise RuntimeError("既有业务表结构或数据发生变化")
    if any(after["tables"][name]["rows"] for name in ADDED):
        raise RuntimeError("新增地址表不是空表")


def rehearse(source, output):
    source, output = Path(source).resolve(), Path(output).resolve()
    if not source.is_file() or output.exists():
        raise RuntimeError("源数据库必须存在，演练目录必须为新路径")
    fingerprints = file_fingerprints(source)
    output.mkdir(parents=True)
    copied = output / "rehearsal.db"
    with closing(connect_readonly(source)) as original:
        before = snapshot(original)
        if before["revision"] != PREVIOUS or ADDED & set(before["tables"]):
            raise RuntimeError("真实库必须为0011且尚无地址表，停止演练")
        with closing(sqlite3.connect(copied)) as target:
            original.backup(target)
    with closing(connect_readonly(copied)) as target:
        if snapshot(target) != before:
            raise RuntimeError("副本与源数据库逻辑快照不一致")
    config = Config(str(ROOT / "alembic.ini"))
    config.attributes["database_url"] = "sqlite:///" + copied.as_posix()
    command.upgrade(config, CURRENT)
    with closing(connect_readonly(copied)) as target:
        after = snapshot(target)
    assert_preserved(before, after)
    engine = create_engine(config.attributes["database_url"])
    try:
        if CURRENT == CURRENT_SCHEMA_REVISION:
            assert_database_schema_ready(engine)
        else:
            # Keep the historical 0012 gate strict while excluding only the explicitly
            # registered 0013 table. A historical rehearsal is not current readiness.
            later_tables = {'order_delivery_snapshots'}
            with engine.connect() as connection:
                inspector = inspect(connection)
                if connection.execute(text('select version_num from alembic_version')).scalar() != CURRENT:
                    raise RuntimeError('历史地址迁移版本不符')
                if (REQUIRED_APPLICATION_TABLES - later_tables) - set(inspector.get_table_names()):
                    raise RuntimeError('历史地址迁移后缺少既有表')
                for table, columns in REQUIRED_MALL_CORE_COLUMNS.items():
                    if table in later_tables:
                        continue
                    if columns - {column['name'] for column in inspector.get_columns(table)}:
                        raise RuntimeError('历史地址迁移后缺少既有字段')
    finally:
        engine.dispose()
    # Empty new tables only; exercise the guarded rollback and re-upgrade.
    command.downgrade(config, PREVIOUS)
    with closing(connect_readonly(copied)) as target:
        if snapshot(target) != before:
            raise RuntimeError("副本降级后逻辑快照不一致")
    command.upgrade(config, CURRENT)
    with closing(connect_readonly(copied)) as target:
        assert_preserved(before, snapshot(target))
    with closing(connect_readonly(source)) as original:
        if snapshot(original) != before or file_fingerprints(source) != fingerprints:
            raise RuntimeError("演练期间真实库变化，停止并重新核验")
    report = {"result": "PASS", "source": str(source), "source_files": fingerprints,
              "before": before, "after": after, "source_unchanged": True,
              "copy_upgrade_downgrade_reupgrade": "PASS", "live_upgrade": "NOT_EXECUTED"}
    (output / "rehearsal-result.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    result = rehearse(args.source, args.output)
    for name in ("source_unchanged", "copy_upgrade_downgrade_reupgrade", "live_upgrade", "result"):
        print(f"{name}={result[name]}")
    print("report=" + str(Path(args.output).resolve() / "rehearsal-result.json"))


if __name__ == "__main__":
    main()
