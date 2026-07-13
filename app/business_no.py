import secrets


# 排除容易混淆的字符：
# 0、1、I、L、O、U
PUBLIC_BUSINESS_NO_ALPHABET = "23456789ABCDEFGHJKMNPQRSTVWXYZ"
PUBLIC_BUSINESS_NO_LENGTH = 18


def generate_public_business_no() -> str:
    """
    生成对外公开业务单号。

    示例：
    BR-7K9M4Q2X8D6F3P5NRT

    该编号不包含日期、批次、数据库 ID 等可推测信息。
    """
    random_part = "".join(
        secrets.choice(PUBLIC_BUSINESS_NO_ALPHABET)
        for _ in range(PUBLIC_BUSINESS_NO_LENGTH)
    )

    return f"BR-{random_part}"