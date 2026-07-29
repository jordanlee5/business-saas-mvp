import sqlite3
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path


DB_PATH = Path("saas_mvp.db")
CENT = Decimal("0.01")

REQUIRED_COLUMNS = {
    "match_reviews": {
        "id",
        "voucher_id",
        "business_record_id",
        "review_status",
        "allocation_amount",
    },
    "business_records": {
        "id",
        "business_no",
        "public_business_no",
        "points_amount",
    },
    "voucher_records": {
        "id",
        "voucher_amount",
    },
}

DISPLAY_BUSINESS_NO_SQL = """
COALESCE(
    NULLIF(TRIM(b.public_business_no), ''),
    NULLIF(TRIM(b.business_no), ''),
    CASE
        WHEN b.id IS NOT NULL THEN 'BR-' || b.id
        WHEN r.business_record_id IS NOT NULL
            THEN '缺失业务#' || r.business_record_id
        ELSE '缺失业务#NULL'
    END
)
"""


def money(value) -> str:
    if value is None:
        return "NULL"

    try:
        normalized = Decimal(str(value)).quantize(
            CENT,
            rounding=ROUND_HALF_UP,
        )
    except (InvalidOperation, ValueError) as exc:
        raise RuntimeError(
            f"数据库中存在无效金额：{value!r}"
        ) from exc

    return f"{normalized:.2f}"


def open_read_only_database() -> sqlite3.Connection:
    if not DB_PATH.exists():
        raise FileNotFoundError(
            f"未找到数据库文件：{DB_PATH.resolve()}"
        )

    uri = f"{DB_PATH.resolve().as_uri()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")

    if connection.execute(
        "PRAGMA query_only"
    ).fetchone()[0] != 1:
        connection.close()
        raise RuntimeError("数据库只读保护未启用")

    return connection


def validate_schema(
    connection: sqlite3.Connection,
) -> None:
    for table_name, required in REQUIRED_COLUMNS.items():
        actual = {
            row["name"]
            for row in connection.execute(
                f"PRAGMA table_info({table_name})"
            )
        }
        missing = required - actual

        if missing:
            raise RuntimeError(
                f"{table_name} 缺少字段："
                f"{sorted(missing)}"
            )


def missing_reason(row: sqlite3.Row) -> str:
    if row["voucher_id"] is None:
        return "匹配记录未关联凭证"

    if row["voucher_row_id"] is None:
        return "关联的凭证记录不存在"

    if row["voucher_amount"] is None:
        return "凭证金额为空，迁移无法回填"

    amount = Decimal(
        str(row["voucher_amount"])
    ).quantize(
        CENT,
        rounding=ROUND_HALF_UP,
    )

    if amount <= Decimal("0.00"):
        return "凭证金额为 0 或负数，迁移按规则未回填"

    return "金额大于 0 但仍为空，需要核对迁移执行情况"


def fetch_missing(
    connection: sqlite3.Connection,
) -> list[sqlite3.Row]:
    return connection.execute(
        f"""
        SELECT
            r.id AS review_id,
            r.business_record_id,
            {DISPLAY_BUSINESS_NO_SQL}
                AS display_business_no,
            b.points_amount AS business_amount,
            r.voucher_id,
            v.id AS voucher_row_id,
            v.voucher_amount,
            (
                SELECT GROUP_CONCAT(x.id, ',')
                FROM match_reviews AS x
                WHERE x.business_record_id
                    = r.business_record_id
                  AND x.review_status = '已通过'
            ) AS same_business_review_ids,
            (
                SELECT ROUND(
                    SUM(x.allocation_amount),
                    2
                )
                FROM match_reviews AS x
                WHERE x.business_record_id
                    = r.business_record_id
                  AND x.review_status = '已通过'
                  AND x.allocation_amount IS NOT NULL
            ) AS business_allocated,
            (
                SELECT GROUP_CONCAT(x.id, ',')
                FROM match_reviews AS x
                WHERE x.voucher_id = r.voucher_id
                  AND x.review_status = '已通过'
            ) AS same_voucher_review_ids,
            (
                SELECT ROUND(
                    SUM(x.allocation_amount),
                    2
                )
                FROM match_reviews AS x
                WHERE x.voucher_id = r.voucher_id
                  AND x.review_status = '已通过'
                  AND x.allocation_amount IS NOT NULL
            ) AS voucher_allocated
        FROM match_reviews AS r
        LEFT JOIN business_records AS b
          ON b.id = r.business_record_id
        LEFT JOIN voucher_records AS v
          ON v.id = r.voucher_id
        WHERE r.review_status = '已通过'
          AND r.allocation_amount IS NULL
        ORDER BY r.id
        """
    ).fetchall()


def fetch_business_overpayments(
    connection: sqlite3.Connection,
) -> list[sqlite3.Row]:
    return connection.execute(
        """
        SELECT
            r.business_record_id,
            COALESCE(
                NULLIF(TRIM(b.public_business_no), ''),
                NULLIF(TRIM(b.business_no), ''),
                'BR-' || b.id
            ) AS display_business_no,
            MAX(b.points_amount) AS business_amount,
            ROUND(
                SUM(r.allocation_amount),
                2
            ) AS allocated_amount,
            ROUND(
                SUM(r.allocation_amount)
                - MAX(b.points_amount),
                2
            ) AS excess_amount
        FROM match_reviews AS r
        JOIN business_records AS b
          ON b.id = r.business_record_id
        WHERE r.review_status = '已通过'
          AND r.allocation_amount IS NOT NULL
        GROUP BY r.business_record_id
        HAVING ROUND(
            SUM(r.allocation_amount),
            2
        ) > ROUND(
            MAX(b.points_amount),
            2
        )
        ORDER BY r.business_record_id
        """
    ).fetchall()


def fetch_voucher_overallocations(
    connection: sqlite3.Connection,
) -> list[sqlite3.Row]:
    return connection.execute(
        """
        SELECT
            r.voucher_id,
            MAX(v.voucher_amount) AS voucher_amount,
            ROUND(
                SUM(r.allocation_amount),
                2
            ) AS allocated_amount,
            ROUND(
                SUM(r.allocation_amount)
                - MAX(v.voucher_amount),
                2
            ) AS excess_amount
        FROM match_reviews AS r
        JOIN voucher_records AS v
          ON v.id = r.voucher_id
        WHERE r.review_status = '已通过'
          AND r.allocation_amount IS NOT NULL
        GROUP BY r.voucher_id
        HAVING ROUND(
            SUM(r.allocation_amount),
            2
        ) > ROUND(
            MAX(v.voucher_amount),
            2
        )
        ORDER BY r.voucher_id
        """
    ).fetchall()


def fetch_business_reviews(
    connection: sqlite3.Connection,
    business_id: int,
) -> list[sqlite3.Row]:
    return connection.execute(
        """
        SELECT
            id AS review_id,
            voucher_id,
            allocation_amount
        FROM match_reviews
        WHERE business_record_id = ?
          AND review_status = '已通过'
        ORDER BY id
        """,
        (business_id,),
    ).fetchall()


def fetch_voucher_reviews(
    connection: sqlite3.Connection,
    voucher_id: int,
) -> list[sqlite3.Row]:
    return connection.execute(
        f"""
        SELECT
            r.id AS review_id,
            r.business_record_id,
            {DISPLAY_BUSINESS_NO_SQL}
                AS display_business_no,
            r.allocation_amount
        FROM match_reviews AS r
        LEFT JOIN business_records AS b
          ON b.id = r.business_record_id
        WHERE r.voucher_id = ?
          AND r.review_status = '已通过'
        ORDER BY r.id
        """,
        (voucher_id,),
    ).fetchall()


def print_missing(rows: list[sqlite3.Row]) -> set[int]:
    print()
    print("A. 已通过但缺少核销金额")
    print(f"记录数：{len(rows)}")
    print(
        "涉及业务数："
        f"{len({row['business_record_id'] for row in rows})}"
    )
    review_ids = set()

    for index, row in enumerate(rows, start=1):
        review_ids.add(row["review_id"])
        print()
        print(
            f"[A{index}] MatchReview ID："
            f"{row['review_id']}"
        )
        print(
            "  业务："
            f"{row['display_business_no']} "
            f"(ID {row['business_record_id']})"
        )
        print(
            f"  业务金额：{money(row['business_amount'])}"
        )
        print(
            f"  凭证 ID：{row['voucher_id']}；"
            f"凭证金额：{money(row['voucher_amount'])}"
        )
        print(f"  原因：{missing_reason(row)}")
        print(
            "  同业务已通过审核 ID："
            f"{row['same_business_review_ids'] or '无'}；"
            "已有核销合计："
            f"{money(row['business_allocated'])}"
        )
        print(
            "  同凭证已通过审核 ID："
            f"{row['same_voucher_review_ids'] or '无'}；"
            "已有分配合计："
            f"{money(row['voucher_allocated'])}"
        )

    return review_ids


def print_business_overpayments(
    connection: sqlite3.Connection,
    rows: list[sqlite3.Row],
) -> tuple[set[int], set[int]]:
    print()
    print("B. 历史业务超额核销")
    print(f"业务数：{len(rows)}")
    review_ids = set()
    business_ids = set()

    for index, row in enumerate(rows, start=1):
        business_id = row["business_record_id"]
        business_ids.add(business_id)
        print()
        print(
            f"[B{index}] {row['display_business_no']} "
            f"(ID {business_id})"
        )
        print(
            f"  业务金额：{money(row['business_amount'])}；"
            f"已核销：{money(row['allocated_amount'])}；"
            f"超额：{money(row['excess_amount'])}"
        )

        for detail in fetch_business_reviews(
            connection,
            business_id,
        ):
            review_ids.add(detail["review_id"])
            print(
                f"    - MR#{detail['review_id']} / "
                f"凭证#{detail['voucher_id']} / "
                f"核销 {money(detail['allocation_amount'])}"
            )

    return review_ids, business_ids


def print_voucher_overallocations(
    connection: sqlite3.Connection,
    rows: list[sqlite3.Row],
) -> tuple[set[int], set[int]]:
    print()
    print("C. 历史凭证超额分配")
    print(f"凭证数：{len(rows)}")
    review_ids = set()
    voucher_ids = set()

    for index, row in enumerate(rows, start=1):
        voucher_id = row["voucher_id"]
        voucher_ids.add(voucher_id)
        print()
        print(f"[C{index}] 凭证 ID：{voucher_id}")
        print(
            f"  凭证金额：{money(row['voucher_amount'])}；"
            f"已分配：{money(row['allocated_amount'])}；"
            f"超额：{money(row['excess_amount'])}"
        )

        for detail in fetch_voucher_reviews(
            connection,
            voucher_id,
        ):
            review_ids.add(detail["review_id"])
            print(
                f"    - MR#{detail['review_id']} / "
                f"{detail['display_business_no']} / "
                f"核销 {money(detail['allocation_amount'])}"
            )

    return review_ids, voucher_ids


def print_relationships(
    connection: sqlite3.Connection,
    missing_ids: set[int],
    business_review_ids: set[int],
    business_ids: set[int],
    voucher_review_ids: set[int],
    voucher_ids: set[int],
) -> None:
    all_review_ids = (
        missing_ids
        | business_review_ids
        | voucher_review_ids
    )
    print()
    print("D. 受影响审核记录关系")
    print(
        "去重后的 MatchReview 数："
        f"{len(all_review_ids)}"
    )

    for review_id in sorted(all_review_ids):
        row = connection.execute(
            """
            SELECT
                business_record_id,
                voucher_id
            FROM match_reviews
            WHERE id = ?
            """,
            (review_id,),
        ).fetchone()
        flags = []

        if review_id in missing_ids:
            flags.append("核销金额缺失")
        if row["business_record_id"] in business_ids:
            flags.append("所在业务超额")
        if row["voucher_id"] in voucher_ids:
            flags.append("所在凭证超额")

        print(
            f"  MR#{review_id}："
            f"{' + '.join(flags)}"
        )


def main() -> None:
    connection = open_read_only_database()

    try:
        validate_schema(connection)
        missing_rows = fetch_missing(connection)
        business_rows = fetch_business_overpayments(
            connection
        )
        voucher_rows = fetch_voucher_overallocations(
            connection
        )

        print("匹配审核核销金额只读审计")
        print(
            "数据库模式：只读（mode=ro + query_only）"
        )

        missing_ids = print_missing(missing_rows)
        (
            business_review_ids,
            business_ids,
        ) = print_business_overpayments(
            connection,
            business_rows,
        )
        (
            voucher_review_ids,
            voucher_ids,
        ) = print_voucher_overallocations(
            connection,
            voucher_rows,
        )
        print_relationships(
            connection,
            missing_ids,
            business_review_ids,
            business_ids,
            voucher_review_ids,
            voucher_ids,
        )

        quick_check = connection.execute(
            "PRAGMA quick_check"
        ).fetchone()[0]

        if quick_check != "ok":
            raise RuntimeError(
                f"数据库完整性检查失败：{quick_check}"
            )

        if connection.total_changes != 0:
            raise RuntimeError(
                "审计期间检测到数据库写入"
            )

        print()
        print("E. 审计校验")
        print(f"quick_check：{quick_check}")
        print(
            "数据库写入次数："
            f"{connection.total_changes}"
        )
        print("只读审计完成")
    finally:
        connection.close()


if __name__ == "__main__":
    main()