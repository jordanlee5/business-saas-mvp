import sqlite3

from app.business_no import generate_public_business_no


DB_PATH = "saas_mvp.db"
TABLE_NAME = "business_records"
COLUMN_NAME = "public_business_no"
UNIQUE_INDEX_NAME = "ix_business_records_public_business_no"


def generate_unique_business_no(
    used_numbers: set[str],
    max_attempts: int = 100,
) -> str:
    """
    在当前数据库已使用编号集合内生成一个唯一公开业务单号。
    """
    for _ in range(max_attempts):
        candidate = generate_public_business_no()

        if candidate not in used_numbers:
            return candidate

    raise RuntimeError("多次生成公开业务单号均发生重复，请停止迁移并检查。")


def main() -> None:
    connection = sqlite3.connect(DB_PATH)

    try:
        cursor = connection.cursor()

        cursor.execute(f"PRAGMA table_info({TABLE_NAME})")
        columns = [row[1] for row in cursor.fetchall()]

        if COLUMN_NAME not in columns:
            cursor.execute(
                f"""
                ALTER TABLE {TABLE_NAME}
                ADD COLUMN {COLUMN_NAME} TEXT
                """
            )
            print(f"已新增字段：{TABLE_NAME}.{COLUMN_NAME}")
        else:
            print(f"字段已存在：{TABLE_NAME}.{COLUMN_NAME}")

        cursor.execute(
            f"""
            SELECT {COLUMN_NAME}
            FROM {TABLE_NAME}
            WHERE {COLUMN_NAME} IS NOT NULL
              AND TRIM({COLUMN_NAME}) != ''
            """
        )

        used_numbers = {
            row[0]
            for row in cursor.fetchall()
            if row[0]
        }

        cursor.execute(
            f"""
            SELECT id
            FROM {TABLE_NAME}
            WHERE {COLUMN_NAME} IS NULL
               OR TRIM({COLUMN_NAME}) = ''
            ORDER BY id ASC
            """
        )

        record_ids = [row[0] for row in cursor.fetchall()]

        updated_count = 0

        for record_id in record_ids:
            public_business_no = generate_unique_business_no(
                used_numbers
            )

            cursor.execute(
                f"""
                UPDATE {TABLE_NAME}
                SET {COLUMN_NAME} = ?
                WHERE id = ?
                """,
                (
                    public_business_no,
                    record_id,
                ),
            )

            used_numbers.add(public_business_no)
            updated_count += 1

        # SQLite 无法通过 ALTER TABLE 直接给已有字段增加 UNIQUE，
        # 因此使用唯一索引保证公开业务单号不重复。
        cursor.execute(
            f"""
            CREATE UNIQUE INDEX IF NOT EXISTS
            {UNIQUE_INDEX_NAME}
            ON {TABLE_NAME} ({COLUMN_NAME})
            """
        )

        connection.commit()

        print(f"历史业务数据补齐数量：{updated_count}")
        print(f"公开业务单号总数量：{len(used_numbers)}")
        print("公开业务单号迁移完成")

    except Exception:
        connection.rollback()
        print("迁移失败，已回滚本次数据库操作")
        raise

    finally:
        connection.close()


if __name__ == "__main__":
    main()