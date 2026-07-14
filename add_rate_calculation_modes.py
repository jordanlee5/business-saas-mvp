import sqlite3
from pathlib import Path


DB_PATH = Path("saas_mvp.db")

DEFAULT_MODE = "external"
VALID_MODES = ("external", "internal")

TABLE_COLUMNS = {
    "users": {
        "service_rate_mode": (
            "TEXT NOT NULL DEFAULT 'external'"
        ),
        "upstream_cost_rate_mode": (
            "TEXT NOT NULL DEFAULT 'external'"
        ),
    },
    "business_records": {
        "record_service_rate_mode": (
            "TEXT NOT NULL DEFAULT 'external'"
        ),
        "record_upstream_cost_rate_mode": (
            "TEXT NOT NULL DEFAULT 'external'"
        ),
    },
}


def table_exists(
    cursor: sqlite3.Cursor,
    table_name: str,
) -> bool:
    cursor.execute(
        """
        SELECT 1
        FROM sqlite_master
        WHERE type = 'table'
        AND name = ?
        """,
        (table_name,),
    )

    return cursor.fetchone() is not None


def get_table_columns(
    cursor: sqlite3.Cursor,
    table_name: str,
) -> set[str]:
    cursor.execute(
        f"PRAGMA table_info({table_name})"
    )

    return {
        row[1]
        for row in cursor.fetchall()
    }


def add_missing_columns(
    cursor: sqlite3.Cursor,
) -> None:
    for table_name, columns in TABLE_COLUMNS.items():
        if not table_exists(cursor, table_name):
            raise RuntimeError(
                f"数据库中不存在数据表：{table_name}"
            )

        existing_columns = get_table_columns(
            cursor,
            table_name,
        )

        for column_name, column_definition in columns.items():
            if column_name in existing_columns:
                print(
                    "字段已存在："
                    f"{table_name}.{column_name}"
                )
                continue

            cursor.execute(
                f"""
                ALTER TABLE {table_name}
                ADD COLUMN {column_name}
                {column_definition}
                """
            )

            print(
                "已新增字段："
                f"{table_name}.{column_name}"
            )


def normalize_mode_values(
    cursor: sqlite3.Cursor,
) -> None:
    """
    空值、空字符串或未知值统一回填为 external。

    目前历史系统只有外扣算法，因此历史数据必须
    统一回填为 external，保证迁移前后金额不变。
    """
    for table_name, columns in TABLE_COLUMNS.items():
        for column_name in columns:
            cursor.execute(
                f"""
                UPDATE {table_name}
                SET {column_name} = ?
                WHERE {column_name} IS NULL
                OR TRIM({column_name}) = ''
                OR {column_name} NOT IN (?, ?)
                """,
                (
                    DEFAULT_MODE,
                    VALID_MODES[0],
                    VALID_MODES[1],
                ),
            )

            print(
                "已规范字段："
                f"{table_name}.{column_name}，"
                f"更新行数：{cursor.rowcount}"
            )


def print_mode_distribution(
    cursor: sqlite3.Cursor,
) -> None:
    print("\n迁移后计算方式分布：")

    for table_name, columns in TABLE_COLUMNS.items():
        for column_name in columns:
            cursor.execute(
                f"""
                SELECT
                    {column_name},
                    COUNT(*)
                FROM {table_name}
                GROUP BY {column_name}
                ORDER BY {column_name}
                """
            )

            rows = cursor.fetchall()

            print(
                f"{table_name}.{column_name}: "
                f"{rows}"
            )


def main() -> None:
    if not DB_PATH.exists():
        raise FileNotFoundError(
            f"找不到数据库文件：{DB_PATH}"
        )

    connection = sqlite3.connect(DB_PATH)

    try:
        cursor = connection.cursor()

        cursor.execute("BEGIN")

        add_missing_columns(cursor)
        normalize_mode_values(cursor)

        connection.commit()

        print_mode_distribution(cursor)

        print("\n费率计算方式字段迁移完成")

    except Exception:
        connection.rollback()

        print(
            "\n迁移失败，已回滚本次数据库操作"
        )

        raise

    finally:
        connection.close()


if __name__ == "__main__":
    main()