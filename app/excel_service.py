import pandas as pd


REQUIRED_COLUMNS = ["姓名", "手机号", "车牌号", "积分金额", "银行卡号"]


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

        name = str(row["姓名"]).strip() if pd.notna(row["姓名"]) else ""
        phone = str(row["手机号"]).strip() if pd.notna(row["手机号"]) else ""
        plate_number = str(row["车牌号"]).strip() if pd.notna(row["车牌号"]) else ""
        bank_card = str(row["银行卡号"]).strip() if pd.notna(row["银行卡号"]) else ""

        try:
            points_amount = float(row["积分金额"])
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