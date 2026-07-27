import sqlite3
from pathlib import Path


DB_PATH = Path("saas_mvp.db")
TABLE_NAME = "match_reviews"

COLUMNS = (
    ("primary_reviewer_id", "INTEGER"),
    ("primary_review_result", "VARCHAR(30)"),
    ("primary_review_comment", "TEXT"),
    ("primary_reviewed_at", "DATETIME"),
    ("secondary_reviewer_id", "INTEGER"),
    ("secondary_review_result", "VARCHAR(30)"),
    ("secondary_review_comment", "TEXT"),
    ("secondary_reviewed_at", "DATETIME"),
)


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
        added_columns = []

        for column_name, column_definition in COLUMNS:
            if column_name in column_names:
                print(f"字段已存在：{TABLE_NAME}.{column_name}")
                continue

            connection.execute(
                f"""
                ALTER TABLE {TABLE_NAME}
                ADD COLUMN {column_name} {column_definition}
                """
            )
            added_columns.append(column_name)
            print(f"已新增字段：{TABLE_NAME}.{column_name}")

        final_column_names = get_column_names(connection)
        missing_columns = [
            column_name
            for column_name, _ in COLUMNS
            if column_name not in final_column_names
        ]

        if missing_columns:
            raise RuntimeError(
                f"审核流程字段迁移不完整：{missing_columns}"
            )

        quick_check = connection.execute(
            "PRAGMA quick_check"
        ).fetchone()[0]

        if quick_check != "ok":
            raise RuntimeError(
                f"数据库完整性检查失败：{quick_check}"
            )

        connection.commit()

        status_distribution = connection.execute(
            """
            SELECT
                COALESCE(review_status, 'NULL'),
                COUNT(*)
            FROM match_reviews
            GROUP BY review_status
            ORDER BY review_status
            """
        ).fetchall()

        print("本次新增字段：", added_columns)
        print("审核状态分布：", status_distribution)
        print("quick_check：", quick_check)
        print("匹配审核流程字段迁移完成")

    except Exception:
        connection.rollback()
        raise

    finally:
        connection.close()


if __name__ == "__main__":
    main()