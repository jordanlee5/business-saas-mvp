import sqlite3
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from audit_duplicate_business_allocation_case import (
    TARGET_BUSINESS_ID,
    TARGET_REVIEW_TO_VOUCHER,
    TARGET_VOUCHER_IDS,
    build_file_evidence,
    compare_voucher_evidence,
    fetch_action_logs,
    fetch_business,
    fetch_related_reviews,
    fetch_target_reviews,
    fetch_vouchers,
    file_sha256,
    money,
    normalize_ocr_text,
    normalized_money,
    resolve_voucher_file_path,
    text_sha256,
    validate_business_overpayment_scope,
    validate_schema,
    validate_target_scope,
)


class MappingRow(dict):
    pass


def make_business(amount="31402.06"):
    return MappingRow(
        business_id=TARGET_BUSINESS_ID,
        business_amount=amount,
    )


def make_voucher(voucher_id, **overrides):
    values = {
        "voucher_id": voucher_id,
        "voucher_amount": "31402.06",
        "filename": f"voucher-{voucher_id}.png",
        "file_path": f"uploads/voucher-{voucher_id}.png",
        "file_hash": f"hash-{voucher_id}",
        "ocr_text": f"OCR {voucher_id}",
    }
    values.update(overrides)
    return MappingRow(values)


def make_review(review_id, voucher_id, **overrides):
    values = {
        "review_id": review_id,
        "voucher_id": voucher_id,
        "business_record_id": TARGET_BUSINESS_ID,
        "review_status": "已通过",
        "allocation_amount": "31402.06",
    }
    values.update(overrides)
    return MappingRow(values)


def make_target_rows():
    vouchers = [
        make_voucher(voucher_id)
        for voucher_id in TARGET_VOUCHER_IDS
    ]
    reviews = [
        make_review(review_id, voucher_id)
        for review_id, voucher_id in (
            TARGET_REVIEW_TO_VOUCHER.items()
        )
    ]
    return [make_business()], vouchers, reviews


def build_in_memory_target_database():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript(
        """
        CREATE TABLE users (
            id INTEGER PRIMARY KEY,
            username TEXT,
            role TEXT,
            admin_level TEXT
        );
        CREATE TABLE upload_batches (
            id INTEGER PRIMARY KEY,
            filename TEXT,
            acceptance_status TEXT,
            created_at TEXT
        );
        CREATE TABLE business_records (
            id INTEGER PRIMARY KEY,
            user_id INTEGER,
            batch_id INTEGER,
            business_no TEXT,
            public_business_no TEXT,
            name TEXT,
            phone TEXT,
            plate_number TEXT,
            points_amount NUMERIC,
            bank_card TEXT,
            created_at TEXT
        );
        CREATE TABLE voucher_upload_batches (
            id INTEGER PRIMARY KEY,
            uploader_id INTEGER,
            partner_id INTEGER,
            total_files INTEGER,
            success_files INTEGER,
            duplicate_files INTEGER,
            failed_files INTEGER,
            total_created_reviews INTEGER,
            created_at TEXT
        );
        CREATE TABLE voucher_records (
            id INTEGER PRIMARY KEY,
            uploader_id INTEGER,
            batch_id INTEGER,
            filename TEXT,
            file_path TEXT,
            file_hash TEXT,
            voucher_amount NUMERIC,
            ocr_text TEXT,
            created_at TEXT
        );
        CREATE TABLE match_reviews (
            id INTEGER PRIMARY KEY,
            voucher_id INTEGER,
            business_record_id INTEGER,
            match_status TEXT,
            name_match TEXT,
            bank_match TEXT,
            amount_match TEXT,
            score INTEGER,
            review_status TEXT,
            allocation_amount NUMERIC,
            primary_reviewer_id INTEGER,
            primary_review_result TEXT,
            primary_review_comment TEXT,
            primary_reviewed_at TEXT,
            secondary_reviewer_id INTEGER,
            secondary_review_result TEXT,
            secondary_review_comment TEXT,
            secondary_reviewed_at TEXT,
            created_at TEXT
        );
        CREATE TABLE admin_action_logs (
            id INTEGER PRIMARY KEY,
            admin_id INTEGER,
            action_type TEXT,
            target_type TEXT,
            target_id INTEGER,
            description TEXT,
            created_at TEXT
        );

        INSERT INTO users VALUES
            (1, 'partner', 'partner', NULL),
            (2, 'primary', 'admin', 'primary_reviewer'),
            (3, 'secondary', 'admin', 'secondary_reviewer');
        INSERT INTO upload_batches VALUES
            (9, 'business.xlsx', '已承接', '2026-01-01 08:00:00');
        INSERT INTO business_records VALUES
            (
                63, 1, 9, 'OLD-63', 'BR-TARGET', '测试客户',
                '13800000000', 'TEST63', 31402.06,
                '6222000000000063', '2026-01-01 08:01:00'
            );
        INSERT INTO voucher_upload_batches VALUES
            (
                10, 2, 1, 1, 1, 0, 0, 1,
                '2026-01-02 08:00:00'
            ),
            (
                11, 2, 1, 1, 1, 0, 0, 1,
                '2026-01-03 08:00:00'
            );
        INSERT INTO voucher_records VALUES
            (
                57, 2, 10, 'voucher-57.png', 'uploads/voucher-57.png',
                'hash57', 31402.06, '流水号 57',
                '2026-01-02 08:00:01'
            ),
            (
                64, 2, 11, 'voucher-64.png', 'uploads/voucher-64.png',
                'hash64', 31402.06, '流水号 64',
                '2026-01-03 08:00:01'
            );
        INSERT INTO match_reviews VALUES
            (
                130, 57, 63, '匹配成功', '完整匹配', '完整匹配', '是',
                8, '已通过', 31402.06,
                2, '通过', '初审130', '2026-01-02 09:00:00',
                3, '通过', '复核130', '2026-01-02 10:00:00',
                '2026-01-02 08:00:02'
            ),
            (
                151, 64, 63, '匹配成功', '完整匹配', '完整匹配', '是',
                8, '已通过', 31402.06,
                2, '通过', '初审151', '2026-01-03 09:00:00',
                3, '通过', '复核151', '2026-01-03 10:00:00',
                '2026-01-03 08:00:02'
            );
        INSERT INTO admin_action_logs VALUES
            (
                20, 2, 'primary_approve_match', 'match_review', 130,
                '管理员初审通过匹配结果 #130', '2026-01-02 09:00:00'
            ),
            (
                21, 3, 'secondary_approve_match', 'match_review', 151,
                '管理员复核通过匹配结果 #151', '2026-01-03 10:00:00'
            );
        """
    )
    return connection


class DuplicateBusinessAllocationAuditTests(unittest.TestCase):
    def test_all_target_queries_run_against_current_schema_shape(self):
        connection = build_in_memory_target_database()

        try:
            validate_schema(connection)
            businesses = fetch_business(connection)
            vouchers = fetch_vouchers(connection)
            target_reviews = fetch_target_reviews(connection)
            related_reviews = fetch_related_reviews(connection)
            logs = fetch_action_logs(connection)

            validate_target_scope(
                businesses,
                vouchers,
                target_reviews,
            )
            validate_business_overpayment_scope(
                related_reviews,
            )

            self.assertEqual(len(businesses), 1)
            self.assertEqual(len(vouchers), 2)
            self.assertEqual(len(target_reviews), 2)
            self.assertEqual(len(related_reviews), 2)
            self.assertEqual(len(logs), 2)
            self.assertEqual(
                target_reviews[0]["primary_reviewer_username"],
                "primary",
            )
            self.assertEqual(
                target_reviews[1]["secondary_reviewer_username"],
                "secondary",
            )
        finally:
            connection.close()

    def test_money_uses_two_decimal_places(self):
        self.assertEqual(money("31402.055"), "31402.06")
        self.assertEqual(normalized_money("31402.055"), Decimal("31402.06"))

    def test_invalid_money_returns_none_from_normalizer(self):
        for value in (None, True, "invalid", "NaN"):
            with self.subTest(value=value):
                self.assertIsNone(normalized_money(value))

    def test_ocr_normalization_only_collapses_whitespace(self):
        self.assertEqual(
            normalize_ocr_text("  交易\n时间\t  12:00  "),
            "交易 时间 12:00",
        )

    def test_text_hash_is_stable(self):
        self.assertEqual(text_sha256("same"), text_sha256("same"))
        self.assertNotEqual(text_sha256("left"), text_sha256("right"))

    def test_file_hash_reads_file_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "voucher.bin"
            path.write_bytes(b"voucher evidence")
            self.assertEqual(
                file_sha256(path),
                "007569d3215a7ef8106b18b10f046049"
                "f1ddcd658081c9a9471ddf35810ee83e",
            )

    def test_relative_voucher_path_resolves_from_database_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database_path = root / "saas_mvp.db"
            database_path.write_bytes(b"")
            voucher_path = root / "uploads" / "voucher.png"
            voucher_path.parent.mkdir()
            voucher_path.write_bytes(b"voucher")

            self.assertEqual(
                resolve_voucher_file_path(
                    database_path,
                    "uploads/voucher.png",
                ),
                voucher_path.resolve(),
            )

    def test_file_evidence_compares_disk_hash_with_database_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database_path = root / "saas_mvp.db"
            database_path.write_bytes(b"")
            voucher_path = root / "voucher.png"
            voucher_path.write_bytes(b"voucher")
            voucher = make_voucher(
                57,
                file_path="voucher.png",
                file_hash=file_sha256(voucher_path),
            )

            evidence = build_file_evidence(
                database_path,
                voucher,
            )

            self.assertTrue(evidence["exists"])
            self.assertEqual(evidence["size"], 7)
            self.assertTrue(evidence["hash_matches_database"])

    def test_comparison_detects_same_material_after_ocr_whitespace(self):
        vouchers = [
            make_voucher(
                57,
                filename="same.png",
                file_hash="ABC",
                ocr_text="交易\n 时间",
            ),
            make_voucher(
                64,
                filename="same.png",
                file_hash="abc",
                ocr_text="交易 时间",
            ),
        ]
        files = [
            {
                "voucher_id": 57,
                "actual_hash": "DEF",
            },
            {
                "voucher_id": 64,
                "actual_hash": "def",
            },
        ]

        result = compare_voucher_evidence(vouchers, files)

        self.assertTrue(result["same_filename"])
        self.assertTrue(result["same_amount"])
        self.assertTrue(result["same_database_hash"])
        self.assertTrue(result["same_actual_hash"])
        self.assertFalse(result["same_ocr_exact"])
        self.assertTrue(result["same_ocr_normalized"])

    def test_comparison_does_not_claim_disk_equality_when_file_missing(self):
        vouchers = [
            make_voucher(57),
            make_voucher(64),
        ]
        files = [
            {"voucher_id": 57, "actual_hash": None},
            {"voucher_id": 64, "actual_hash": None},
        ]

        result = compare_voucher_evidence(vouchers, files)

        self.assertFalse(result["actual_hashes_available"])
        self.assertFalse(result["same_actual_hash"])

    def test_target_scope_accepts_exact_frozen_case(self):
        businesses, vouchers, reviews = make_target_rows()
        validate_target_scope(businesses, vouchers, reviews)

    def test_target_scope_rejects_changed_review_status(self):
        businesses, vouchers, reviews = make_target_rows()
        reviews[0]["review_status"] = "已驳回"

        with self.assertRaisesRegex(
            RuntimeError,
            "不再是已通过状态",
        ):
            validate_target_scope(
                businesses,
                vouchers,
                reviews,
            )

    def test_target_scope_rejects_changed_relationship(self):
        businesses, vouchers, reviews = make_target_rows()
        reviews[0]["voucher_id"] = 64

        with self.assertRaisesRegex(
            RuntimeError,
            "凭证关联已变化",
        ):
            validate_target_scope(
                businesses,
                vouchers,
                reviews,
            )

    def test_target_scope_rejects_changed_amount(self):
        businesses, vouchers, reviews = make_target_rows()
        reviews[1]["allocation_amount"] = "1.00"

        with self.assertRaisesRegex(
            RuntimeError,
            "核销金额已变化",
        ):
            validate_target_scope(
                businesses,
                vouchers,
                reviews,
            )

    def test_business_overpayment_scope_accepts_exact_pair(self):
        _, _, reviews = make_target_rows()
        validate_business_overpayment_scope(reviews)

    def test_business_overpayment_scope_rejects_extra_approval(self):
        _, _, reviews = make_target_rows()
        reviews.append(
            make_review(200, 80)
        )

        with self.assertRaisesRegex(
            RuntimeError,
            "已通过审核集合不再是",
        ):
            validate_business_overpayment_scope(reviews)

    def test_business_overpayment_scope_ignores_other_business(self):
        _, _, reviews = make_target_rows()
        other = make_review(200, 80)
        other["business_record_id"] = 999
        reviews.append(other)
        validate_business_overpayment_scope(reviews)


if __name__ == "__main__":
    unittest.main()
