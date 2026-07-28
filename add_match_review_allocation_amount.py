import sqlite3
from pathlib import Path


DB_PATH = Path("saas_mvp.db")
MATCH_REVIEW_TABLE = "match_reviews"
VOUCHER_TABLE = "voucher_records"
BUSINESS_TABLE = "business_records"
ALLOCATION_COLUMN = "allocation_amount"
BACKFILL_STATUSES = (
    "已通过",
    "待复核",
)


def table_exists(
    connection: sqlite3.Connection,
    table_name: str,
) -> bool:
    row = connection.execute(
        """
        SELECT 1
        FROM sqlite_master
        WHERE type = 'table'
          AND name = ?
        """,
        (table_name,),
    ).fetchone()

    return row is not None


def get_column_names(
    connection: sqlite3.Connection,
    table_name: str,
) -> set[str]:
    rows = connection.execute(
        f"PRAGMA table_info({table_name})"
    ).fetchall()

    return {
        row[1]
        for row in rows
    }


def require_tables(
    connection: sqlite3.Connection,
) -> None:
    required_tables = (
        MATCH_REVIEW_TABLE,
        VOUCHER_TABLE,
        BUSINESS_TABLE,
    )
    missing_tables = [
        table_name
        for table_name in required_tables
        if not table_exists(
            connection,
            table_name,
        )
    ]

    if missing_tables:
        raise RuntimeError(
            f"数据库缺少必要数据表：{missing_tables}"
        )


def add_allocation_column(
    connection: sqlite3.Connection,
) -> bool:
    column_names = get_column_names(
        connection,
        MATCH_REVIEW_TABLE,
    )

    if ALLOCATION_COLUMN in column_names:
        return False

    connection.execute(
        f"""
        ALTER TABLE {MATCH_REVIEW_TABLE}
        ADD COLUMN {ALLOCATION_COLUMN}
        NUMERIC(12, 2)
        """
    )

    return True


def backfill_historical_allocations(
    connection: sqlite3.Connection,
) -> int:
    """
    保留旧系统的金额口径：

    1. 历史已通过记录按整张凭证金额回填；
    2. 历史待复核记录按整张凭证金额回填为拟核销金额；
    3. 其他状态保持为空，等待后续初审时明确填写；
    4. 已有核销金额绝不覆盖，保证脚本可重复执行。
    """
    cursor = connection.execute(
        f"""
        UPDATE {MATCH_REVIEW_TABLE}
        SET {ALLOCATION_COLUMN} = (
            SELECT ROUND(
                {VOUCHER_TABLE}.voucher_amount,
                2
            )
            FROM {VOUCHER_TABLE}
            WHERE {VOUCHER_TABLE}.id
                = {MATCH_REVIEW_TABLE}.voucher_id
        )
        WHERE {ALLOCATION_COLUMN} IS NULL
          AND review_status IN (?, ?)
          AND EXISTS (
              SELECT 1
              FROM {VOUCHER_TABLE}
              WHERE {VOUCHER_TABLE}.id
                  = {MATCH_REVIEW_TABLE}.voucher_id
                AND ROUND(
                    {VOUCHER_TABLE}.voucher_amount,
                    2
                ) > 0
          )
        """,
        BACKFILL_STATUSES,
    )

    return cursor.rowcount


def count_rows(
    connection: sqlite3.Connection,
    where_clause: str,
    parameters: tuple = (),
) -> int:
    row = connection.execute(
        f"""
        SELECT COUNT(*)
        FROM {MATCH_REVIEW_TABLE}
        WHERE {where_clause}
        """,
        parameters,
    ).fetchone()

    return int(row[0])


def count_business_overpayments(
    connection: sqlite3.Connection,
) -> int:
    row = connection.execute(
        f"""
        SELECT COUNT(*)
        FROM (
            SELECT
                reviews.business_record_id
            FROM {MATCH_REVIEW_TABLE} AS reviews
            JOIN {BUSINESS_TABLE} AS businesses
              ON businesses.id
                = reviews.business_record_id
            WHERE reviews.review_status = '已通过'
              AND reviews.{ALLOCATION_COLUMN}
                IS NOT NULL
            GROUP BY reviews.business_record_id
            HAVING ROUND(
                SUM(reviews.{ALLOCATION_COLUMN}),
                2
            ) > ROUND(
                MAX(businesses.points_amount),
                2
            )
        )
        """
    ).fetchone()

    return int(row[0])


def count_voucher_overallocations(
    connection: sqlite3.Connection,
) -> int:
    row = connection.execute(
        f"""
        SELECT COUNT(*)
        FROM (
            SELECT
                reviews.voucher_id
            FROM {MATCH_REVIEW_TABLE} AS reviews
            JOIN {VOUCHER_TABLE} AS vouchers
              ON vouchers.id = reviews.voucher_id
            WHERE reviews.review_status = '已通过'
              AND reviews.{ALLOCATION_COLUMN}
                IS NOT NULL
            GROUP BY reviews.voucher_id
            HAVING ROUND(
                SUM(reviews.{ALLOCATION_COLUMN}),
                2
            ) > ROUND(
                MAX(vouchers.voucher_amount),
                2
            )
        )
        """
    ).fetchone()

    return int(row[0])


def build_summary(
    connection: sqlite3.Connection,
) -> dict[str, int]:
    return {
        "approved_total": count_rows(
            connection,
            "review_status = ?",
            ("已通过",),
        ),
        "approved_with_allocation": count_rows(
            connection,
            (
                "review_status = ? "
                f"AND {ALLOCATION_COLUMN} IS NOT NULL"
            ),
            ("已通过",),
        ),
        "approved_missing_allocation": count_rows(
            connection,
            (
                "review_status = ? "
                f"AND {ALLOCATION_COLUMN} IS NULL"
            ),
            ("已通过",),
        ),
        "pending_secondary_total": count_rows(
            connection,
            "review_status = ?",
            ("待复核",),
        ),
        "pending_secondary_with_allocation": count_rows(
            connection,
            (
                "review_status = ? "
                f"AND {ALLOCATION_COLUMN} IS NOT NULL"
            ),
            ("待复核",),
        ),
        "business_overpayments": (
            count_business_overpayments(
                connection
            )
        ),
        "voucher_overallocations": (
            count_voucher_overallocations(
                connection
            )
        ),
    }


def migrate(
    connection: sqlite3.Connection,
) -> tuple[bool, int, dict[str, int]]:
    require_tables(connection)

    column_added = add_allocation_column(
        connection
    )
    backfilled_rows = (
        backfill_historical_allocations(
            connection
        )
    )

    final_columns = get_column_names(
        connection,
        MATCH_REVIEW_TABLE,
    )

    if ALLOCATION_COLUMN not in final_columns:
        raise RuntimeError(
            "本次核销金额字段迁移不完整"
        )

    quick_check = connection.execute(
        "PRAGMA quick_check"
    ).fetchone()[0]

    if quick_check != "ok":
        raise RuntimeError(
            f"数据库完整性检查失败：{quick_check}"
        )

    return (
        column_added,
        backfilled_rows,
        build_summary(connection),
    )


def print_summary(
    column_added: bool,
    backfilled_rows: int,
    summary: dict[str, int],
) -> None:
    if column_added:
        print(
            "已新增字段："
            "match_reviews.allocation_amount"
        )
    else:
        print(
            "字段已存在："
            "match_reviews.allocation_amount"
        )

    print(
        "本次回填历史记录数：",
        backfilled_rows,
    )
    print(
        "历史已通过记录：",
        summary["approved_total"],
    )
    print(
        "已通过且已有核销金额：",
        summary["approved_with_allocation"],
    )
    print(
        "已通过但缺少核销金额：",
        summary["approved_missing_allocation"],
    )
    print(
        "待复核记录：",
        summary["pending_secondary_total"],
    )
    print(
        "待复核且已有拟核销金额：",
        summary[
            "pending_secondary_with_allocation"
        ],
    )
    print(
        "历史业务超额核销数量：",
        summary["business_overpayments"],
    )
    print(
        "历史凭证超额分配数量：",
        summary["voucher_overallocations"],
    )
    print("quick_check：ok")
    print("本次核销金额字段迁移完成")


def main() -> None:
    if not DB_PATH.exists():
        raise FileNotFoundError(
            f"未找到数据库文件：{DB_PATH.resolve()}"
        )

    connection = sqlite3.connect(DB_PATH)

    try:
        connection.execute("BEGIN")

        (
            column_added,
            backfilled_rows,
            summary,
        ) = migrate(connection)

        connection.commit()

        print_summary(
            column_added=column_added,
            backfilled_rows=backfilled_rows,
            summary=summary,
        )

    except Exception:
        connection.rollback()
        print("迁移失败，已回滚本次数据库操作")
        raise

    finally:
        connection.close()


if __name__ == "__main__":
    main()