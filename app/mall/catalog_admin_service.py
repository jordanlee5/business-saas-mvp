"""商城商品目录后台的只读展示模型。"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from sqlalchemy import and_, func, or_

from .domain import ProductMediaRole, ProductStatus


CATALOG_SECTION_PRODUCTS = "products"
CATALOG_SECTION_CATEGORIES = "categories"
CATALOG_SECTION_SUPPLIERS = "suppliers"
VALID_CATALOG_SECTIONS = frozenset(
    {
        CATALOG_SECTION_PRODUCTS,
        CATALOG_SECTION_CATEGORIES,
        CATALOG_SECTION_SUPPLIERS,
    }
)


PRODUCT_STATUS_LABELS = {
    ProductStatus.DRAFT.value: "草稿",
    ProductStatus.PUBLISHED.value: "已上架",
    ProductStatus.UNPUBLISHED.value: "已下架",
}

PRODUCT_MEDIA_ROLE_LABELS = {
    ProductMediaRole.MAIN.value: "主图",
    ProductMediaRole.CAROUSEL.value: "轮播图",
    ProductMediaRole.DETAIL.value: "详情图",
}
PRODUCT_MEDIA_ROLE_ORDER = {
    ProductMediaRole.MAIN.value: 0,
    ProductMediaRole.CAROUSEL.value: 1,
    ProductMediaRole.DETAIL.value: 2,
}


@dataclass(frozen=True)
class CatalogCategoryItem:
    id: int
    name: str
    slug: str
    description: str | None
    sort_order: int
    is_active: bool


@dataclass(frozen=True)
class CatalogSupplierItem:
    id: int
    supplier_public_id: str
    name: str
    contact_name: str | None
    contact_phone: str | None
    remark: str | None
    is_active: bool


@dataclass(frozen=True)
class CatalogSkuItem:
    id: int
    product_id: int
    supplier_id: int
    supplier_name: str
    sku_code: str
    name: str
    supplier_sku_code: str | None
    points_price: Decimal
    cost_price: Decimal
    low_stock_threshold: int
    is_active: bool
    sort_order: int


@dataclass(frozen=True)
class CatalogMediaItem:
    id: int
    product_id: int
    media_role: str
    role_label: str
    image_path: str
    alt_text: str | None
    sort_order: int
    is_active: bool


@dataclass(frozen=True)
class CatalogProductItem:
    id: int
    product_public_id: str
    category_id: int
    category_name: str
    name: str
    subtitle: str | None
    description: str | None
    status: str
    status_label: str
    sort_order: int
    published_at: datetime | None
    skus: tuple[CatalogSkuItem, ...]
    media: tuple[CatalogMediaItem, ...]


@dataclass(frozen=True)
class CatalogAdminSnapshot:
    categories: tuple[CatalogCategoryItem, ...]
    active_categories: tuple[CatalogCategoryItem, ...]
    suppliers: tuple[CatalogSupplierItem, ...]
    active_suppliers: tuple[CatalogSupplierItem, ...]
    products: tuple[CatalogProductItem, ...]
    sku_count: int
    media_count: int
    product_count: int


def normalize_catalog_section(value) -> str:
    normalized = str(value or "").strip().lower()
    if normalized not in VALID_CATALOG_SECTIONS:
        raise ValueError("商品目录页面分区无效")
    return normalized


CATALOG_PAGE_SIZE = 25


def catalog_listing(db, keyword, page):
    from ..models import InventoryBalance, Product, ProductMedia, ProductSku, Supplier

    query = (db.query(Product.id.label("product_id"), Product.name.label("product_name"),
                      Product.status, ProductSku.id.label("sku_id"), ProductSku.sku_code,
                      ProductSku.name.label("sku_name"), ProductSku.is_active, ProductSku.cost_price,
                      ProductSku.points_price, Supplier.name.label("supplier_name"),
                      func.coalesce(InventoryBalance.on_hand_quantity - InventoryBalance.reserved_quantity, 0)
                      .label("available"), ProductMedia.image_path)
             .select_from(Product)
             .outerjoin(ProductSku, ProductSku.product_id == Product.id)
             .outerjoin(Supplier, Supplier.id == ProductSku.supplier_id)
             .outerjoin(InventoryBalance, InventoryBalance.sku_id == ProductSku.id)
             .outerjoin(ProductMedia, and_(ProductMedia.product_id == Product.id,
                                           ProductMedia.media_role == "MAIN", ProductMedia.is_active.is_(True))))
    if keyword:
        fields = (Product.name, Product.product_public_id, ProductSku.sku_code, ProductSku.name,
                  ProductSku.supplier_sku_code, Supplier.name)
        query = query.filter(or_(*(column.icontains(keyword, autoescape=True) for column in fields)))
    total = query.count()
    pages = max(1, (total + CATALOG_PAGE_SIZE - 1) // CATALOG_PAGE_SIZE)
    page = min(max(page, 1), pages)
    rows = (query.order_by(Product.sort_order, Product.id, ProductSku.sort_order, ProductSku.id)
            .offset((page - 1) * CATALOG_PAGE_SIZE).limit(CATALOG_PAGE_SIZE).all())
    return dict(rows=rows, total=total, page=page, pages=pages)


def get_catalog_admin_snapshot(db, *, include_products=True, product_id=None, sku_id=None) -> CatalogAdminSnapshot:
    """读取后台目录快照，不刷新、不修复，也不写入任何数据。"""
    from ..models import (
        Product,
        ProductCategory,
        ProductMedia,
        ProductSku,
        Supplier,
    )

    with db.no_autoflush:
        category_rows = (
            db.query(ProductCategory)
            .order_by(ProductCategory.sort_order.asc(), ProductCategory.id.asc())
            .all()
        )
        supplier_rows = (
            db.query(Supplier)
            .order_by(Supplier.name.asc(), Supplier.id.asc())
            .all()
        )
        products = db.query(Product)
        skus = db.query(ProductSku)
        media = db.query(ProductMedia)
        if product_id is not None:
            products = products.filter_by(id=product_id)
            skus = skus.filter_by(product_id=product_id)
            media = media.filter_by(product_id=product_id)
        if sku_id is not None:
            skus = skus.filter_by(id=sku_id)
        product_rows = (
            products
            .order_by(Product.sort_order.asc(), Product.id.asc())
            .all()
        ) if include_products else []
        sku_rows = (
            skus
            .order_by(
                ProductSku.product_id.asc(),
                ProductSku.sort_order.asc(),
                ProductSku.id.asc(),
            )
            .all()
        ) if include_products else []
        media_rows = (
            media
            .order_by(
                ProductMedia.product_id.asc(),
                ProductMedia.media_role.asc(),
                ProductMedia.sort_order.asc(),
                ProductMedia.id.asc(),
            )
            .all()
        ) if include_products else []

    categories = tuple(
        CatalogCategoryItem(
            id=row.id,
            name=row.name,
            slug=row.slug,
            description=row.description,
            sort_order=row.sort_order,
            is_active=row.is_active is True,
        )
        for row in category_rows
    )
    suppliers = tuple(
        CatalogSupplierItem(
            id=row.id,
            supplier_public_id=row.supplier_public_id,
            name=row.name,
            contact_name=row.contact_name,
            contact_phone=row.contact_phone,
            remark=row.remark,
            is_active=row.is_active is True,
        )
        for row in supplier_rows
    )
    category_names = {item.id: item.name for item in categories}
    supplier_names = {item.id: item.name for item in suppliers}
    skus_by_product: dict[int, list[CatalogSkuItem]] = {}
    for row in sku_rows:
        item = CatalogSkuItem(
            id=row.id,
            product_id=row.product_id,
            supplier_id=row.supplier_id,
            supplier_name=supplier_names.get(row.supplier_id, "未知供应商"),
            sku_code=row.sku_code,
            name=row.name,
            supplier_sku_code=row.supplier_sku_code,
            points_price=row.points_price,
            cost_price=row.cost_price,
            low_stock_threshold=row.low_stock_threshold,
            is_active=row.is_active is True,
            sort_order=row.sort_order,
        )
        skus_by_product.setdefault(row.product_id, []).append(item)
    media_by_product: dict[int, list[CatalogMediaItem]] = {}
    for row in media_rows:
        item = CatalogMediaItem(
            id=row.id,
            product_id=row.product_id,
            media_role=row.media_role,
            role_label=PRODUCT_MEDIA_ROLE_LABELS.get(
                row.media_role,
                "异常用途",
            ),
            image_path=row.image_path,
            alt_text=row.alt_text,
            sort_order=row.sort_order,
            is_active=row.is_active is True,
        )
        media_by_product.setdefault(row.product_id, []).append(item)
    products = tuple(
        CatalogProductItem(
            id=row.id,
            product_public_id=row.product_public_id,
            category_id=row.category_id,
            category_name=category_names.get(row.category_id, "未知分类"),
            name=row.name,
            subtitle=row.subtitle,
            description=row.description,
            status=row.status,
            status_label=PRODUCT_STATUS_LABELS.get(row.status, "异常状态"),
            sort_order=row.sort_order,
            published_at=row.published_at,
            skus=tuple(skus_by_product.get(row.id, ())),
            media=tuple(
                sorted(
                    media_by_product.get(row.id, ()),
                    key=lambda item: (
                        PRODUCT_MEDIA_ROLE_ORDER.get(item.media_role, 99),
                        item.sort_order,
                        item.id,
                    ),
                )
            ),
        )
        for row in product_rows
    )
    if include_products and product_id is None and sku_id is None:
        totals = len(products), len(sku_rows), len(media_rows)
    else:
        totals = db.query(*(db.query(func.count(model.id)).scalar_subquery()
                            for model in (Product, ProductSku, ProductMedia))).one()
    return CatalogAdminSnapshot(
        categories=categories,
        active_categories=tuple(item for item in categories if item.is_active),
        suppliers=suppliers,
        active_suppliers=tuple(item for item in suppliers if item.is_active),
        products=products,
        product_count=totals[0], sku_count=totals[1], media_count=totals[2],
    )
