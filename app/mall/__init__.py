"""积分商城领域规则包。"""

from .audit import (
    MallAuditActionType,
    VALID_MALL_AUDIT_ACTION_TYPES,
    normalize_mall_audit_action_type,
)
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
    "MallAuditActionType",
    "POINTS_QUANTUM",
    "VALID_MALL_AUDIT_ACTION_TYPES",
    "VALID_BUSINESS_CHANNELS",
    "calculate_points_expiry",
    "is_activation_within_deadline",
    "normalize_business_channel",
    "normalize_mall_audit_action_type",
    "normalize_points",
]
