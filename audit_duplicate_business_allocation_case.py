import argparse
import hashlib
import sqlite3
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path


DEFAULT_DB_PATH = Path("saas_mvp.db")
TARGET_BUSINESS_ID = 63
TARGET_VOUCHER_IDS = (57, 64)
TARGET_REVIEW_TO_VOUCHER = {
    130: 57,
    151: 64,
}
CENT = Decimal("0.01")


REQUIRED_COLUMNS = {
    "business_records": {
        "id",
        "user_id",
        "batch_id",
        "business_no",
        "public_business_no",
        "name",
        "phone",
        "plate_number",
        "points_amount",
        "bank_card",
        "created_at",
    },
    "upload_batches": {
        "id",
        "filename",
        "acceptance_status",
        "created_at",
    },
    "voucher_records": {
        "id",
        "uploader_id",
        "batch_id",
        "filename",
        "file_path",
        "file_hash",
        "voucher_amount",
        "ocr_text",
        "created_at",
    },
    "voucher_upload_batches": {
        "id",
        "uploader_id",
        "partner_id",
        "total_files",
        "success_files",
        "duplicate_files",
        "failed_files",
        "total_created_reviews",
        "created_at",
    },
    "match_reviews": {
        "id",
        "voucher_id",
        "business_record_id",
        "match_status",
        "name_match",
        "bank_match",
        "amount_match",
        "score",
        "review_status",
        "allocation_amount",
        "primary_reviewer_id",
        "primary_review_result",
        "primary_review_comment",
        "primary_reviewed_at",
        "secondary_reviewer_id",
        "secondary_review_result",
        "secondary_review_comment",
        "secondary_reviewed_at",
        "created_at",
    },
    "admin_action_logs": {
        "id",
        "admin_id",
        "action_type",
        "target_type",
        "target_id",
        "description",
        "created_at",
    },
    "users": {
        "id",
        "username",
        "role",
        "admin_level",
    },
}


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


def normalized_money(value) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None

    try:
        amount = Decimal(str(value)).quantize(
            CENT,
            rounding=ROUND_HALF_UP,
        )
    except (InvalidOperation, ValueError):
        return None

    if not amount.is_finite():
        return None

    return amount


def shown(value) -> str:
    if value is None:
        return "-"

    text = str(value)
    return text if text else "-"


def normalize_ocr_text(value) -> str:
    return " ".join(str(value or "").split())


def text_sha256(value) -> str:
    return hashlib.sha256(
        str(value or "").encode("utf-8")
    ).hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()

    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)

    return digest.hexdigest()


def resolve_voucher_file_path(
    database_path: Path,
    raw_path,
) -> Path | None:
    if raw_path is None or not str(raw_path).strip():
        return None

    path = Path(str(raw_path))

    if path.is_absolute():
        return path

    candidates = (
        database_path.resolve().parent / path,
        Path.cwd() / path,
    )

    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()

    return candidates[0].resolve()


def build_file_evidence(
    database_path: Path,
    voucher,
) -> dict:
    resolved_path = resolve_voucher_file_path(
        database_path,
        voucher["file_path"],
    )
    exists = bool(
        resolved_path is not None
        and resolved_path.is_file()
    )
    actual_hash = (
        file_sha256(resolved_path)
        if exists and resolved_path is not None
        else None
    )
    database_hash = shown(voucher["file_hash"])

    return {
        "voucher_id": voucher["voucher_id"],
        "resolved_path": resolved_path,
        "exists": exists,
        "size": (
            resolved_path.stat().st_size
            if exists and resolved_path is not None
            else None
        ),
        "actual_hash": actual_hash,
        "database_hash": database_hash,
        "hash_matches_database": (
            actual_hash is not None
            and database_hash != "-"
            and actual_hash.lower() == database_hash.lower()
        ),
    }


def compare_voucher_evidence(
    vouchers,
    file_evidence,
) -> dict:
    ordered_vouchers = sorted(
        vouchers,
        key=lambda row: row["voucher_id"],
    )
    ordered_files = sorted(
        file_evidence,
        key=lambda item: item["voucher_id"],
    )

    if len(ordered_vouchers) != 2 or len(ordered_files) != 2:
        raise RuntimeError("凭证比较必须恰好包含两条记录")

    left, right = ordered_vouchers
    left_file, right_file = ordered_files
    left_ocr = shown(left["ocr_text"])
    right_ocr = shown(right["ocr_text"])
    left_normalized = normalize_ocr_text(left["ocr_text"])
    right_normalized = normalize_ocr_text(right["ocr_text"])
    left_db_hash = shown(left["file_hash"])
    right_db_hash = shown(right["file_hash"])

    actual_hashes_available = (
        left_file["actual_hash"] is not None
        and right_file["actual_hash"] is not None
    )

    return {
        "same_filename": shown(left["filename"]) == shown(right["filename"]),
        "same_amount": normalized_money(left["voucher_amount"])
        == normalized_money(right["voucher_amount"]),
        "same_database_hash": (
            left_db_hash != "-"
            and right_db_hash != "-"
            and left_db_hash.lower() == right_db_hash.lower()
        ),
        "same_actual_hash": (
            actual_hashes_available
            and left_file["actual_hash"].lower()
            == right_file["actual_hash"].lower()
        ),
        "actual_hashes_available": actual_hashes_available,
        "same_ocr_exact": left_ocr == right_ocr,
        "same_ocr_normalized": left_normalized == right_normalized,
        "left_ocr_hash": text_sha256(left_normalized),
        "right_ocr_hash": text_sha256(right_normalized),
    }


def open_read_only_database(
    database_path: Path,
) -> sqlite3.Connection:
    if not database_path.exists():
        raise FileNotFoundError(
            f"未找到数据库文件：{database_path.resolve()}"
        )

    uri = f"{database_path.resolve().as_uri()}?mode=ro"
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
                f"{table_name} 缺少字段：{sorted(missing)}"
            )


def fetch_business(
    connection: sqlite3.Connection,
):
    return connection.execute(
        """
        SELECT
            b.id AS business_id,
            COALESCE(
                NULLIF(TRIM(b.public_business_no), ''),
                NULLIF(TRIM(b.business_no), ''),
                'BR-' || b.id
            ) AS display_business_no,
            b.business_no,
            b.public_business_no,
            b.name,
            b.phone,
            b.plate_number,
            b.points_amount AS business_amount,
            b.bank_card,
            b.user_id,
            partner.username AS partner_username,
            b.batch_id,
            batch.filename AS business_batch_filename,
            batch.acceptance_status,
            batch.created_at AS business_batch_created_at,
            b.created_at AS business_created_at
        FROM business_records AS b
        LEFT JOIN users AS partner
          ON partner.id = b.user_id
        LEFT JOIN upload_batches AS batch
          ON batch.id = b.batch_id
        WHERE b.id = ?
        """,
        (TARGET_BUSINESS_ID,),
    ).fetchall()


def fetch_vouchers(
    connection: sqlite3.Connection,
):
    placeholders = ", ".join(
        "?" for _ in TARGET_VOUCHER_IDS
    )
    return connection.execute(
        f"""
        SELECT
            v.id AS voucher_id,
            v.uploader_id,
            uploader.username AS uploader_username,
            v.batch_id AS voucher_batch_id,
            batch.partner_id AS voucher_batch_partner_id,
            partner.username AS voucher_batch_partner_username,
            batch.total_files,
            batch.success_files,
            batch.duplicate_files,
            batch.failed_files,
            batch.total_created_reviews,
            batch.created_at AS voucher_batch_created_at,
            v.filename,
            v.file_path,
            v.file_hash,
            v.voucher_amount,
            v.ocr_text,
            v.created_at AS voucher_created_at
        FROM voucher_records AS v
        LEFT JOIN users AS uploader
          ON uploader.id = v.uploader_id
        LEFT JOIN voucher_upload_batches AS batch
          ON batch.id = v.batch_id
        LEFT JOIN users AS partner
          ON partner.id = batch.partner_id
        WHERE v.id IN ({placeholders})
        ORDER BY v.id
        """,
        TARGET_VOUCHER_IDS,
    ).fetchall()


def review_select_sql(where_clause: str) -> str:
    return f"""
        SELECT
            r.id AS review_id,
            r.voucher_id,
            v.filename AS voucher_filename,
            v.voucher_amount,
            r.business_record_id,
            COALESCE(
                NULLIF(TRIM(b.public_business_no), ''),
                NULLIF(TRIM(b.business_no), ''),
                'BR-' || b.id
            ) AS display_business_no,
            b.name AS business_name,
            b.points_amount AS business_amount,
            r.match_status,
            r.name_match,
            r.bank_match,
            r.amount_match,
            r.score,
            r.review_status,
            r.allocation_amount,
            r.primary_reviewer_id,
            primary_user.username AS primary_reviewer_username,
            r.primary_review_result,
            r.primary_review_comment,
            r.primary_reviewed_at,
            r.secondary_reviewer_id,
            secondary_user.username AS secondary_reviewer_username,
            r.secondary_review_result,
            r.secondary_review_comment,
            r.secondary_reviewed_at,
            r.created_at AS review_created_at
        FROM match_reviews AS r
        LEFT JOIN voucher_records AS v
          ON v.id = r.voucher_id
        LEFT JOIN business_records AS b
          ON b.id = r.business_record_id
        LEFT JOIN users AS primary_user
          ON primary_user.id = r.primary_reviewer_id
        LEFT JOIN users AS secondary_user
          ON secondary_user.id = r.secondary_reviewer_id
        {where_clause}
    """


def fetch_target_reviews(
    connection: sqlite3.Connection,
):
    review_ids = tuple(TARGET_REVIEW_TO_VOUCHER)
    placeholders = ", ".join("?" for _ in review_ids)
    return connection.execute(
        review_select_sql(
            f"WHERE r.id IN ({placeholders}) ORDER BY r.id"
        ),
        review_ids,
    ).fetchall()


def fetch_related_reviews(
    connection: sqlite3.Connection,
):
    voucher_placeholders = ", ".join(
        "?" for _ in TARGET_VOUCHER_IDS
    )
    return connection.execute(
        review_select_sql(
            "WHERE r.business_record_id = ? "
            f"OR r.voucher_id IN ({voucher_placeholders}) "
            "ORDER BY r.voucher_id, r.business_record_id, r.id"
        ),
        (TARGET_BUSINESS_ID, *TARGET_VOUCHER_IDS),
    ).fetchall()


def fetch_action_logs(
    connection: sqlite3.Connection,
):
    review_ids = tuple(TARGET_REVIEW_TO_VOUCHER)
    placeholders = ", ".join("?" for _ in review_ids)
    return connection.execute(
        f"""
        SELECT
            log.id AS log_id,
            log.admin_id,
            admin.username AS admin_username,
            log.action_type,
            log.target_type,
            log.target_id,
            log.description,
            log.created_at
        FROM admin_action_logs AS log
        LEFT JOIN users AS admin
          ON admin.id = log.admin_id
        WHERE log.target_type = 'match_review'
          AND log.target_id IN ({placeholders})
        ORDER BY log.id
        """,
        review_ids,
    ).fetchall()


def validate_target_scope(
    businesses,
    vouchers,
    reviews,
) -> None:
    if len(businesses) != 1:
        raise RuntimeError(
            "目标业务#63不存在或不唯一，停止取证"
        )

    business = businesses[0]

    if business["business_id"] != TARGET_BUSINESS_ID:
        raise RuntimeError("目标业务ID发生偏移，停止取证")

    if normalized_money(business["business_amount"]) != Decimal(
        "31402.06"
    ):
        raise RuntimeError(
            "业务#63金额已不再是31402.06，停止取证"
        )

    voucher_by_id = {
        row["voucher_id"]: row for row in vouchers
    }

    if set(voucher_by_id) != set(TARGET_VOUCHER_IDS):
        raise RuntimeError(
            "目标凭证#57/#64缺失或重复，停止取证"
        )

    for voucher_id, voucher in voucher_by_id.items():
        if normalized_money(voucher["voucher_amount"]) != Decimal(
            "31402.06"
        ):
            raise RuntimeError(
                f"凭证#{voucher_id}金额已变化，停止取证"
            )

    review_by_id = {
        row["review_id"]: row for row in reviews
    }

    if set(review_by_id) != set(TARGET_REVIEW_TO_VOUCHER):
        raise RuntimeError(
            "目标MR#130/#151缺失或重复，停止取证"
        )

    for review_id, expected_voucher_id in (
        TARGET_REVIEW_TO_VOUCHER.items()
    ):
        review = review_by_id[review_id]

        if review["voucher_id"] != expected_voucher_id:
            raise RuntimeError(
                f"MR#{review_id}的凭证关联已变化，停止取证"
            )

        if review["business_record_id"] != TARGET_BUSINESS_ID:
            raise RuntimeError(
                f"MR#{review_id}的业务关联已变化，停止取证"
            )

        if review["review_status"] != "已通过":
            raise RuntimeError(
                f"MR#{review_id}不再是已通过状态，停止取证"
            )

        if normalized_money(review["allocation_amount"]) != Decimal(
            "31402.06"
        ):
            raise RuntimeError(
                f"MR#{review_id}核销金额已变化，停止取证"
            )


def validate_business_overpayment_scope(
    related_reviews,
) -> None:
    approved_rows = [
        row
        for row in related_reviews
        if (
            row["business_record_id"] == TARGET_BUSINESS_ID
            and row["review_status"] == "已通过"
        )
    ]
    approved_ids = {
        row["review_id"] for row in approved_rows
    }

    if approved_ids != set(TARGET_REVIEW_TO_VOUCHER):
        raise RuntimeError(
            "业务#63当前已通过审核集合不再是MR#130/#151，"
            "停止取证"
        )

    allocated_total = sum(
        (
            normalized_money(row["allocation_amount"])
            or Decimal("0.00")
        )
        for row in approved_rows
    )

    if allocated_total != Decimal("62804.12"):
        raise RuntimeError(
            "业务#63当前已核销合计不再是62804.12，"
            "停止取证"
        )


def print_business(business) -> None:
    print()
    print("A. 目标业务")
    print(
        f"业务：{business['display_business_no']} "
        f"(ID {business['business_id']})"
    )
    print(f"业务金额：{money(business['business_amount'])}")
    print(
        f"姓名：{shown(business['name'])}；"
        f"手机号：{shown(business['phone'])}；"
        f"车牌号：{shown(business['plate_number'])}"
    )
    print(f"银行卡：{shown(business['bank_card'])}")
    print(
        f"上传方：{shown(business['partner_username'])} "
        f"(ID {shown(business['user_id'])})"
    )
    print(
        f"业务批次：{shown(business['business_batch_filename'])} "
        f"(ID {shown(business['batch_id'])})；"
        f"承接状态：{shown(business['acceptance_status'])}"
    )
    print(
        f"批次时间：{shown(business['business_batch_created_at'])}；"
        f"业务创建时间：{shown(business['business_created_at'])}"
    )


def print_vouchers(vouchers, file_evidence) -> None:
    evidence_by_id = {
        item["voucher_id"]: item
        for item in file_evidence
    }
    print()
    print("B. 两张凭证及文件证据")

    for voucher in vouchers:
        evidence = evidence_by_id[voucher["voucher_id"]]
        print()
        print(f"[凭证#{voucher['voucher_id']}]")
        print(
            f"文件名：{shown(voucher['filename'])}；"
            f"金额：{money(voucher['voucher_amount'])}"
        )
        print(f"数据库路径：{shown(voucher['file_path'])}")
        print(f"解析路径：{shown(evidence['resolved_path'])}")
        print(
            f"文件存在：{evidence['exists']}；"
            f"字节数：{shown(evidence['size'])}"
        )
        print(f"数据库SHA-256：{evidence['database_hash']}")
        print(
            "磁盘实际SHA-256："
            f"{shown(evidence['actual_hash'])}"
        )
        print(
            "磁盘哈希与数据库一致："
            f"{evidence['hash_matches_database']}"
        )
        print(
            f"上传人：{shown(voucher['uploader_username'])} "
            f"(ID {shown(voucher['uploader_id'])})"
        )
        print(
            f"凭证批次ID：{shown(voucher['voucher_batch_id'])}；"
            "批次指定上传方："
            f"{shown(voucher['voucher_batch_partner_username'])} "
            f"(ID {shown(voucher['voucher_batch_partner_id'])})"
        )
        print(
            "批次统计："
            f"总文件{shown(voucher['total_files'])}，"
            f"成功{shown(voucher['success_files'])}，"
            f"重复{shown(voucher['duplicate_files'])}，"
            f"失败{shown(voucher['failed_files'])}，"
            "生成审核记录"
            f"{shown(voucher['total_created_reviews'])}"
        )
        print(
            f"批次时间：{shown(voucher['voucher_batch_created_at'])}；"
            f"凭证创建时间：{shown(voucher['voucher_created_at'])}"
        )


def print_comparison(comparison) -> None:
    print()
    print("C. 两张凭证自动对比")
    print(f"文件名相同：{comparison['same_filename']}")
    print(f"凭证金额相同：{comparison['same_amount']}")
    print(
        "数据库文件哈希相同："
        f"{comparison['same_database_hash']}"
    )
    print(
        "两份磁盘文件均可读取："
        f"{comparison['actual_hashes_available']}"
    )
    print(
        "磁盘实际文件哈希相同："
        f"{comparison['same_actual_hash']}"
    )
    print(
        "OCR原文完全相同："
        f"{comparison['same_ocr_exact']}"
    )
    print(
        "OCR忽略空白后相同："
        f"{comparison['same_ocr_normalized']}"
    )
    print(
        "凭证#57规范化OCR哈希："
        f"{comparison['left_ocr_hash']}"
    )
    print(
        "凭证#64规范化OCR哈希："
        f"{comparison['right_ocr_hash']}"
    )


def print_review(review, indent="") -> None:
    print(
        f"{indent}MR#{review['review_id']} / "
        f"凭证#{review['voucher_id']} / "
        f"业务#{review['business_record_id']} "
        f"{review['display_business_no']}"
    )
    print(
        f"{indent}  状态：{shown(review['review_status'])}；"
        f"核销：{money(review['allocation_amount'])}；"
        f"分数：{shown(review['score'])}；"
        f"匹配状态：{shown(review['match_status'])}"
    )
    print(
        f"{indent}  姓名：{shown(review['name_match'])}；"
        f"银行卡：{shown(review['bank_match'])}；"
        f"金额：{shown(review['amount_match'])}"
    )
    print(
        f"{indent}  初审：{shown(review['primary_review_result'])} / "
        f"{shown(review['primary_reviewer_username'])} "
        f"(ID {shown(review['primary_reviewer_id'])}) / "
        f"{shown(review['primary_reviewed_at'])}"
    )
    print(
        f"{indent}  初审备注："
        f"{shown(review['primary_review_comment'])}"
    )
    print(
        f"{indent}  复核：{shown(review['secondary_review_result'])} / "
        f"{shown(review['secondary_reviewer_username'])} "
        f"(ID {shown(review['secondary_reviewer_id'])}) / "
        f"{shown(review['secondary_reviewed_at'])}"
    )
    print(
        f"{indent}  复核备注："
        f"{shown(review['secondary_review_comment'])}"
    )
    print(
        f"{indent}  审核记录创建："
        f"{shown(review['review_created_at'])}"
    )


def print_target_reviews(reviews) -> None:
    print()
    print("D. 两条重复核销审核链")
    allocated_total = sum(
        (
            normalized_money(review["allocation_amount"])
            or Decimal("0.00")
        )
        for review in reviews
    )
    print(
        "两条已通过核销合计："
        f"{money(allocated_total)}；"
        "相对业务金额超额：31402.06"
    )

    for review in reviews:
        print()
        print_review(review)


def print_related_reviews(reviews) -> None:
    print()
    print("E. 同业务或同凭证的全部关联审核记录")
    print(f"去重记录数：{len(reviews)}")

    for review in reviews:
        print()
        print_review(review, indent="  ")


def print_action_logs(logs) -> None:
    print()
    print("F. 可精确关联到MR#130/#151的操作日志")
    print(f"日志数：{len(logs)}")

    if not logs:
        print(
            "无精确target_id日志；历史批量日志若未保存逐条ID，"
            "不能据此反推某一条审核记录。"
        )
        return

    for log in logs:
        print()
        print(
            f"日志#{log['log_id']} / "
            f"{shown(log['action_type'])} / "
            f"目标MR#{shown(log['target_id'])}"
        )
        print(
            f"  操作者：{shown(log['admin_username'])} "
            f"(ID {shown(log['admin_id'])})；"
            f"时间：{shown(log['created_at'])}"
        )
        print(f"  描述：{shown(log['description'])}")


def print_ocr(vouchers) -> None:
    print()
    print("G. 完整OCR原文")
    print(
        "说明：交易时间、流水号、付款方、收款方目前没有独立结构化字段，"
        "需要从以下OCR原文及原始凭证图片人工核对。"
    )

    for voucher in vouchers:
        print()
        print(f"----- 凭证#{voucher['voucher_id']} OCR开始 -----")
        print(shown(voucher["ocr_text"]))
        print(f"----- 凭证#{voucher['voucher_id']} OCR结束 -----")


def print_boundary_conclusion(comparison) -> None:
    print()
    print("H. 机器可确认的边界结论")
    print("业务#63被两张不同凭证各核销31402.06，当前重复核销成立。")

    if comparison["same_actual_hash"]:
        print("两份磁盘文件字节完全一致，存在强重复文件证据。")
    elif comparison["same_database_hash"]:
        print("数据库文件哈希相同，存在强重复文件证据。")
    elif comparison["same_ocr_normalized"]:
        print(
            "两份OCR忽略空白后相同，存在疑似同一交易证据；"
            "仍须查看原始图片。"
        )
    else:
        print(
            "两份凭证未被自动判定为同一文件或相同OCR；"
            "必须逐项核对交易要素。"
        )

    print(
        "本工具不自动决定保留MR#130还是MR#151，也不修改任何状态。"
    )


def parse_args():
    parser = argparse.ArgumentParser(
        description=(
            "只读取证业务#63的两笔重复全额核销，"
            "不修改数据库或凭证文件。"
        )
    )
    parser.add_argument(
        "--database",
        type=Path,
        default=DEFAULT_DB_PATH,
        help="SQLite数据库路径，默认saas_mvp.db",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    database_path = args.database
    connection = open_read_only_database(database_path)

    try:
        validate_schema(connection)
        businesses = fetch_business(connection)
        vouchers = fetch_vouchers(connection)
        reviews = fetch_target_reviews(connection)
        validate_target_scope(
            businesses,
            vouchers,
            reviews,
        )
        related_reviews = fetch_related_reviews(connection)
        validate_business_overpayment_scope(
            related_reviews,
        )
        logs = fetch_action_logs(connection)
        file_evidence = [
            build_file_evidence(database_path, voucher)
            for voucher in vouchers
        ]
        comparison = compare_voucher_evidence(
            vouchers,
            file_evidence,
        )

        print("业务#63重复核销专项只读取证")
        print("数据库模式：只读（mode=ro + query_only）")
        print("固定范围：业务#63；凭证#57/#64；MR#130/#151")

        print_business(businesses[0])
        print_vouchers(vouchers, file_evidence)
        print_comparison(comparison)
        print_target_reviews(reviews)
        print_related_reviews(related_reviews)
        print_action_logs(logs)
        print_ocr(vouchers)
        print_boundary_conclusion(comparison)

        quick_check = connection.execute(
            "PRAGMA quick_check"
        ).fetchone()[0]

        if quick_check != "ok":
            raise RuntimeError(
                f"数据库完整性检查失败：{quick_check}"
            )

        if connection.total_changes != 0:
            raise RuntimeError("取证期间检测到数据库写入")

        print()
        print("I. 取证校验")
        print(f"PRAGMA quick_check：{quick_check}")
        print(
            "数据库写入次数："
            f"{connection.total_changes}"
        )
        print("凭证文件写入次数：0")
        print("专项取证完成：未修改数据库或凭证文件")
    finally:
        connection.close()


if __name__ == "__main__":
    main()
