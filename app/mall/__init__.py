"""积分商城领域规则包。"""

from .domain import (
    BusinessChannel,
    POINTS_QUANTUM,
    VALID_BUSINESS_CHANNELS,
    calculate_points_expiry,
    is_activation_within_deadline,
    normalize_business_channel,
    normalize_points,
)

__all__ = [
    "BusinessChannel",
    "POINTS_QUANTUM",
    "VALID_BUSINESS_CHANNELS",
    "calculate_points_expiry",
    "is_activation_within_deadline",
    "normalize_business_channel",
    "normalize_points",
]
