import pandas as pd
import re
from decimal import Decimal, ROUND_HALF_UP


REQUIRED_COLUMNS = ["姓名", "手机号", "车牌号", "积分金额", "银行卡号"]


def clean_text_cell(value):
    """
    清洗普通文本字段，比如姓名、车牌号。
    """
    if pd.isna(value):
        return ""

    text = str(value).strip()

    if text.endswith(".0"):
        text = text[:-2]

    return text


def clean_digit_cell(value):
    """
    清洗手机号、银行卡号这类纯数字编号。
    防止 Excel 把它们读成 123456.0 或 123456.6667。
    """
    if pd.isna(value):
        return ""

    text = str(value).strip()

    # 如果是 123456.0 或 123456.6667 这种形式，先取小数点前面
    if "." in text:
        left_part = text.split(".")[0]
        if left_part.isdigit():
            text = left_part

    # 只保留数字
    text = re.sub(r"\D", "", text)

    return text


def round_money_2(value):
    if value is None:
        return None

    try:
        return float(
            Decimal(str(value)).quantize(
                Decimal("0.01"),
                rounding=ROUND_HALF_UP
            )
        )
    except Exception:
        return None


def parse_business_excel(file_path: str):
    """
    读取固定模板 Excel，并返回：
    - records: 合格数据列表
    - errors: 错误信息列表
    """

    df = pd.read_excel(file_path)

    # 去掉列名两边空格
    df.columns = [str(col).strip() for col in df.columns]

    missing_columns = [col for col in REQUIRED_COLUMNS if col not in df.columns]
    if missing_columns:
        return [], [f"缺少必要字段：{', '.join(missing_columns)}"]

    records = []
    errors = []

    for index, row in df.iterrows():
        row_number = index + 2

        name = clean_text_cell(row["姓名"])
        phone = clean_digit_cell(row["手机号"])
        plate_number = clean_text_cell(row["车牌号"])
        bank_card = clean_digit_cell(row["银行卡号"])

        try:
            points_amount = round_money_2(row["积分金额"])
        except Exception:
            points_amount = None

        if not name:
            errors.append(f"第 {row_number} 行：姓名为空")
            continue

        if not phone:
            errors.append(f"第 {row_number} 行：手机号为空")
            continue

        if not bank_card:
            errors.append(f"第 {row_number} 行：银行卡号为空")
            continue

        if points_amount is None:
            errors.append(f"第 {row_number} 行：积分金额不是数字")
            continue

        records.append(
            {
                "name": name,
                "phone": phone,
                "plate_number": plate_number,
                "points_amount": points_amount,
                "bank_card": bank_card,
            }
        )

    return records, errors