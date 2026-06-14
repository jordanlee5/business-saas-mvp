import os
import re
from PIL import Image
from decimal import Decimal, ROUND_HALF_UP
from rapidocr import RapidOCR


rapid_ocr_engine = RapidOCR()

def prepare_image_for_ocr(file_path: str) -> str:
    """
    OCR 图片预处理：
    对较长的手机截图，只截取上半部分，减少无关内容干扰。
    返回处理后的图片路径。
    """

    image = Image.open(file_path)
    width, height = image.size

    # 如果图片明显是长截图，就只保留上半部分约 70%
    if height > width * 1.5:
        crop_height = int(height * 0.72)
        cropped = image.crop((0, 0, width, crop_height))

        base_name, ext = os.path.splitext(file_path)
        processed_path = f"{base_name}_ocr_crop{ext}"

        cropped.save(processed_path)
        return processed_path

    return file_path


def normalize_text(text: str) -> str:
    """
    清理 OCR 文本：
    - 去掉空格
    - 去掉换行
    - 去掉常见分隔符
    """
    if not text:
        return ""

    return (
        text.replace(" ", "")
        .replace("\n", "")
        .replace("\r", "")
        .replace("\t", "")
        .replace(",", "")
        .replace("，", "")
    )


def normalize_bank_card(card: str) -> str:
    """
    银行卡号标准化：只保留数字
    """
    if not card:
        return ""
    return re.sub(r"\D", "", card)

def match_bank_card(bank_card: str, ocr_text: str):
    """
    银行卡匹配：
    1. 如果完整银行卡号出现在 OCR 文本里，算完整匹配
    2. 如果 OCR 文本里是 6217****5942 这种脱敏格式，
       只要前 4 位和后 4 位都匹配，也算脱敏匹配
    """

    excel_card = normalize_bank_card(bank_card)

    if not excel_card:
        return False, "否", 0

    normalized_ocr = normalize_bank_card(ocr_text)

    # 完整卡号匹配
    if excel_card in normalized_ocr:
        return True, "完整匹配", 3

    # 脱敏卡号匹配：前4位 + 后4位
    if len(excel_card) >= 8:
        first4 = excel_card[:4]
        last4 = excel_card[-4:]

        if first4 in normalized_ocr and last4 in normalized_ocr:
            return True, f"脱敏匹配，前4位和后4位一致：{first4}****{last4}", 2

    return False, "否", 0


def normalize_amount(amount) -> str:
    """
    金额标准化：
    154000.0 -> 154000
    154000.50 -> 154000.5
    """
    if amount is None:
        return ""

    try:
        value = float(amount)
        if value.is_integer():
            return str(int(value))
        return str(value).rstrip("0").rstrip(".")
    except Exception:
        return str(amount)


def normalize_amount_2dp(amount):
    """
    金额统一按两位小数比较。
    例如：
    40022.484 -> 40022.48
    31775 -> 31775.00
    """

    if amount is None:
        return ""

    try:
        value = Decimal(str(amount))
        value = value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        return format(value, "f")
    except Exception:
        return ""


def extract_voucher_amount(ocr_text: str):
    """
    从 OCR 文本中提取凭证转账金额。

    优先识别这些关键词附近的金额：
    转账金额、金额（小写）、金额(小写)、交易金额

    如果找不到关键词金额，再从全文里找金额数字。
    """

    if not ocr_text:
        return None

    text = ocr_text.replace(",", "").replace("￥", "").replace("¥", "")

    amount_keywords = [
        "转账金额",
        "金额（小写）",
        "金额(小写)",
        "交易金额",
        "金额",
    ]

    lines = [line.strip() for line in text.splitlines() if line.strip()]

    for index, line in enumerate(lines):
        for keyword in amount_keywords:
            if keyword in line:
                # 情况1：金额和关键词在同一行，例如：转账金额 40022.48
                numbers = re.findall(r"\d+\.\d{1,3}|\d+", line)
                if numbers:
                    try:
                        value = Decimal(numbers[-1]).quantize(
                            Decimal("0.01"),
                            rounding=ROUND_HALF_UP,
                        )
                        return float(value)
                    except Exception:
                        pass

                # 情况2：关键词在一行，金额在下一行，例如：
                # 转账金额
                # 40022.48
                if index + 1 < len(lines):
                    next_line = lines[index + 1]
                    numbers = re.findall(r"\d+\.\d{1,3}|\d+", next_line)
                    if numbers:
                        try:
                            value = Decimal(numbers[0]).quantize(
                                Decimal("0.01"),
                                rounding=ROUND_HALF_UP,
                            )
                            return float(value)
                        except Exception:
                            pass

    # 兜底：从全文提取带小数的金额，优先取看起来像金额的数字
    numbers = re.findall(r"\d+\.\d{1,3}", text)

    if numbers:
        try:
            value = Decimal(numbers[0]).quantize(
                Decimal("0.01"),
                rounding=ROUND_HALF_UP,
            )
            return float(value)
        except Exception:
            return None

    return None


def match_name(name: str, normalized_ocr: str):
    """
    姓名匹配：
    - 完整姓名出现：强匹配
    - 部分汉字出现：疑似匹配
    """

    if not name:
        return False, 0, "未匹配"

    clean_name = name.strip()

    if clean_name in normalized_ocr:
        return True, len(clean_name), "完整匹配"

    chars = [ch for ch in clean_name if "\u4e00" <= ch <= "\u9fff"]

    if not chars:
        return False, 0, "未匹配"

    hit_count = sum(1 for ch in chars if ch in normalized_ocr)

    # 两个字姓名：命中 1 个字，算疑似
    # 三个字姓名：命中 1 个字以上，算疑似；命中 2 个以上更可信
    if hit_count >= 1:
        return True, hit_count, f"疑似匹配，命中 {hit_count}/{len(chars)} 个字"

    return False, 0, "未匹配"


def ocr_image(file_path: str) -> str:
    ocr_file_path = prepare_image_for_ocr(file_path)

    result = rapid_ocr_engine(ocr_file_path)

    if not result or not result.txts:
        raise Exception("RapidOCR 未识别到任何文字")

    return "\n".join(result.txts)


def match_ocr_with_records(ocr_text: str, records):
    """
    将 OCR 文本与业务记录匹配。
    records 是 BusinessRecord 列表。
    """

    normalized_ocr = normalize_text(ocr_text)
    results = []

    for record in records:
        name = record.name or ""
        amount = normalize_amount(record.points_amount)
        amount_2dp = normalize_amount_2dp(record.points_amount)

        name_match, name_score, name_detail = match_name(name, normalized_ocr)

        bank_match, bank_detail, bank_score = match_bank_card(
            record.bank_card,
            ocr_text,
        )

        amount_match = False

        if amount and amount in normalized_ocr:
            amount_match = True

        if amount_2dp and amount_2dp in normalized_ocr:
            amount_match = True

        score = 0

        if name_match:
            score += name_score

        score += bank_score

        if amount_match:
            score += 3

        if score >= 5:
            status = "匹配成功"
        elif score >= 1:
            status = "部分匹配"
        else:
            status = "未匹配"

        results.append(
            {
                "record": record,
                "bank_match": bank_match,
                "bank_detail": bank_detail,
                "name_match": name_match,
                "name_detail": name_detail,
                "amount_match": amount_match,
                "score": score,
                "status": status,
            }
        )

    # 只返回有匹配迹象的记录，按分数从高到低排序
    results = [r for r in results if r["score"] > 0]
    results.sort(key=lambda x: x["score"], reverse=True)

    return results