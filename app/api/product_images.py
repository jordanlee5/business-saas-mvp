"""Authenticated original/thumbnail delivery; no public static product mount."""
import re
from pathlib import Path

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import FileResponse

from .catalog_access import CatalogAccessRoute, PRIVATE_HEADERS, require_catalog_member
from .miniprogram_v1 import public_catalog_session, release_owned_session
from ..admin_permissions import can_manage_mall_catalog
from ..mall import product_media_storage as storage
from ..models import Product, ProductCategory, ProductMedia, ProductSku, Supplier

router = APIRouter(route_class=CatalogAccessRoute)


def image_file(db, image_path, *, admin=False):
    parts = image_path.split("/")
    if (len(parts) != 2 or not storage.PRODUCT_PUBLIC_ID_PATTERN.fullmatch(parts[0])
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,180}\.(webp|png|jpg|jpeg)", parts[1])
            or ".." in parts[1]):
        raise HTTPException(404, "图片不存在")
    filename = parts[1]
    if filename.endswith(".thumb-640.webp"):
        filename = filename.removesuffix(".thumb-640.webp") + ".webp"
        if not storage.GENERATED_IMAGE_PATTERN.fullmatch(filename):
            raise HTTPException(404, "图片不存在")
    original_url = f"{storage.PRODUCT_IMAGE_URL_PREFIX}/{parts[0]}/{filename}"
    query = db.query(ProductMedia.id).join(Product, Product.id == ProductMedia.product_id).filter(
        ProductMedia.image_path == original_url, Product.product_public_id == parts[0])
    if not admin:
        valid_sku = db.query(ProductSku.id).join(Supplier, Supplier.id == ProductSku.supplier_id).filter(
            ProductSku.product_id == Product.id, ProductSku.is_active.is_(True), Supplier.is_active.is_(True)).exists()
        query = query.join(ProductCategory, ProductCategory.id == Product.category_id).filter(
            ProductMedia.is_active.is_(True), Product.status == "PUBLISHED", ProductCategory.is_active.is_(True), valid_sku)
    if query.first() is None:
        raise HTTPException(404, "图片不存在")
    root = Path(storage.PRODUCT_IMAGE_STORAGE_ROOT).resolve()
    target = (root / image_path).resolve()
    if not target.is_relative_to(root) or not target.is_file():
        raise HTTPException(404, "图片不存在")
    return target


@router.api_route("/uploads/mall_products/{image_path:path}", methods=["GET", "HEAD"], name="protected_product_image")
@release_owned_session
def product_image(request: Request, image_path: str, authorization: str | None = Header(None),
                  db=Depends(public_catalog_session)):
    admin = False
    if authorization is not None:
        # A bad member token must not silently fall back to an admin cookie.
        require_catalog_member(authorization, db)
    else:
        from ..main import get_current_user
        user = get_current_user(request)
        admin = bool(user and user.is_active and not user.must_change_password and can_manage_mall_catalog(user))
        if not admin:
            raise HTTPException(401, "请先登录并核验访问权限")
    target = image_file(db, image_path, admin=admin)
    return FileResponse(target, headers=PRIVATE_HEADERS)
