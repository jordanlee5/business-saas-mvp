import re

from app.database import engine, SessionLocal, Base
from app import models
from app.models import BusinessRecord


def get_columns(table_name):
    """
    获取某张表当前已有的字段名。
    """
    with engine.connect() as conn:
        result = conn.exec_driver_sql(f"PRAGMA table_info({table_name})")
        return [row[1] for row in result.fetchall()]


def add_column_if_missing(table_name, column_name, column_sql):
    """
    如果字段不存在，就自动新增字段。
    如果字段已存在，就跳过。
    """
    columns = get_columns(table_name)

    if column_name in columns:
        print(f"[字段检查] {table_name}.{column_name} 已存在，跳过")
        return

    with engine.begin() as conn:
        conn.exec_driver_sql(
            f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_sql}"
        )

    print(f"[字段检查] {table_name}.{column_name} 已新增")


def fix_missing_columns():
    """
    修复本地 SQLite 数据库可能缺失的字段。
    """

    print("\n========== 1. 检查数据库字段 ==========")

    Base.metadata.create_all(bind=engine)
    print("[数据表检查] 已检查并创建缺失的数据表")

    add_column_if_missing(
        "business_records",
        "business_no",
        "VARCHAR",
    )

    add_column_if_missing(
        "voucher_records",
        "file_hash",
        "VARCHAR",
    )

    add_column_if_missing(
        "voucher_records",
        "voucher_amount",
        "FLOAT DEFAULT 0.0",
    )


def clean_digit_text(value):
    """
    清洗手机号、银行卡号这类数字编号字段。

    示例：
    15515516138.0 -> 15515516138
    15287554242.6667 -> 15287554242
    6214 8301 8468 3745 -> 6214830184683745
    """

    if value is None:
        return ""

    text = str(value).strip()

    if "." in text:
        left_part = text.split(".")[0]
        if left_part.isdigit():
            text = left_part

    text = re.sub(r"\D", "", text)

    return text


def fix_business_no_and_clean_records():
    """
    补全历史业务单号，并清洗历史手机号、银行卡号。
    """

    print("\n========== 2. 补全业务单号 / 清洗历史数据 ==========")

    db = SessionLocal()

    records = db.query(BusinessRecord).order_by(BusinessRecord.id.asc()).all()

    fixed_business_no_count = 0
    cleaned_count = 0

    for record in records:
        changed = False

        if not record.business_no:
            old_business_no = record.business_no
            record.business_no = f"BRH{record.id:09d}"
            fixed_business_no_count += 1
            changed = True

            print(
                f"[业务单号] ID {record.id}："
                f"{old_business_no} -> {record.business_no}"
            )

        old_phone = record.phone or ""
        old_bank_card = record.bank_card or ""

        new_phone = clean_digit_text(old_phone)
        new_bank_card = clean_digit_text(old_bank_card)

        if new_phone != old_phone:
            record.phone = new_phone
            changed = True

            print(
                f"[手机号清洗] ID {record.id}："
                f"{old_phone} -> {new_phone}"
            )

        if new_bank_card != old_bank_card:
            record.bank_card = new_bank_card
            changed = True

            print(
                f"[银行卡清洗] ID {record.id}："
                f"{old_bank_card} -> {new_bank_card}"
            )

        if changed:
            cleaned_count += 1

    db.commit()

    empty_business_no_count = (
        db.query(BusinessRecord)
        .filter(
            (BusinessRecord.business_no == None) |  # noqa: E711
            (BusinessRecord.business_no == "")
        )
        .count()
    )

    db.close()

    print(f"\n业务单号补全数量：{fixed_business_no_count}")
    print(f"发生清洗或更新的数据行数：{cleaned_count}")
    print(f"空业务单号剩余数量：{empty_business_no_count}")


def main():
    print("开始执行本地数据库维护脚本...")

    fix_missing_columns()
    fix_business_no_and_clean_records()

    print("\n========== 维护完成 ==========")
    print("本地数据库字段、历史业务单号、手机号和银行卡号已检查完成。")


if __name__ == "__main__":
    main()