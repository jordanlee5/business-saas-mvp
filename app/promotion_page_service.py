import re
from urllib.parse import urlparse


DEFAULT_PRIMARY_COLOR = "#2563EB"

PROMOTION_SLUG_PATTERN = re.compile(
    r"^[a-z0-9]+(?:-[a-z0-9]+)*$"
)

HEX_COLOR_PATTERN = re.compile(
    r"^#[0-9A-F]{6}$"
)


def normalize_promotion_slug(
    value: str | None,
) -> str:
    return (value or "").strip().lower()


def normalize_primary_color(
    value: str | None,
) -> str:
    return (
        value
        or DEFAULT_PRIMARY_COLOR
    ).strip().upper()


def is_safe_promotion_cta_url(
    value: str,
) -> bool:
    url = value.strip()

    # 允许系统内部地址，
    # 但拒绝 //example.com 和反斜杠形式。
    if (
        url.startswith("/")
        and not url.startswith("//")
        and "\\" not in url
    ):
        return True

    parsed = urlparse(url)

    return bool(
        parsed.scheme == "https"
        and parsed.netloc
        and not parsed.username
        and not parsed.password
    )


def validate_promotion_page_input(
    *,
    slug: str,
    company_name: str,
    page_title: str,
    primary_color: str,
    cta_text: str = "",
    cta_url: str = "",
) -> str | None:
    normalized_slug = (
        normalize_promotion_slug(slug)
    )

    if (
        len(normalized_slug) < 3
        or len(normalized_slug) > 80
        or not PROMOTION_SLUG_PATTERN.fullmatch(
            normalized_slug
        )
    ):
        return (
            "页面短链接只能使用小写字母、"
            "数字和中划线，长度为 3～80 个字符"
        )

    if not company_name.strip():
        return "保险公司名称不能为空"

    if not page_title.strip():
        return "宣传页标题不能为空"

    normalized_color = (
        normalize_primary_color(
            primary_color
        )
    )

    if not HEX_COLOR_PATTERN.fullmatch(
        normalized_color
    ):
        return "主题色必须使用六位十六进制颜色"

    normalized_cta_text = cta_text.strip()
    normalized_cta_url = cta_url.strip()

    if bool(normalized_cta_text) != bool(
        normalized_cta_url
    ):
        return (
            "按钮文字和按钮链接需要同时填写"
        )

    if (
        normalized_cta_url
        and not is_safe_promotion_cta_url(
            normalized_cta_url
        )
    ):
        return (
            "按钮链接只能使用系统内部地址"
            "或 HTTPS 地址"
        )

    return None