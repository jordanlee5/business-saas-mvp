"""商城商品目录后台的只读展示模型。"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from .domain import ProductStatus


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


@dataclass(frozen=True)
class CatalogAdminSnapshot:
    categories: tuple[CatalogCategoryItem, ...]
    active_categories: tuple[CatalogCategoryItem, ...]
    suppliers: tuple[CatalogSupplierItem, ...]
    active_suppliers: tuple[CatalogSupplierItem, ...]
    products: tuple[CatalogProductItem, ...]
    sku_count: int


def normalize_catalog_section(value) -> str:
    normalized = str(value or "").strip().lower()
    if normalized not in VALID_CATALOG_SECTIONS:
        raise ValueError("商品目录页面分区无效")
    return normalized


def get_catalog_admin_snapshot(db) -> CatalogAdminSnapshot:
    """读取后台目录快照，不刷新、不修复，也不写入任何数据。"""
    from ..models import Product, ProductCategory, ProductSku, Supplier

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
        product_rows = (
            db.query(Product)
            .order_by(Product.sort_order.asc(), Product.id.asc())
            .all()
        )
        sku_rows = (
            db.query(ProductSku)
            .order_by(
                ProductSku.product_id.asc(),
                ProductSku.sort_order.asc(),
                ProductSku.id.asc(),
            )
            .all()
        )

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
        )
        for row in product_rows
    )
    return CatalogAdminSnapshot(
        categories=categories,
        active_categories=tuple(item for item in categories if item.is_active),
        suppliers=suppliers,
        active_suppliers=tuple(item for item in suppliers if item.is_active),
        products=products,
        sku_count=len(sku_rows),
    )
