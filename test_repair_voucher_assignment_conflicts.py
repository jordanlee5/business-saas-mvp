import csv
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from audit_voucher_assignment_conflicts import (
    HISTORICAL_CASE_TYPE,
    HISTORICAL_KEEP_DECISION,
    HISTORICAL_REVOKE_DECISION,
    MANUAL_DISPOSITION_COLUMNS,
    UNRESOLVED_CASE_TYPE,
    UNRESOLVED_CONFIRM_DECISION,
    UNRESOLVED_EXCLUDE_DECISION,
)
from repair_voucher_assignment_conflicts import (
    APPROVED_REVIEW_STATUS,
    LEGACY_PENDING_REVIEW_STATUS,
    REJECTED_REVIEW_STATUS,
    RepairAction,
    RepairPlan,
    VoucherConflictRepairError,
    apply_repair_plan,
    build_repair_plan,
    create_database_backup,
    main,
    read_manual_disposition_csv,
    validate_actions_against_database,
)


def make_disposition_row(
    *,
    case_no="D1",
    case_type=UNRESOLVED_CASE_TYPE,
    voucher_id="10",
    review_id="1",
    business_id="1",
    review_status=LEGACY_PENDING_REVIEW_STATUS,
    amount_safety="金额安全候选",
    decision="",
    confirmed_by="",
    confirmed_at="",
):
    row = {
        column: ""
        for column in MANUAL_DISPOSITION_COLUMNS
    }
    row.update(
        {
            "案例编号": case_no,
            "案例类型": case_type,
            "凭证ID": voucher_id,
            "凭证文件名": f"voucher-{voucher_id}.png",
            "凭证金额": "60.00",
            "审核记录ID": review_id,
            "审核状态": review_status,
            "本次核销金额": "60.00",
            "审核记录创建时间": "2026-08-01 10:00:00",
            "业务ID": business_id,
            "公开业务单号": f"PUBLIC-{business_id}",
            "业务金额": "100.00",
            "已核销金额": "0.00",
            "剩余金额": "100.00",
            "业务核销状态": "未付款",
            "金额安全性": amount_safety,
            "匹配分数": "3",
            "匹配状态": "匹配成功",
            "姓名匹配证据": "命中",
            "银行卡匹配证据": "命中",
            "金额匹配证据": "命中",
            "处置建议": "测试处置建议",
            "人工决定": decision,
            "确认人": confirmed_by,
            "确认时间": confirmed_at,
        }
    )

    return row


def write_disposition_csv(path, rows):
    with Path(path).open(
        "w",
        encoding="utf-8-sig",
        newline="",
    ) as output_file:
        writer = csv.DictWriter(
            output_file,
            fieldnames=MANUAL_DISPOSITION_COLUMNS,
        )
        writer.writeheader()
        writer.writerows(rows)


def create_test_database(path):
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE business_records (
            id INTEGER PRIMARY KEY,
            points_amount NUMERIC
        );

        CREATE TABLE voucher_records (
            id INTEGER PRIMARY KEY,
            voucher_amount NUMERIC
        );

        CREATE TABLE match_reviews (
            id INTEGER PRIMARY KEY,
            voucher_id INTEGER,
            business_record_id INTEGER,
            review_status TEXT,
            allocation_amount NUMERIC,
            primary_review_result TEXT,
            secondary_review_result TEXT
        );
        """
    )
    connection.executemany(
        "INSERT INTO business_records(id, points_amount) VALUES (?, ?)",
        (
            (1, 100),
            (2, 100),
            (3, 50),
            (4, 50),
        ),
    )
    connection.executemany(
        "INSERT INTO voucher_records(id, voucher_amount) VALUES (?, ?)",
        (
            (10, 60),
            (20, 50),
        ),
    )
    connection.executemany(
        """
        INSERT INTO match_reviews(
            id,
            voucher_id,
            business_record_id,
            review_status,
            allocation_amount,
            primary_review_result,
            secondary_review_result
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            (
                1,
                10,
                1,
                LEGACY_PENDING_REVIEW_STATUS,
                None,
                None,
                None,
            ),
            (
                2,
                10,
                2,
                LEGACY_PENDING_REVIEW_STATUS,
                None,
                None,
                None,
            ),
            (
                3,
                20,
                3,
                APPROVED_REVIEW_STATUS,
                50,
                "通过",
                "通过",
            ),
            (
                4,
                20,
                4,
                APPROVED_REVIEW_STATUS,
                50,
                "通过",
                "通过",
            ),
        ),
    )
    connection.commit()
    connection.close()


def make_repair_plan():
    return RepairPlan(
        actions=(
            RepairAction(
                case_no="D1",
                case_type=UNRESOLVED_CASE_TYPE,
                voucher_id=10,
                review_id=2,
                business_id=2,
                decision=UNRESOLVED_EXCLUDE_DECISION,
                old_status=LEGACY_PENDING_REVIEW_STATUS,
                new_status=REJECTED_REVIEW_STATUS,
                confirmed_by="Administrator",
                confirmed_at="2026-08-06 15:57:06",
            ),
            RepairAction(
                case_no="E1",
                case_type=HISTORICAL_CASE_TYPE,
                voucher_id=20,
                review_id=4,
                business_id=4,
                decision=HISTORICAL_REVOKE_DECISION,
                old_status=APPROVED_REVIEW_STATUS,
                new_status=REJECTED_REVIEW_STATUS,
                confirmed_by="Administrator",
                confirmed_at="2026-08-06 15:57:06",
            ),
        ),
        completed_group_count=2,
        pending_group_count=0,
        unchanged_decision_count=2,
    )


class VoucherConflictRepairTests(unittest.TestCase):
    def test_build_plan_skips_pending_and_kept_rows(self):
        confirmed_at = "2026-08-06 15:57:06"
        rows = (
            make_disposition_row(
                decision=UNRESOLVED_CONFIRM_DECISION,
                confirmed_by="Administrator",
                confirmed_at=confirmed_at,
            ),
            make_disposition_row(
                review_id="2",
                business_id="2",
                amount_safety="非金额安全候选",
                decision=UNRESOLVED_EXCLUDE_DECISION,
                confirmed_by="Administrator",
                confirmed_at=confirmed_at,
            ),
            make_disposition_row(
                case_no="E1",
                case_type=HISTORICAL_CASE_TYPE,
                voucher_id="20",
                review_id="3",
                business_id="3",
                review_status=APPROVED_REVIEW_STATUS,
                decision=HISTORICAL_KEEP_DECISION,
                confirmed_by="Administrator",
                confirmed_at=confirmed_at,
            ),
            make_disposition_row(
                case_no="E1",
                case_type=HISTORICAL_CASE_TYPE,
                voucher_id="20",
                review_id="4",
                business_id="4",
                review_status=APPROVED_REVIEW_STATUS,
                decision=HISTORICAL_REVOKE_DECISION,
                confirmed_by="Administrator",
                confirmed_at=confirmed_at,
            ),
            make_disposition_row(
                case_no="D2",
                voucher_id="30",
                review_id="5",
                business_id="5",
            ),
            make_disposition_row(
                case_no="D2",
                voucher_id="30",
                review_id="6",
                business_id="6",
                amount_safety="非金额安全候选",
            ),
        )

        plan = build_repair_plan(rows)

        self.assertEqual(len(plan.actions), 2)
        self.assertEqual(
            [action.review_id for action in plan.actions],
            [2, 4],
        )
        self.assertEqual(plan.completed_group_count, 2)
        self.assertEqual(plan.pending_group_count, 1)
        self.assertEqual(plan.unchanged_decision_count, 2)

    def test_build_plan_rejects_duplicate_review_id(self):
        confirmed_at = "2026-08-06 15:57:06"
        rows = (
            make_disposition_row(
                decision=UNRESOLVED_CONFIRM_DECISION,
                confirmed_by="Administrator",
                confirmed_at=confirmed_at,
            ),
            make_disposition_row(
                review_id="1",
                business_id="2",
                amount_safety="非金额安全候选",
                decision=UNRESOLVED_EXCLUDE_DECISION,
                confirmed_by="Administrator",
                confirmed_at=confirmed_at,
            ),
        )

        with self.assertRaisesRegex(
            VoucherConflictRepairError,
            "在处置清单中重复",
        ):
            build_repair_plan(rows)

    def test_build_plan_rejects_invalid_identifiers(self):
        confirmed_at = "2026-08-06 15:57:06"
        rows = (
            make_disposition_row(
                review_id="not-an-id",
                decision=UNRESOLVED_CONFIRM_DECISION,
                confirmed_by="Administrator",
                confirmed_at=confirmed_at,
            ),
            make_disposition_row(
                review_id="2",
                business_id="2",
                amount_safety="非金额安全候选",
                decision=UNRESOLVED_EXCLUDE_DECISION,
                confirmed_by="Administrator",
                confirmed_at=confirmed_at,
            ),
        )

        with self.assertRaisesRegex(
            VoucherConflictRepairError,
            "审核记录ID必须是正整数",
        ):
            build_repair_plan(rows)

    def test_csv_reader_preserves_header_and_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "disposition.csv"
            rows = (
                make_disposition_row(),
                make_disposition_row(
                    review_id="2",
                    business_id="2",
                ),
            )
            write_disposition_csv(path, rows)

            loaded_rows = read_manual_disposition_csv(path)

        self.assertEqual(loaded_rows, rows)

    def test_validate_actions_detects_database_drift(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "test.db"
            create_test_database(db_path)
            connection = sqlite3.connect(db_path)
            connection.row_factory = sqlite3.Row
            action = make_repair_plan().actions[0]
            connection.execute(
                "UPDATE match_reviews SET review_status = '已通过' WHERE id = 2"
            )
            connection.commit()

            with self.assertRaisesRegex(
                VoucherConflictRepairError,
                "数据库现状已变化",
            ):
                validate_actions_against_database(
                    connection,
                    (action,),
                )

            connection.close()

    def test_backup_is_complete_and_preserves_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db_path = root / "test.db"
            backup_directory = root / "backups"
            create_test_database(db_path)

            backup_path = create_database_backup(
                db_path,
                backup_directory,
            )

            source = sqlite3.connect(db_path)
            backup = sqlite3.connect(backup_path)
            source_rows = source.execute(
                "SELECT * FROM match_reviews ORDER BY id"
            ).fetchall()
            backup_rows = backup.execute(
                "SELECT * FROM match_reviews ORDER BY id"
            ).fetchall()
            source.close()
            backup.close()

        self.assertEqual(source_rows, backup_rows)

    def test_apply_updates_only_target_status_and_keeps_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db_path = root / "test.db"
            backup_directory = root / "backups"
            create_test_database(db_path)

            result = apply_repair_plan(
                make_repair_plan(),
                db_path=db_path,
                backup_directory=backup_directory,
            )

            connection = sqlite3.connect(db_path)
            repaired_rows = connection.execute(
                """
                SELECT
                    id,
                    review_status,
                    primary_review_result,
                    secondary_review_result
                FROM match_reviews
                ORDER BY id
                """
            ).fetchall()
            connection.close()
            backup = sqlite3.connect(result.backup_path)
            original_statuses = backup.execute(
                "SELECT id, review_status FROM match_reviews ORDER BY id"
            ).fetchall()
            backup.close()

        self.assertEqual(
            repaired_rows,
            [
                (1, "待审核", None, None),
                (2, "已驳回", None, None),
                (3, "已通过", "通过", "通过"),
                (4, "已驳回", "通过", "通过"),
            ],
        )
        self.assertEqual(
            original_statuses,
            [
                (1, "待审核"),
                (2, "待审核"),
                (3, "已通过"),
                (4, "已通过"),
            ],
        )
        self.assertEqual(result.updated_count, 2)
        self.assertEqual(result.quick_check, "ok")
        self.assertEqual(
            result.before_voucher_overallocation_count,
            1,
        )
        self.assertEqual(
            result.after_voucher_overallocation_count,
            0,
        )

    def test_apply_rolls_back_when_any_action_drifted(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db_path = root / "test.db"
            backup_directory = root / "backups"
            create_test_database(db_path)
            plan = make_repair_plan()
            connection = sqlite3.connect(db_path)
            connection.execute(
                "UPDATE match_reviews SET review_status = '已驳回' WHERE id = 4"
            )
            connection.commit()
            connection.close()

            with self.assertRaisesRegex(
                VoucherConflictRepairError,
                "数据库现状已变化",
            ):
                apply_repair_plan(
                    plan,
                    db_path=db_path,
                    backup_directory=backup_directory,
                )

            connection = sqlite3.connect(db_path)
            statuses = connection.execute(
                "SELECT id, review_status FROM match_reviews ORDER BY id"
            ).fetchall()
            connection.close()

        self.assertEqual(
            statuses,
            [
                (1, "待审核"),
                (2, "待审核"),
                (3, "已通过"),
                (4, "已驳回"),
            ],
        )

    def test_main_defaults_to_read_only_preview(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db_path = root / "test.db"
            csv_path = root / "disposition.csv"
            backup_directory = root / "backups"
            create_test_database(db_path)
            confirmed_at = "2026-08-06 15:57:06"
            rows = (
                make_disposition_row(
                    decision=UNRESOLVED_CONFIRM_DECISION,
                    confirmed_by="Administrator",
                    confirmed_at=confirmed_at,
                ),
                make_disposition_row(
                    review_id="2",
                    business_id="2",
                    amount_safety="非金额安全候选",
                    decision=UNRESOLVED_EXCLUDE_DECISION,
                    confirmed_by="Administrator",
                    confirmed_at=confirmed_at,
                ),
            )
            write_disposition_csv(csv_path, rows)

            with patch(
                "repair_voucher_assignment_conflicts.run_read_only_audit"
            ) as audit_mock:
                plan = main(
                    disposition_csv=csv_path,
                    apply=False,
                    db_path=db_path,
                    backup_directory=backup_directory,
                )

            connection = sqlite3.connect(db_path)
            status = connection.execute(
                "SELECT review_status FROM match_reviews WHERE id = 2"
            ).fetchone()[0]
            connection.close()

        audit_mock.assert_called_once_with(
            validate_csv_path=csv_path
        )
        self.assertEqual(len(plan.actions), 1)
        self.assertEqual(status, LEGACY_PENDING_REVIEW_STATUS)
        self.assertFalse(backup_directory.exists())


if __name__ == "__main__":
    unittest.main()
