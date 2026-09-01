"""版本化 JSON API 路由入口。"""

from .miniprogram_v1 import (
    MINIPROGRAM_API_PREFIX,
    miniprogram_v1_router,
)

__all__ = [
    "MINIPROGRAM_API_PREFIX",
    "miniprogram_v1_router",
]
