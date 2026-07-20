import sqlite3
from pathlib import Path


DB_PATH = Path("saas_mvp.db")
TABLE_NAME = "users"
COLUMN_NAME = "is_active"


def get_column_names(connection: sqlite3.Connection) -> set[str]:
    rows = connection.execute(
        f"PRAGMA table_info({TABLE_NAME})"
    ).fetchall()

    return {row[1] for row in rows}


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

        column_names = get_column_names(connection)

        if COLUMN_NAME not in column_names:
            connection.execute(
                """
                ALTER TABLE users
                ADD COLUMN is_active
                INTEGER NOT NULL DEFAULT 1
                """
            )

            print(
                "已新增字段："
                "users.is_active"
            )
        else:
            print(
                "字段已存在："
                "users.is_active"
            )

        normalized_count = connection.execute(
            """
            UPDATE users
            SET is_active = 1
            WHERE is_active IS NULL
            """
        ).rowcount

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
                is_active,
                COUNT(*)
            FROM users
            GROUP BY is_active
            ORDER BY is_active
            """
        ).fetchall()

        total_users = connection.execute(
            "SELECT COUNT(*) FROM users"
        ).fetchone()[0]

        print(
            "补全空状态记录数：",
            normalized_count,
        )
        print(
            "用户总数：",
            total_users,
        )
        print(
            "账号状态分布：",
            distribution,
        )
        print(
            "quick_check：",
            quick_check,
        )
        print("账号状态字段迁移完成")

    except Exception:
        connection.rollback()
        raise

    finally:
        connection.close()


if __name__ == "__main__":
    main()