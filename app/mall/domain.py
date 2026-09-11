from calendar import monthrange
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from enum import Enum

from ..time_utils import UTC8_TIMEZONE


POINTS_QUANTUM = Decimal("0.01")


class BusinessChannel(str, Enum):
    """一条业务可以选择的积分使用渠道。"""

    CASH_REBATE = "CASH_REBATE"
    MALL_REDEMPTION = "MALL_REDEMPTION"


class BusinessClaimStatus(str, Enum):
    """商城渠道业务权益的领取状态。"""

    PENDING_ACTIVATION = "PENDING_ACTIVATION"
    ACTIVATED = "ACTIVATED"
    EXPIRED = "EXPIRED"
    FROZEN = "FROZEN"


class ActivationSecurityMethod(str, Enum):
    """会员激活可采用的附加安全因子。"""

    ONE_TIME_CODE = "ONE_TIME_CODE"
    SMS_OTP = "SMS_OTP"


class ActivationCredentialStatus(str, Enum):
    """一次激活凭据的生命周期状态。"""

    ACTIVE = "ACTIVE"
    USED = "USED"
    LOCKED = "LOCKED"
    EXPIRED = "EXPIRED"
    REVOKED = "REVOKED"


class PointsGrantStatus(str, Enum):
    """一个会员积分批次的状态。"""

    ACTIVE = "ACTIVE"
    EXHAUSTED = "EXHAUSTED"
    EXPIRED = "EXPIRED"
    FROZEN = "FROZEN"


class PointsLedgerEntryType(str, Enum):
    """不可变积分流水支持的业务动作。"""

    GRANT = "GRANT"
    RESERVE = "RESERVE"
    RELEASE = "RELEASE"
    CONSUME = "CONSUME"
    REFUND = "REFUND"
    EXPIRE = "EXPIRE"
    ADJUST = "ADJUST"


class ProductStatus(str, Enum):
    """商城商品在后台目录中的生命周期状态。"""

    DRAFT = "DRAFT"
    PUBLISHED = "PUBLISHED"
    UNPUBLISHED = "UNPUBLISHED"


class ProductMediaRole(str, Enum):
    """商品图片在目录中的稳定用途。"""

    MAIN = "MAIN"
    CAROUSEL = "CAROUSEL"
    DETAIL = "DETAIL"


class InventoryMovementType(str, Enum):
    """M4 阶段允许写入的 SKU 库存流水类型。"""

    RECEIPT = "RECEIPT"
    ADJUSTMENT = "ADJUSTMENT"


class InventoryStockStatus(str, Enum):
    """根据 SKU 可售数量和低库存阈值得出的只读状态。"""

    IN_STOCK = "IN_STOCK"
    LOW_STOCK = "LOW_STOCK"
    OUT_OF_STOCK = "OUT_OF_STOCK"


VALID_BUSINESS_CHANNELS = frozenset(
    channel.value
    for channel in BusinessChannel
)


VALID_BUSINESS_CLAIM_STATUSES = frozenset(
    status.value
    for status in BusinessClaimStatus
)


VALID_ACTIVATION_SECURITY_METHODS = frozenset(
    method.value
    for method in ActivationSecurityMethod
)


VALID_ACTIVATION_CREDENTIAL_STATUSES = frozenset(
    status.value
    for status in ActivationCredentialStatus
)


VALID_POINTS_GRANT_STATUSES = frozenset(
    status.value
    for status in PointsGrantStatus
)


VALID_POINTS_LEDGER_ENTRY_TYPES = frozenset(
    entry_type.value
    for entry_type in PointsLedgerEntryType
)


VALID_PRODUCT_STATUSES = frozenset(
    status.value
    for status in ProductStatus
)


VALID_PRODUCT_MEDIA_ROLES = frozenset(
    role.value
    for role in ProductMediaRole
)


VALID_INVENTORY_MOVEMENT_TYPES = frozenset(
    movement_type.value
    for movement_type in InventoryMovementType
)


VALID_INVENTORY_STOCK_STATUSES = frozenset(
    status.value
    for status in InventoryStockStatus
)


def normalize_business_channel(
    value: BusinessChannel | str,
) -> BusinessChannel:
    """
    将渠道值规范为 ``BusinessChannel``。

    渠道名称必须使用已确认的大写枚举值，避免把未知值
    静默归入现金返现或商城兑换。
    """
    if isinstance(value, BusinessChannel):
        return value

    if not isinstance(value, str):
        raise ValueError(
            "业务渠道必须是 CASH_REBATE 或 MALL_REDEMPTION"
        )

    normalized_value = value.strip()

    try:
        return BusinessChannel(normalized_value)
    except ValueError as exc:
        raise ValueError(
            "业务渠道必须是 CASH_REBATE 或 MALL_REDEMPTION"
        ) from exc


def normalize_activation_security_method(
    value: ActivationSecurityMethod | str,
) -> ActivationSecurityMethod:
    """规范激活安全因子；未知值必须失败关闭。"""
    if isinstance(value, ActivationSecurityMethod):
        return value

    if not isinstance(value, str):
        raise ValueError("不支持的激活安全因子")

    try:
        return ActivationSecurityMethod(value.strip())
    except ValueError as exc:
        raise ValueError("不支持的激活安全因子") from exc


def normalize_product_status(
    value: ProductStatus | str,
) -> ProductStatus:
    """规范商品状态；未知值必须失败关闭。"""
    if isinstance(value, ProductStatus):
        return value

    if not isinstance(value, str):
        raise ValueError("商品状态无效")

    try:
        return ProductStatus(value.strip())
    except ValueError as exc:
        raise ValueError("商品状态无效") from exc


def normalize_product_media_role(
    value: ProductMediaRole | str,
) -> ProductMediaRole:
    """规范商品图片用途；未知值必须失败关闭。"""
    if isinstance(value, ProductMediaRole):
        return value

    if not isinstance(value, str):
        raise ValueError("商品图片用途无效")

    try:
        return ProductMediaRole(value.strip().upper())
    except ValueError as exc:
        raise ValueError("商品图片用途无效") from exc


def normalize_inventory_movement_type(
    value: InventoryMovementType | str,
) -> InventoryMovementType:
    """规范库存流水类型；未知值必须失败关闭。"""
    if isinstance(value, InventoryMovementType):
        return value

    if not isinstance(value, str):
        raise ValueError("库存流水类型无效")

    try:
        return InventoryMovementType(value.strip().upper())
    except ValueError as exc:
        raise ValueError("库存流水类型无效") from exc


def classify_inventory_stock(
    *,
    on_hand_quantity: int,
    reserved_quantity: int,
    low_stock_threshold: int,
) -> InventoryStockStatus:
    """按可售数量判断正常、低库存或无库存状态。"""
    values = {
        "现存数量": on_hand_quantity,
        "预占数量": reserved_quantity,
        "低库存阈值": low_stock_threshold,
    }
    for field_name, value in values.items():
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"{field_name}必须是非负整数")
    if reserved_quantity > on_hand_quantity:
        raise ValueError("预占数量不能超过现存数量")

    available_quantity = on_hand_quantity - reserved_quantity
    if available_quantity == 0:
        return InventoryStockStatus.OUT_OF_STOCK
    if available_quantity <= low_stock_threshold:
        return InventoryStockStatus.LOW_STOCK
    return InventoryStockStatus.IN_STOCK


def normalize_points(
    value,
    field_name: str = "积分",
) -> Decimal:
    """
    将积分安全规范为保留两位小数的 ``Decimal``。

    此函数只负责精度规范，不限制正负号；后续入账、消费、
    退款和人工调整应各自在领域服务中校验允许的方向。
    """
    if isinstance(value, bool):
        raise ValueError(
            f"{field_name}不能是布尔值"
        )

    if isinstance(value, Decimal):
        decimal_value = value
    else:
        try:
            decimal_value = Decimal(
                str(value).strip()
            )
        except (
            InvalidOperation,
            AttributeError,
            ValueError,
        ) as exc:
            raise ValueError(
                f"{field_name}必须是有效数字"
            ) from exc

    if not decimal_value.is_finite():
        raise ValueError(
            f"{field_name}必须是有限数字"
        )

    return decimal_value.quantize(
        POINTS_QUANTUM,
        rounding=ROUND_HALF_UP,
    )


def _to_utc8_wall_time(
    value: datetime,
    field_name: str,
) -> datetime:
    """把时间转换为现有系统使用的无时区 UTC+8 墙上时间。"""
    if not isinstance(value, datetime):
        raise ValueError(
            f"{field_name}必须是有效时间"
        )

    if (
        value.tzinfo is not None
        and value.utcoffset() is not None
    ):
        return value.astimezone(
            UTC8_TIMEZONE
        ).replace(tzinfo=None)

    return value


def calculate_points_expiry(
    activated_at: datetime,
) -> datetime:
    """
    计算积分批次从激活时刻起一个自然年后的到期时刻。

    到期判定统一使用 UTC+8。若激活日期为 2 月 29 日，
    而次年没有相同日期，则取次年 2 月最后一天的相同时刻。
    """
    activated_utc8 = _to_utc8_wall_time(
        activated_at,
        "激活时间",
    )
    expiry_year = activated_utc8.year + 1
    expiry_day = min(
        activated_utc8.day,
        monthrange(
            expiry_year,
            activated_utc8.month,
        )[1],
    )

    return activated_utc8.replace(
        year=expiry_year,
        day=expiry_day,
    )


def is_activation_within_deadline(
    activation_at: datetime,
    claim_deadline: datetime,
) -> bool:
    """
    判断一次激活是否仍位于所属批次的领取截止时间内。

    恰好等于截止时刻仍可激活；只有超过截止时刻才拒绝。
    激活后积分的到期时间应继续由 ``calculate_points_expiry``
    独立计算，不能被领取截止日截短。
    """
    activation_utc8 = _to_utc8_wall_time(
        activation_at,
        "激活时间",
    )
    deadline_utc8 = _to_utc8_wall_time(
        claim_deadline,
        "激活截止时间",
    )

    return activation_utc8 <= deadline_utc8
