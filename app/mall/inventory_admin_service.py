"""商城库存后台的只读分页与流水展示模型。"""

from dataclasses import dataclass
from datetime import datetime

from .domain import InventoryMovementType, InventoryStockStatus
from .inventory_service import audit_inventory_balance


INVENTORY_STATUS_ALL = "ALL"
VALID_INVENTORY_STATUS_FILTERS = frozenset(
    {
        INVENTORY_STATUS_ALL,
        *(status.value for status in InventoryStockStatus),
    }
)

INVENTORY_STATUS_LABELS = {
    InventoryStockStatus.IN_STOCK.value: "库存正常",
    InventoryStockStatus.LOW_STOCK.value: "低库存",
    InventoryStockStatus.OUT_OF_STOCK.value: "无库存",
}

INVENTORY_MOVEMENT_TYPE_LABELS = {
    InventoryMovementType.RECEIPT.value: "入库",
    InventoryMovementType.ADJUSTMENT.value: "人工调整",
    InventoryMovementType.RESERVE.value: "订单预占",
    InventoryMovementType.RELEASE.value: "订单释放",
    InventoryMovementType.OUTBOUND.value: "订单出库",
    InventoryMovementType.RETURN.value: "订单退回",
}


@dataclass(frozen=True)
class InventorySkuItem:
    sku_id: int
    sku_code: str
    sku_name: str
    sku_is_active: bool
    product_id: int
    product_public_id: str
    product_name: str
    product_status: str
    supplier_id: int
    supplier_public_id: str
    supplier_name: str
    supplier_is_active: bool
    on_hand_quantity: int
    reserved_quantity: int
    available_quantity: int
    low_stock_threshold: int
    stock_status: str
    stock_status_label: str
    balance_version: int
    movement_count: int
    is_consistent: bool


@dataclass(frozen=True)
class InventorySkuOption:
    sku_id: int
    label: str


@dataclass(frozen=True)
class InventoryMovementItem:
    movement_id: int
    movement_public_id: str
    sku_id: int
    sku_code: str
    product_name: str
    movement_type: str
    movement_type_label: str
    quantity_delta: int
    quantity_before: int
    quantity_after: int
    balance_version: int
    reason: str
    actor_username: str
    created_at: datetime


@dataclass(frozen=True)
class InventorySummary:
    sku_count: int
    in_stock_count: int
    low_stock_count: int
    out_of_stock_count: int
    inconsistent_count: int
    movement_count: int


@dataclass(frozen=True)
class InventoryMovementPage:
    items: tuple[InventoryMovementItem, ...]
    sku_id: int
    page: int
    page_size: int
    total: int
    total_pages: int


@dataclass(frozen=True)
class InventoryAdminPage:
    items: tuple[InventorySkuItem, ...]
    sku_options: tuple[InventorySkuOption, ...]
    summary: InventorySummary
    movements: InventoryMovementPage
    keyword: str
    stock_status: str
    page: int
    page_size: int
    total: int
    total_pages: int


def _integer(value, field_name, minimum, maximum=None):
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < minimum
        or (maximum is not None and value > maximum)
    ):
        raise ValueError(f"{field_name}超出允许范围")


def _normalize_keyword(value) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError("库存查询关键词无效")
    normalized = " ".join(value.split())
    if len(normalized) > 100:
        raise ValueError("库存查询关键词不能超过100个字符")
    return normalized


def normalize_inventory_status_filter(value) -> str:
    if not isinstance(value, str):
        raise ValueError("库存状态筛选无效")
    normalized = value.strip().upper()
    if normalized not in VALID_INVENTORY_STATUS_FILTERS:
        raise ValueError("库存状态筛选无效")
    return normalized


def _matches_keyword(item: InventorySkuItem, keyword: str) -> bool:
    if not keyword:
        return True
    needle = keyword.casefold()
    values = (
        item.sku_code,
        item.sku_name,
        item.product_public_id,
        item.product_name,
        item.supplier_public_id,
        item.supplier_name,
    )
    return any(needle in str(value or "").casefold() for value in values)


def list_inventory_admin(
    db,
    *,
    keyword="",
    stock_status=INVENTORY_STATUS_ALL,
    page=1,
    page_size=10,
    movement_sku_id=0,
    movement_page=1,
    movement_page_size=20,
) -> InventoryAdminPage:
    """读取 SKU 库存和不可变流水；不修复余额、不写入数据。"""
    from ..models import (
        InventoryMovement,
        Product,
        ProductSku,
        Supplier,
        User,
    )

    _integer(page, "页码", 1)
    _integer(page_size, "每页数量", 1, 50)
    _integer(movement_sku_id, "流水 SKU 编号", 0)
    _integer(movement_page, "流水页码", 1)
    _integer(movement_page_size, "流水每页数量", 1, 50)
    normalized_keyword = _normalize_keyword(keyword)
    normalized_status = normalize_inventory_status_filter(stock_status)

    with db.no_autoflush:
        sku_rows = (
            db.query(ProductSku, Product, Supplier)
            .join(Product, Product.id == ProductSku.product_id)
            .join(Supplier, Supplier.id == ProductSku.supplier_id)
            .order_by(
                Product.sort_order.asc(),
                Product.id.asc(),
                ProductSku.sort_order.asc(),
                ProductSku.id.asc(),
            )
            .all()
        )

        all_items = []
        for sku, product, supplier in sku_rows:
            audit = audit_inventory_balance(db, sku_id=sku.id)
            status_value = audit.snapshot.stock_status.value
            all_items.append(InventorySkuItem(
                sku_id=sku.id,
                sku_code=sku.sku_code,
                sku_name=sku.name,
                sku_is_active=sku.is_active is True,
                product_id=product.id,
                product_public_id=product.product_public_id,
                product_name=product.name,
                product_status=product.status,
                supplier_id=supplier.id,
                supplier_public_id=supplier.supplier_public_id,
                supplier_name=supplier.name,
                supplier_is_active=supplier.is_active is True,
                on_hand_quantity=audit.snapshot.on_hand_quantity,
                reserved_quantity=audit.snapshot.reserved_quantity,
                available_quantity=audit.snapshot.available_quantity,
                low_stock_threshold=audit.snapshot.low_stock_threshold,
                stock_status=status_value,
                stock_status_label=INVENTORY_STATUS_LABELS[status_value],
                balance_version=audit.snapshot.balance_version,
                movement_count=audit.movement_count,
                is_consistent=audit.is_consistent,
            ))

        all_items_tuple = tuple(all_items)
        filtered_items = tuple(
            item
            for item in all_items_tuple
            if _matches_keyword(item, normalized_keyword)
            and (
                normalized_status == INVENTORY_STATUS_ALL
                or item.stock_status == normalized_status
            )
        )
        total = len(filtered_items)
        total_pages = max(1, (total + page_size - 1) // page_size)
        effective_page = min(page, total_pages)
        start = (effective_page - 1) * page_size
        page_items = filtered_items[start:start + page_size]

        sku_options = tuple(
            InventorySkuOption(
                sku_id=item.sku_id,
                label=f"{item.sku_code} · {item.product_name} / {item.sku_name}",
            )
            for item in all_items_tuple
        )
        valid_sku_ids = {option.sku_id for option in sku_options}
        if movement_sku_id and movement_sku_id not in valid_sku_ids:
            raise ValueError("流水筛选的商品 SKU 不存在")

        movement_query = (
            db.query(
                InventoryMovement,
                ProductSku.sku_code,
                Product.name,
                User.username,
            )
            .join(ProductSku, ProductSku.id == InventoryMovement.sku_id)
            .join(Product, Product.id == ProductSku.product_id)
            .outerjoin(User, User.id == InventoryMovement.actor_admin_id)
        )
        if movement_sku_id:
            movement_query = movement_query.filter(
                InventoryMovement.sku_id == movement_sku_id
            )
        movement_total = movement_query.count()
        movement_total_pages = max(
            1,
            (movement_total + movement_page_size - 1)
            // movement_page_size,
        )
        effective_movement_page = min(
            movement_page,
            movement_total_pages,
        )
        movement_rows = (
            movement_query
            .order_by(
                InventoryMovement.created_at.desc(),
                InventoryMovement.id.desc(),
            )
            .offset(
                (effective_movement_page - 1) * movement_page_size
            )
            .limit(movement_page_size)
            .all()
        )
        movement_items = tuple(
            InventoryMovementItem(
                movement_id=movement.id,
                movement_public_id=movement.movement_public_id,
                sku_id=movement.sku_id,
                sku_code=sku_code,
                product_name=product_name,
                movement_type=movement.movement_type,
                movement_type_label=INVENTORY_MOVEMENT_TYPE_LABELS.get(
                    movement.movement_type,
                    "异常类型",
                ),
                quantity_delta=movement.quantity_delta,
                quantity_before=movement.quantity_before,
                quantity_after=movement.quantity_after,
                balance_version=movement.balance_version,
                reason=movement.reason,
                actor_username=actor_username or "会员订单",
                created_at=movement.created_at,
            )
            for movement, sku_code, product_name, actor_username
            in movement_rows
        )
        all_movement_count = db.query(InventoryMovement.id).count()

    summary = InventorySummary(
        sku_count=len(all_items_tuple),
        in_stock_count=sum(
            item.stock_status == InventoryStockStatus.IN_STOCK.value
            for item in all_items_tuple
        ),
        low_stock_count=sum(
            item.stock_status == InventoryStockStatus.LOW_STOCK.value
            for item in all_items_tuple
        ),
        out_of_stock_count=sum(
            item.stock_status == InventoryStockStatus.OUT_OF_STOCK.value
            for item in all_items_tuple
        ),
        inconsistent_count=sum(
            not item.is_consistent for item in all_items_tuple
        ),
        movement_count=all_movement_count,
    )
    return InventoryAdminPage(
        items=page_items,
        sku_options=sku_options,
        summary=summary,
        movements=InventoryMovementPage(
            items=movement_items,
            sku_id=movement_sku_id,
            page=effective_movement_page,
            page_size=movement_page_size,
            total=movement_total,
            total_pages=movement_total_pages,
        ),
        keyword=normalized_keyword,
        stock_status=normalized_status,
        page=effective_page,
        page_size=page_size,
        total=total,
        total_pages=total_pages,
    )
