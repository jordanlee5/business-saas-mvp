from datetime import datetime
from zoneinfo import ZoneInfo


UTC8_TIMEZONE = ZoneInfo("Asia/Shanghai")
DEFAULT_DATETIME_FORMAT = "%Y-%m-%d %H:%M:%S"


def utc8_now() -> datetime:
    """
    返回当前 UTC+8 时间。

    当前项目使用 SQLite。为兼容现有 DateTime 字段和日期筛选，
    数据库存储使用不携带 tzinfo 的 UTC+8 墙上时间。
    """
    return datetime.now(
        UTC8_TIMEZONE
    ).replace(tzinfo=None)


def format_utc8(
    value: datetime | None,
    format_string: str = DEFAULT_DATETIME_FORMAT,
) -> str:
    """
    将时间统一格式化为 UTC+8。

    - 有时区信息：先转换到 Asia/Shanghai；
    - 无时区信息：视为数据库中已经保存的 UTC+8 时间；
    - 空值：统一显示为短横线。
    """
    if value is None:
        return "-"

    if (
        value.tzinfo is not None
        and value.utcoffset() is not None
    ):
        value = value.astimezone(
            UTC8_TIMEZONE
        ).replace(tzinfo=None)

    return value.strftime(format_string)