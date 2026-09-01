"""微信小程序 v1 API 路由骨架。"""

from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel


MINIPROGRAM_API_PREFIX = "/api/miniprogram/v1"


class MiniprogramApiStatus(BaseModel):
    """小程序 API 的稳定状态响应。"""

    status: Literal["ok"] = "ok"
    api_version: Literal["v1"] = "v1"
    service: Literal["mall-miniprogram-api"] = (
        "mall-miniprogram-api"
    )


miniprogram_v1_router = APIRouter(
    prefix=MINIPROGRAM_API_PREFIX,
    tags=["miniprogram-v1"],
)


@miniprogram_v1_router.get(
    "/status",
    response_model=MiniprogramApiStatus,
    operation_id="get_miniprogram_api_status",
    summary="读取小程序 API 状态",
)
def get_miniprogram_api_status() -> MiniprogramApiStatus:
    """返回不包含业务数据的公开 API 版本状态。"""
    return MiniprogramApiStatus()
