"""版本化 JSON API 路由入口。"""

from .miniprogram_v1 import (
    MINIPROGRAM_API_PREFIX,
    miniprogram_v1_router,
)
from .member_addresses import address_router

miniprogram_v1_router.include_router(address_router)
from .member_orders import order_router
miniprogram_v1_router.include_router(order_router)

__all__ = [
    "MINIPROGRAM_API_PREFIX",
    "miniprogram_v1_router",
]
