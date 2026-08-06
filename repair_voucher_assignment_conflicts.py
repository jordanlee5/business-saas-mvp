import argparse
import csv
import hashlib
import sqlite3
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from app.time_utils import utc8_now
from audit_match_review_allocations import DB_PATH
from audit_voucher_assignment_conflicts import (
    HISTORICAL_CASE_TYPE,
    HISTORICAL_KEEP_DECISION,
    HISTORICAL_REVOKE_DECISION,
    MANUAL_DISPOSITION_COLUMNS,
    MANUAL_DISPOSITION_EDITABLE_COLUMNS,
    ManualDispositionCsvValidationError,
    UNRESOLVED_CASE_TYPE,
    UNRESOLVED_CONFIRM_DECISION,
    UNRESOLVED_EXCLUDE_DECISION,
    main as run_read_only_audit,
    manual_disposition_text,
    validate_manual_disposition_groups,
)


LEGACY_PENDING_REVIEW_STATUS = "待审核"
PENDING_PRIMARY_REVIEW_STATUS = "待初审"
APPROVED_REVIEW_STATUS = "已通过"
REJECTED_REVIEW_STATUS = "已驳回"

PENDING_REVIEW_STATUSES = frozenset(
    {
        LEGACY_PENDING_REVIEW_STATUS,
        PENDING_PRIMARY_REVIEW_STATUS,
    }
)
BACKUP_DIRECTORY = Path("database_backups")


class VoucherConflictRepairError(RuntimeError):
    pass


@dataclass(frozen=True)
class RepairAction:
    case_no: str
    case_type: str
    voucher_id: int
    review_id: int
    business_id: int
    decision: str
    old_status: str
    new_status: str
    confirmed_by: str
    confirmed_at: str


@dataclass(frozen=True)
class RepairPlan:
    actions: tuple[RepairAction, ...]
    completed_group_count: int
    pending_group_count: int
    unchanged_decision_count: int


@dataclass(frozen=True)
class RepairResult:
    backup_path: Path
    updated_count: int
    quick_check: str
    before_business_overpayment_count: int
    after_business_overpayment_count: int
    before_voucher_overallocation_count: int
    after_voucher_overallocation_count: int


def parse_positive_int(value, field_name, group_label):
    text = str(value or "").strip()

    if not text.isdigit() or int(text) <= 0:
        raise VoucherConflictRepairError(
            f"{group_label} 的{field_name}必须是正整数："
            f"{text or '空值'}"
        )

    return int(text)


def read_manual_disposition_csv(input_path):
    input_path = Path(input_path)

    with input_path.open(
        "r",
        encoding="utf-8-sig",
        newline="",
    ) as input_file:
        reader = csv.DictReader(input_file)

        if tuple(reader.fieldnames or ()) != (
            MANUAL_DISPOSITION_COLUMNS
        ):
            raise VoucherConflictRepairError(
                "CSV 列名或顺序不一致"
            )

        rows = []

        for line_number, row in enumerate(
            reader,
            start=2,
        ):
            if (
                None in row
                or any(
                    row[column] is None
                    for column in MANUAL_DISPOSITION_COLUMNS
                )
            ):
                raise VoucherConflictRepairError(
                    f"CSV 第 {line_number} 行字段数量不一致"
                )

            rows.append(row)

    return tuple(rows)


def build_repair_plan(rows):
    rows = tuple(rows)
    group_validation = (
        validate_manual_disposition_groups(rows)
    )
    actions = []
    unchanged_decision_count = 0
    seen_review_ids = set()

    for row in rows:
        manual_values = tuple(
            manual_disposition_text(row, column)
            for column in MANUAL_DISPOSITION_EDITABLE_COLUMNS
        )

        if not any(manual_values):
            continue

        case_no = manual_disposition_text(
            row,
            "案例编号",
        )
        case_type = manual_disposition_text(
            row,
            "案例类型",
        )
        decision = manual_disposition_text(
            row,
            "人工决定",
        )
        group_label = f"{case_no}（{case_type}）"

        voucher_id = parse_positive_int(
            row.get("凭证ID"),
            "凭证ID",
            group_label,
        )
        review_id = parse_positive_int(
            row.get("审核记录ID"),
            "审核记录ID",
            group_label,
        )
        business_id = parse_positive_int(
            row.get("业务ID"),
            "业务ID",
            group_label,
        )

        if review_id in seen_review_ids:
            raise VoucherConflictRepairError(
                f"审核记录ID {review_id} 在处置清单中重复"
            )

        seen_review_ids.add(review_id)

        old_status = manual_disposition_text(
            row,
            "审核状态",
        )

        if case_type == UNRESOLVED_CASE_TYPE:
            if old_status not in PENDING_REVIEW_STATUSES:
                raise VoucherConflictRepairError(
                    f"{group_label} 的审核记录 {review_id} "
                    "不再处于待初审状态"
                )

            if decision == UNRESOLVED_CONFIRM_DECISION:
                unchanged_decision_count += 1
                continue

            if decision != UNRESOLVED_EXCLUDE_DECISION:
                raise VoucherConflictRepairError(
                    f"{group_label} 存在不支持的修复决定："
                    f"{decision}"
                )

        elif case_type == HISTORICAL_CASE_TYPE:
            if old_status != APPROVED_REVIEW_STATUS:
                raise VoucherConflictRepairError(
                    f"{group_label} 的审核记录 {review_id} "
                    "不再处于已通过状态"
                )

            if decision == HISTORICAL_KEEP_DECISION:
                unchanged_decision_count += 1
                continue

            if decision != HISTORICAL_REVOKE_DECISION:
                raise VoucherConflictRepairError(
                    f"{group_label} 存在不支持的修复决定："
                    f"{decision}"
                )

        else:
            raise VoucherConflictRepairError(
                f"{group_label} 的案例类型不支持修复"
            )

        actions.append(
            RepairAction(
                case_no=case_no,
                case_type=case_type,
                voucher_id=voucher_id,
                review_id=review_id,
                business_id=business_id,
                decision=decision,
                old_status=old_status,
                new_status=REJECTED_REVIEW_STATUS,
                confirmed_by=manual_disposition_text(
                    row,
                    "确认人",
                ),
                confirmed_at=manual_disposition_text(
                    row,
                    "确认时间",
                ),
            )
        )

    if not actions:
        raise VoucherConflictRepairError(
            "处置清单中没有可执行的状态修复"
        )

    return RepairPlan(
        actions=tuple(actions),
        completed_group_count=(
            group_validation.completed_group_count
        ),
        pending_group_count=(
            group_validation.pending_group_count
        ),
        unchanged_decision_count=(
            unchanged_decision_count
        ),
    )


def file_sha256(path):
    digest = hashlib.sha256()

    with Path(path).open("rb") as input_file:
        for chunk in iter(
            lambda: input_file.read(1024 * 1024),
            b"",
        ):
            digest.update(chunk)

    return digest.hexdigest()


def open_read_only_database(db_path):
    db_path = Path(db_path)

    if not db_path.is_file():
        raise FileNotFoundError(
            f"未找到数据库文件：{db_path.resolve()}"
        )

    uri = f"{db_path.resolve().as_uri()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")

    if connection.execute(
        "PRAGMA query_only"
    ).fetchone()[0] != 1:
        connection.close()
        raise VoucherConflictRepairError(
            "数据库只读保护未启用"
        )

    return connection


def validate_actions_against_database(
    connection,
    actions,
):
    for action in actions:
        row = connection.execute(
            """
            SELECT
                id,
                voucher_id,
                business_record_id,
                review_status
            FROM match_reviews
            WHERE id = ?
            """,
            (action.review_id,),
        ).fetchone()

        if row is None:
            raise VoucherConflictRepairError(
                f"审核记录 {action.review_id} 不存在"
            )

        actual = (
            row["voucher_id"],
            row["business_record_id"],
            row["review_status"],
        )
        expected = (
            action.voucher_id,
            action.business_id,
            action.old_status,
        )

        if actual != expected:
            raise VoucherConflictRepairError(
                f"审核记录 {action.review_id} 的数据库现状"
                "已变化，停止修复"
            )


def create_database_backup(
    db_path,
    backup_directory=BACKUP_DIRECTORY,
):
    db_path = Path(db_path)
    backup_directory = Path(backup_directory)
    backup_directory.mkdir(
        parents=True,
        exist_ok=True,
    )
    timestamp = utc8_now().strftime("%Y%m%d_%H%M%S")
    backup_path = backup_directory / (
        f"{db_path.stem}.before_voucher_conflict_repair_"
        f"{timestamp}{db_path.suffix}"
    )

    if backup_path.exists():
        raise FileExistsError(
            f"备份文件已存在，不会覆盖：{backup_path}"
        )

    source = open_read_only_database(db_path)
    destination = sqlite3.connect(backup_path)

    try:
        source.backup(destination)
        quick_check = destination.execute(
            "PRAGMA quick_check"
        ).fetchone()[0]

        if quick_check != "ok":
            raise VoucherConflictRepairError(
                f"数据库备份完整性检查失败：{quick_check}"
            )

    except Exception:
        destination.close()
        source.close()

        if backup_path.exists():
            backup_path.unlink()

        raise

    destination.close()
    source.close()

    if not backup_path.is_file() or backup_path.stat().st_size <= 0:
        raise VoucherConflictRepairError(
            "数据库备份文件未正确生成"
        )

    return backup_path


def count_business_overpayments(connection):
    return connection.execute(
        """
        SELECT COUNT(*)
        FROM (
            SELECT r.business_record_id
            FROM match_reviews AS r
            JOIN business_records AS b
              ON b.id = r.business_record_id
            WHERE r.review_status = '已通过'
              AND r.allocation_amount IS NOT NULL
            GROUP BY r.business_record_id
            HAVING ROUND(SUM(r.allocation_amount), 2)
                 > ROUND(MAX(b.points_amount), 2)
        )
        """
    ).fetchone()[0]


def count_voucher_overallocations(connection):
    return connection.execute(
        """
        SELECT COUNT(*)
        FROM (
            SELECT r.voucher_id
            FROM match_reviews AS r
            JOIN voucher_records AS v
              ON v.id = r.voucher_id
            WHERE r.review_status = '已通过'
              AND r.allocation_amount IS NOT NULL
            GROUP BY r.voucher_id
            HAVING ROUND(SUM(r.allocation_amount), 2)
                 > ROUND(MAX(v.voucher_amount), 2)
        )
        """
    ).fetchone()[0]


def validate_targeted_postconditions(
    connection,
    actions,
):
    for action in actions:
        status = connection.execute(
            "SELECT review_status FROM match_reviews WHERE id = ?",
            (action.review_id,),
        ).fetchone()

        if status is None or status[0] != action.new_status:
            raise VoucherConflictRepairError(
                f"审核记录 {action.review_id} 未正确更新"
            )

    unresolved_voucher_ids = sorted(
        {
            action.voucher_id
            for action in actions
            if action.case_type == UNRESOLVED_CASE_TYPE
        }
    )
    historical_voucher_ids = sorted(
        {
            action.voucher_id
            for action in actions
            if action.case_type == HISTORICAL_CASE_TYPE
        }
    )

    for voucher_id in unresolved_voucher_ids:
        active_business_count = connection.execute(
            """
            SELECT COUNT(DISTINCT business_record_id)
            FROM match_reviews
            WHERE voucher_id = ?
              AND review_status IN (
                  '待审核',
                  '待初审',
                  '待复核',
                  '已通过'
              )
            """,
            (voucher_id,),
        ).fetchone()[0]

        if active_business_count > 1:
            raise VoucherConflictRepairError(
                f"凭证 {voucher_id} 修复后仍存在多个有效归属"
            )

    for voucher_id in historical_voucher_ids:
        approved_business_count = connection.execute(
            """
            SELECT COUNT(DISTINCT business_record_id)
            FROM match_reviews
            WHERE voucher_id = ?
              AND review_status = '已通过'
            """,
            (voucher_id,),
        ).fetchone()[0]

        if approved_business_count > 1:
            raise VoucherConflictRepairError(
                f"凭证 {voucher_id} 修复后仍在多个业务通过"
            )


def apply_repair_plan(
    plan,
    db_path=DB_PATH,
    backup_directory=BACKUP_DIRECTORY,
):
    db_path = Path(db_path)
    backup_path = create_database_backup(
        db_path,
        backup_directory,
    )
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row

    try:
        connection.execute("BEGIN IMMEDIATE")
        validate_actions_against_database(
            connection,
            plan.actions,
        )
        before_business_overpayment_count = (
            count_business_overpayments(connection)
        )
        before_voucher_overallocation_count = (
            count_voucher_overallocations(connection)
        )
        updated_count = 0

        for action in plan.actions:
            cursor = connection.execute(
                """
                UPDATE match_reviews
                SET review_status = ?
                WHERE id = ?
                  AND voucher_id = ?
                  AND business_record_id = ?
                  AND review_status = ?
                """,
                (
                    action.new_status,
                    action.review_id,
                    action.voucher_id,
                    action.business_id,
                    action.old_status,
                ),
            )

            if cursor.rowcount != 1:
                raise VoucherConflictRepairError(
                    f"审核记录 {action.review_id} 未被唯一更新"
                )

            updated_count += 1

        validate_targeted_postconditions(
            connection,
            plan.actions,
        )
        after_business_overpayment_count = (
            count_business_overpayments(connection)
        )
        after_voucher_overallocation_count = (
            count_voucher_overallocations(connection)
        )

        if (
            after_business_overpayment_count
            > before_business_overpayment_count
        ):
            raise VoucherConflictRepairError(
                "修复后业务超额核销数量增加"
            )

        if (
            after_voucher_overallocation_count
            > before_voucher_overallocation_count
        ):
            raise VoucherConflictRepairError(
                "修复后凭证超额核销数量增加"
            )

        quick_check = connection.execute(
            "PRAGMA quick_check"
        ).fetchone()[0]

        if quick_check != "ok":
            raise VoucherConflictRepairError(
                f"修复后数据库完整性检查失败：{quick_check}"
            )

        connection.commit()

    except Exception:
        connection.rollback()
        raise

    finally:
        connection.close()

    return RepairResult(
        backup_path=backup_path,
        updated_count=updated_count,
        quick_check=quick_check,
        before_business_overpayment_count=(
            before_business_overpayment_count
        ),
        after_business_overpayment_count=(
            after_business_overpayment_count
        ),
        before_voucher_overallocation_count=(
            before_voucher_overallocation_count
        ),
        after_voucher_overallocation_count=(
            after_voucher_overallocation_count
        ),
    )


def print_repair_plan(plan):
    decision_counts = Counter(
        action.decision
        for action in plan.actions
    )

    print()
    print("I. 历史冲突修复预演")
    print(
        "已完成人工决定组数："
        f"{plan.completed_group_count}"
    )
    print(
        "保持待处理组数："
        f"{plan.pending_group_count}"
    )
    print(
        "确认保留且不改状态的审核记录数："
        f"{plan.unchanged_decision_count}"
    )
    print(
        "计划更新审核记录数："
        f"{len(plan.actions)}"
    )
    print(
        "  排除候选："
        f"{decision_counts[UNRESOLVED_EXCLUDE_DECISION]} 条"
    )
    print(
        "  撤销重复通过："
        f"{decision_counts[HISTORICAL_REVOKE_DECISION]} 条"
    )

    for action in plan.actions:
        print(
            f"  {action.case_no} / MR#{action.review_id} / "
            f"凭证#{action.voucher_id} / 业务#{action.business_id} / "
            f"{action.old_status} -> {action.new_status} / "
            f"{action.decision}"
        )


def parse_args():
    parser = argparse.ArgumentParser(
        description=(
            "按已校验人工处置清单预演或执行"
            "凭证归属冲突修复"
        )
    )
    parser.add_argument(
        "disposition_csv",
        type=Path,
        help="已经过 H 段校验的人工处置 CSV",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help=(
            "显式执行数据库修复；"
            "不提供时仅做只读预演"
        ),
    )
    parser.add_argument(
        "--backup-dir",
        type=Path,
        default=BACKUP_DIRECTORY,
        help=(
            "执行修复前的数据库备份目录；"
            "默认 database_backups"
        ),
    )

    return parser.parse_args()


def main(
    disposition_csv,
    apply=False,
    db_path=DB_PATH,
    backup_directory=BACKUP_DIRECTORY,
):
    disposition_csv = Path(disposition_csv)
    csv_hash_before = file_sha256(disposition_csv)

    print("第一步：重新运行只读审计与 H 段证据校验")
    run_read_only_audit(
        validate_csv_path=disposition_csv
    )

    rows = read_manual_disposition_csv(
        disposition_csv
    )
    plan = build_repair_plan(rows)
    print_repair_plan(plan)

    read_only_connection = open_read_only_database(
        db_path
    )

    try:
        validate_actions_against_database(
            read_only_connection,
            plan.actions,
        )

        if read_only_connection.total_changes != 0:
            raise VoucherConflictRepairError(
                "预演期间检测到数据库写入"
            )

    finally:
        read_only_connection.close()

    if file_sha256(disposition_csv) != csv_hash_before:
        raise VoucherConflictRepairError(
            "预演期间人工处置 CSV 发生变化"
        )

    if not apply:

        print()
        print("J. 只读预演结论")
        print("数据库备份：未创建（预演模式）")
        print("数据库写入次数：0")
        print("清单文件写入次数：0")
        print("预演完成：未修改 CSV 或数据库")
        return plan

    print()
    print("J. 显式执行修复")
    result = apply_repair_plan(
        plan,
        db_path=db_path,
        backup_directory=backup_directory,
    )

    print(
        "数据库备份："
        f"{result.backup_path.resolve()}"
    )
    print(
        "实际更新审核记录数："
        f"{result.updated_count}"
    )
    print(
        "业务超额核销组数："
        f"{result.before_business_overpayment_count} -> "
        f"{result.after_business_overpayment_count}"
    )
    print(
        "凭证超额核销组数："
        f"{result.before_voucher_overallocation_count} -> "
        f"{result.after_voucher_overallocation_count}"
    )
    print(f"PRAGMA quick_check：{result.quick_check}")
    print("清单文件写入次数：0")
    print("修复完成：数据库事务已提交")

    return result


if __name__ == "__main__":
    args = parse_args()

    try:
        main(
            disposition_csv=args.disposition_csv,
            apply=args.apply,
            backup_directory=args.backup_dir,
        )
    except (
        ManualDispositionCsvValidationError,
        OSError,
        sqlite3.Error,
        VoucherConflictRepairError,
    ) as exc:
        raise SystemExit(f"修复停止：{exc}") from exc
