"""微信小程序 v1 API 路由骨架。"""

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from pydantic import BaseModel

from ..database import SessionLocal
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
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=50),
    db=Depends(public_catalog_session),
):
    return list_public_products(db, category_slug=category, page=page, page_size=page_size)


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
