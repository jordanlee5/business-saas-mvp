import sqlite3
from pathlib import Path


DB_PATH = Path("saas_mvp.db")
TABLE_NAME = "users"
COLUMN_NAME = "must_change_password"


def get_column_names(
    connection: sqlite3.Connection,
) -> set[str]:
    rows = connection.execute(
        f"PRAGMA table_info({TABLE_NAME})"
    ).fetchall()

    return {row[1] for row in rows}


def migrate(
    connection: sqlite3.Connection,
) -> tuple[bool, int]:
    column_names = get_column_names(
        connection
    )
    column_added = False

    if COLUMN_NAME not in column_names:
        connection.execute(
            """
            ALTER TABLE users
            ADD COLUMN must_change_password
            INTEGER NOT NULL DEFAULT 0
            """
        )
        column_added = True

    normalized_count = connection.execute(
        """
        UPDATE users
        SET must_change_password = 0
        WHERE must_change_password IS NULL
        """
    ).rowcount

    return (
        column_added,
        normalized_count,
    )


def main() -> None:
    if not DB_PATH.exists():
        raise FileNotFoundError(
            f"未找到数据库文件：{DB_PATH.resolve()}"
        )

    connection = sqlite3.connect(DB_PATH)

    try:
        table_exists = connection.execute(
            """
            SELECT 1
            FROM sqlite_master
            WHERE type = 'table'
              AND name = ?
            """,
            (TABLE_NAME,),
        ).fetchone()

        if not table_exists:
            raise RuntimeError(
                f"数据库中不存在数据表：{TABLE_NAME}"
            )

        (
            column_added,
            normalized_count,
        ) = migrate(connection)

        quick_check = connection.execute(
            "PRAGMA quick_check"
        ).fetchone()[0]

        if quick_check != "ok":
            raise RuntimeError(
                f"数据库完整性检查失败：{quick_check}"
            )

        connection.commit()

        distribution = connection.execute(
            """
            SELECT
                must_change_password,
                COUNT(*)
            FROM users
            GROUP BY must_change_password
            ORDER BY must_change_password
            """
        ).fetchall()

        print(
            "字段处理：",
            (
                "已新增 users.must_change_password"
                if column_added
                else "字段已存在，无需重复新增"
            ),
        )
        print(
            "补全空状态记录数：",
            normalized_count,
        )
        print(
            "首次改密状态分布：",
            distribution,
        )
        print(
            "quick_check：",
            quick_check,
        )
        print("首次改密字段迁移完成")

    except Exception:
        connection.rollback()
        raise

    finally:
        connection.close()


if __name__ == "__main__":
    main()