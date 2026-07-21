import sqlite3
from pathlib import Path


DB_PATH = Path("saas_mvp.db")
TABLE_NAME = "users"
COLUMN_NAME = "admin_level"

SUPER_ADMIN = "super_admin"

VALID_ADMIN_LEVELS = (
    "super_admin",
    "primary_reviewer",
    "secondary_reviewer",
    "operator",
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

        if COLUMN_NAME not in column_names:
            connection.execute(
                """
                ALTER TABLE users
                ADD COLUMN admin_level VARCHAR(30)
                """
            )
            print("已新增字段：users.admin_level")
        else:
            print("字段已存在：users.admin_level")

        migrated_admin_count = connection.execute(
            """
            UPDATE users
            SET admin_level = ?
            WHERE role = 'admin'
              AND (
                    admin_level IS NULL
                    OR TRIM(admin_level) = ''
                  )
            """,
            (SUPER_ADMIN,),
        ).rowcount

        cleared_partner_count = connection.execute(
            """
            UPDATE users
            SET admin_level = NULL
            WHERE COALESCE(role, '') <> 'admin'
              AND admin_level IS NOT NULL
            """
        ).rowcount

        placeholders = ", ".join(
            "?" for _ in VALID_ADMIN_LEVELS
        )

        invalid_rows = connection.execute(
            f"""
            SELECT
                id,
                username,
                role,
                admin_level
            FROM users
            WHERE
                (
                    role = 'admin'
                    AND (
                        admin_level IS NULL
                        OR admin_level NOT IN ({placeholders})
                    )
                )
                OR
                (
                    COALESCE(role, '') <> 'admin'
                    AND admin_level IS NOT NULL
                )
            ORDER BY id
            """,
            VALID_ADMIN_LEVELS,
        ).fetchall()

        if invalid_rows:
            raise RuntimeError(
                "发现无效管理员级别记录："
                f"{invalid_rows}"
            )

        total_users = connection.execute(
            "SELECT COUNT(*) FROM users"
        ).fetchone()[0]

        total_admins = connection.execute(
            """
            SELECT COUNT(*)
            FROM users
            WHERE role = 'admin'
            """
        ).fetchone()[0]

        super_admin_count = connection.execute(
            """
            SELECT COUNT(*)
            FROM users
            WHERE role = 'admin'
              AND admin_level = 'super_admin'
            """
        ).fetchone()[0]

        if total_admins > 0 and super_admin_count < 1:
            raise RuntimeError(
                "系统存在管理员，但没有超级管理员"
            )

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
                role,
                COALESCE(admin_level, 'NULL'),
                COUNT(*)
            FROM users
            GROUP BY role, admin_level
            ORDER BY role, admin_level
            """
        ).fetchall()

        print("迁移为超级管理员数量：", migrated_admin_count)
        print("清理非管理员级别数量：", cleared_partner_count)
        print("用户总数：", total_users)
        print("管理员总数：", total_admins)
        print("超级管理员数量：", super_admin_count)
        print("角色与管理员级别分布：", distribution)
        print("quick_check：", quick_check)
        print("管理员级别字段迁移完成")

    except Exception:
        connection.rollback()
        raise

    finally:
        connection.close()


if __name__ == "__main__":
    main()
