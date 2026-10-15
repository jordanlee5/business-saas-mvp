"""微信小程序 v1 API 路由骨架。"""

import os
from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Query, Response
from pydantic import BaseModel
from sqlalchemy import func

from ..database import SessionLocal
from ..models import Member, MemberWechatBinding, PointsAccount, PointsGrant
from ..time_utils import format_utc8, utc8_now
from ..mall.points_ledger_service import audit_points_account_balance
from ..mall.domain import _to_utc8_wall_time
from ..mall.wechat_identity_service import (
    WechatCodeError, WechatConfigurationError, WechatExchangeUnavailable,
    exchange_wechat_code,
)
from ..mall.miniprogram_session_service import (
    InvalidSession, SessionConfigurationError, assert_session_configured,
    issue_session, session_matches_binding, verify_session,
)
from ..mall.public_catalog_service import (
    get_public_product_detail, list_public_categories, list_public_products,
)


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


def public_catalog_session():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.rollback()
        db.close()


class WechatLoginRequest(BaseModel):
    code: str


class MemberSessionResponse(BaseModel):
    token: str
    token_type: Literal["Bearer"] = "Bearer"
    expires_in: int


class CurrentMemberResponse(BaseModel):
    member_public_id: str


class MemberPointsSummaryResponse(BaseModel):
    available_points: str
    reserved_points: str
    grant_count: int
    nearest_expires_at: str | None


@miniprogram_v1_router.post(
    "/auth/wechat-session", response_model=MemberSessionResponse,
    operation_id="create_miniprogram_member_session",
    summary="使用微信登录 code 创建已绑定会员短期会话",
)
def create_member_session(
    body: WechatLoginRequest, response: Response,
    db=Depends(public_catalog_session),
):
    response.headers["Cache-Control"] = "no-store"
    try:
        assert_session_configured()
    except SessionConfigurationError:
        raise HTTPException(status_code=503, detail="会员会话暂不可用") from None
    try:
        identity = exchange_wechat_code(body.code)
    except WechatConfigurationError:
        raise HTTPException(status_code=503, detail="微信登录暂不可用") from None
    except WechatCodeError:
        raise HTTPException(status_code=401, detail="微信登录凭证无效") from None
    except WechatExchangeUnavailable:
        raise HTTPException(status_code=502, detail="微信登录暂不可用") from None

    binding = db.query(MemberWechatBinding).filter(
        MemberWechatBinding.wechat_app_id == identity.app_id,
        MemberWechatBinding.openid == identity.openid,
    ).one_or_none()
    if binding is None:
        raise HTTPException(status_code=403, detail="会员身份尚未激活")
    member = db.query(Member).filter(Member.id == binding.member_id).one_or_none()
    if member is None or not member.is_active:
        raise HTTPException(status_code=403, detail="会员身份尚未激活")
    try:
        token, ttl = issue_session(binding.id, identity.app_id, identity.openid)
    except SessionConfigurationError:
        raise HTTPException(status_code=503, detail="会员会话暂不可用") from None
    return MemberSessionResponse(token=token, expires_in=ttl)


@miniprogram_v1_router.get(
    "/auth/me", response_model=CurrentMemberResponse,
    operation_id="get_miniprogram_current_member",
    summary="读取当前有效会话所绑定的会员身份",
)
def get_current_member(
    response: Response, authorization: str | None = Header(None),
    db=Depends(public_catalog_session),
):
    response.headers["Cache-Control"] = "no-store"
    member = _require_current_member(authorization, db)
    return CurrentMemberResponse(member_public_id=member.member_public_id)


def _require_current_member(authorization, db) -> Member:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="会员会话无效")
    try:
        claims = verify_session(authorization[7:])
    except SessionConfigurationError:
        raise HTTPException(status_code=503, detail="会员会话暂不可用") from None
    except InvalidSession:
        raise HTTPException(status_code=401, detail="会员会话无效") from None
    if claims.app_id != os.environ.get("WECHAT_MINIPROGRAM_APP_ID", "").strip():
        raise HTTPException(status_code=401, detail="会员会话无效")
    binding = db.query(MemberWechatBinding).filter(
        MemberWechatBinding.id == claims.binding_id,
        MemberWechatBinding.wechat_app_id == claims.app_id,
    ).one_or_none()
    if binding is None:
        raise HTTPException(status_code=401, detail="会员会话无效")
    try:
        matches = session_matches_binding(claims, binding.wechat_app_id, binding.openid)
    except SessionConfigurationError:
        raise HTTPException(status_code=503, detail="会员会话暂不可用") from None
    if not matches:
        raise HTTPException(status_code=401, detail="会员会话无效")
    member = db.query(Member).filter(Member.id == binding.member_id).one_or_none()
    if member is None or not member.is_active:
        raise HTTPException(status_code=401, detail="会员会话无效")
    return member


@miniprogram_v1_router.get(
    "/points/summary", response_model=MemberPointsSummaryResponse,
    operation_id="get_miniprogram_member_points_summary",
    summary="读取当前会员积分汇总",
)
def get_member_points_summary(
    response: Response, authorization: str | None = Header(None),
    db=Depends(public_catalog_session),
):
    response.headers["Cache-Control"] = "no-store"
    member = _require_current_member(authorization, db)
    with db.no_autoflush:
        account = db.query(PointsAccount.id).filter(
            PointsAccount.member_id == member.id,
        ).one_or_none()
        if account is None:
            raise HTTPException(status_code=503, detail="积分数据暂不可用")
        try:
            audit = audit_points_account_balance(db, account_id=account.id)
        except ValueError:
            raise HTTPException(status_code=503, detail="积分数据暂不可用") from None
        if not audit.is_consistent:
            raise HTTPException(status_code=503, detail="积分数据暂不可用")
        now = utc8_now()
        grant_count = db.query(func.count(PointsGrant.id)).filter(
            PointsGrant.account_id == account.id,
        ).scalar()
        active_expiry = [
            _to_utc8_wall_time(grant.expires_at, "积分到期时间")
            for grant in db.query(
                PointsGrant.expires_at,
            ).filter(
                PointsGrant.account_id == account.id,
                PointsGrant.status == "ACTIVE",
                PointsGrant.available_points > 0,
            ).all()
            if grant.expires_at is not None and _to_utc8_wall_time(
                grant.expires_at, "积分到期时间",
            ) > now
        ]
        nearest = min(active_expiry) if active_expiry else None
        return MemberPointsSummaryResponse(
            available_points=f"{audit.cached_available_points:.2f}",
            reserved_points=f"{audit.cached_reserved_points:.2f}",
            grant_count=grant_count,
            nearest_expires_at=(
                format_utc8(nearest, "%Y-%m-%dT%H:%M:%S+08:00")
                if nearest is not None else None
            ),
        )


class PublicCategory(BaseModel):
    name: str
    slug: str


class PublicProduct(BaseModel):
    product_public_id: str
    name: str
    subtitle: str | None
    category_slug: str
    min_points_price: str
    in_stock: bool
    main_image_url: str | None


class PublicProductPage(BaseModel):
    items: list[PublicProduct]
    page: int
    page_size: int
    total: int


class PublicProductSku(BaseModel):
    sku_code: str
    name: str
    points_price: str
    in_stock: bool
    stock_status: Literal["IN_STOCK", "LOW_STOCK", "OUT_OF_STOCK"]


class PublicProductImage(BaseModel):
    role: Literal["main", "carousel", "detail"]
    url: str
    alt_text: str | None


class PublicProductDetail(PublicProduct):
    description: str | None
    images: list[PublicProductImage]
    skus: list[PublicProductSku]


@miniprogram_v1_router.get(
    "/categories", response_model=list[PublicCategory],
    operation_id="list_miniprogram_categories", summary="读取上架商品分类",
)
def get_public_categories(db=Depends(public_catalog_session)):
    return list_public_categories(db)


@miniprogram_v1_router.get(
    "/products", response_model=PublicProductPage,
    operation_id="list_miniprogram_products", summary="读取上架商品列表",
)
def get_public_products(
    category: str | None = Query(None, min_length=1, max_length=80),
    q: str | None = Query(None, min_length=1, max_length=80, pattern=r"\S"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=50),
    db=Depends(public_catalog_session),
):
    return list_public_products(db, category_slug=category, search_term=q,
                                page=page, page_size=page_size)


@miniprogram_v1_router.get(
    "/products/{product_public_id}", response_model=PublicProductDetail,
    operation_id="get_miniprogram_product_detail", summary="读取上架商品详情",
)
def get_public_product(
    product_public_id: str = Path(..., min_length=1, max_length=32,
                                  pattern=r"^[A-Z0-9-]+$"),
    db=Depends(public_catalog_session),
):
    detail = get_public_product_detail(db, product_public_id=product_public_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="商品不存在")
    return detail
