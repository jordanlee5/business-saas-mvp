"""受权限和审计保护的商城商品目录写服务。"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
import re
import secrets

from sqlalchemy import or_

from ..time_utils import UTC8_TIMEZONE, utc8_now
from .audit import MallAuditActionType
from .domain import ProductStatus, normalize_points


CATALOG_PERMISSION_MESSAGE = "当前账号无权管理商城商品目录"
SUPPLIER_PERMISSION_MESSAGE = "当前账号无权管理商城供应商"
PUBLIC_ID_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
PUBLIC_ID_RANDOM_LENGTH = 12
PUBLIC_ID_GENERATION_ATTEMPTS = 16
SLUG_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_UNSET = object()


@dataclass(frozen=True)
class CatalogMutationResult:
    """一次目录写操作的实体、审计和实际变化信息。"""

    entity: object
    action_log: object | None
    changed: bool
    changed_fields: tuple[str, ...]


def _normalize_required_text(value, *, field_name, maximum_length):
    if value is None:
        raise ValueError(f"{field_name}不能为空")
    normalized = str(value).strip()
    if not normalized:
        raise ValueError(f"{field_name}不能为空")
    if len(normalized) > maximum_length:
        raise ValueError(f"{field_name}不能超过 {maximum_length} 个字符")
    return normalized


def _normalize_optional_text(value, *, field_name, maximum_length):
    if value is None:
        return None
    normalized = str(value).strip()
    if not normalized:
        return None
    if len(normalized) > maximum_length:
        raise ValueError(f"{field_name}不能超过 {maximum_length} 个字符")
    return normalized


def _normalize_slug(value):
    slug = _normalize_required_text(
        value,
        field_name="分类 slug",
        maximum_length=80,
    ).lower()
    if not SLUG_PATTERN.fullmatch(slug):
        raise ValueError("分类 slug 只能包含小写字母、数字和单个连字符")
    return slug


def _normalize_code(value, *, field_name, maximum_length):
    return _normalize_required_text(
        value,
        field_name=field_name,
        maximum_length=maximum_length,
    ).upper()


def _normalize_nonnegative_integer(value, *, field_name):
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field_name}必须是非负整数")
    if value < 0:
        raise ValueError(f"{field_name}必须是非负整数")
    return value


def _normalize_boolean(value, *, field_name):
    if not isinstance(value, bool):
        raise ValueError(f"{field_name}必须是布尔值")
    return value


def _normalize_price(value, *, field_name, allow_zero):
    price = normalize_points(value, field_name)
    if allow_zero:
        if price < Decimal("0.00"):
            raise ValueError(f"{field_name}不能为负数")
    elif price <= Decimal("0.00"):
        raise ValueError(f"{field_name}必须大于零")
    return price


def _database_time(db, value):
    if db.get_bind().dialect.name == "postgresql":
        return value.replace(tzinfo=UTC8_TIMEZONE)
    return value


def _current_time(db, value):
    current = utc8_now() if value is None else value
    if not isinstance(current, datetime):
        raise ValueError("目录操作时间无效")
    if current.tzinfo is not None and current.utcoffset() is not None:
        current = current.astimezone(UTC8_TIMEZONE).replace(tzinfo=None)
    return _database_time(db, current)


def _require_actor(db, *, actor_admin_id, action_type, permission_message):
    from ..admin_permissions import can_perform_mall_audit_action
    from ..models import User

    if (
        isinstance(actor_admin_id, bool)
        or not isinstance(actor_admin_id, int)
        or actor_admin_id <= 0
    ):
        raise PermissionError(permission_message)
    with db.no_autoflush:
        actor = (
            db.query(User)
            .filter(User.id == actor_admin_id)
            .with_for_update()
            .populate_existing()
            .one_or_none()
        )
    if (
        actor is None
        or actor.is_active is not True
        or not can_perform_mall_audit_action(actor, action_type)
    ):
        raise PermissionError(permission_message)
    return actor


def _lock_entity(db, model, entity_id, *, field_name):
    if (
        isinstance(entity_id, bool)
        or not isinstance(entity_id, int)
        or entity_id <= 0
    ):
        raise ValueError(f"{field_name}无效")
    with db.no_autoflush:
        entity = (
            db.query(model)
            .filter(model.id == entity_id)
            .with_for_update()
            .populate_existing()
            .one_or_none()
        )
    if entity is None:
        raise ValueError(f"{field_name}不存在")
    return entity


def _ensure_unique(
    db,
    model,
    *,
    checks,
    exclude_id=None,
):
    conditions = [column == value for column, value, _message in checks]
    with db.no_autoflush:
        query = db.query(model).filter(or_(*conditions))
        if exclude_id is not None:
            query = query.filter(model.id != exclude_id)
        existing = query.first()
    if existing is None:
        return
    for column, value, message in checks:
        if getattr(existing, column.key) == value:
            raise ValueError(message)
    raise ValueError("目录唯一字段冲突")


def _generate_public_id(db, model, column, prefix):
    for _attempt in range(PUBLIC_ID_GENERATION_ATTEMPTS):
        random_part = "".join(
            secrets.choice(PUBLIC_ID_ALPHABET)
            for _ in range(PUBLIC_ID_RANDOM_LENGTH)
        )
        value = f"{prefix}-{random_part}"
        with db.no_autoflush:
            exists = db.query(model.id).filter(column == value).first()
        if exists is None:
            return value
    raise RuntimeError("无法生成唯一的目录公开编号")


def _record_audit(
    db,
    *,
    actor,
    action_type,
    target_type,
    target_id,
    description,
    operation_time,
):
    from ..models import AdminActionLog

    log = AdminActionLog(
        admin_id=actor.id,
        action_type=action_type.value,
        target_type=target_type,
        target_id=target_id,
        description=description,
        created_at=operation_time,
    )
    db.add(log)
    db.flush()
    return log


def _set_if_changed(entity, field_name, value, changed_fields):
    if getattr(entity, field_name) != value:
        setattr(entity, field_name, value)
        changed_fields.append(field_name)


def _finish_update(
    db,
    *,
    entity,
    actor,
    action_type,
    target_type,
    description_prefix,
    changed_fields,
    operation_time,
):
    if not changed_fields:
        return CatalogMutationResult(
            entity=entity,
            action_log=None,
            changed=False,
            changed_fields=(),
        )
    entity.updated_at = operation_time
    db.flush()
    action_log = _record_audit(
        db,
        actor=actor,
        action_type=action_type,
        target_type=target_type,
        target_id=entity.id,
        description=(
            f"{description_prefix}；变更字段：{', '.join(changed_fields)}"
        ),
        operation_time=operation_time,
    )
    return CatalogMutationResult(
        entity=entity,
        action_log=action_log,
        changed=True,
        changed_fields=tuple(changed_fields),
    )


def create_product_category(
    db,
    *,
    actor_admin_id: int,
    name,
    slug,
    description=None,
    sort_order=0,
    is_active=True,
    now=None,
):
    """创建商品分类；调用方负责提交或回滚。"""
    from ..models import ProductCategory

    actor = _require_actor(
        db,
        actor_admin_id=actor_admin_id,
        action_type=MallAuditActionType.CATEGORY_CREATE,
        permission_message=CATALOG_PERMISSION_MESSAGE,
    )
    normalized_name = _normalize_required_text(
        name, field_name="分类名称", maximum_length=120
    )
    normalized_slug = _normalize_slug(slug)
    normalized_description = _normalize_optional_text(
        description, field_name="分类说明", maximum_length=5000
    )
    normalized_sort = _normalize_nonnegative_integer(
        sort_order, field_name="分类排序"
    )
    normalized_active = _normalize_boolean(
        is_active, field_name="分类启用状态"
    )
    _ensure_unique(
        db,
        ProductCategory,
        checks=(
            (ProductCategory.name, normalized_name, "分类名称已存在"),
            (ProductCategory.slug, normalized_slug, "分类 slug 已存在"),
        ),
    )
    operation_time = _current_time(db, now)
    category = ProductCategory(
        name=normalized_name,
        slug=normalized_slug,
        description=normalized_description,
        sort_order=normalized_sort,
        is_active=normalized_active,
        created_at=operation_time,
        updated_at=operation_time,
    )
    db.add(category)
    db.flush()
    action_log = _record_audit(
        db,
        actor=actor,
        action_type=MallAuditActionType.CATEGORY_CREATE,
        target_type="product_category",
        target_id=category.id,
        description=f"创建商品分类：{category.name}（{category.slug}）",
        operation_time=operation_time,
    )
    return CatalogMutationResult(
        entity=category,
        action_log=action_log,
        changed=True,
        changed_fields=(
            "name", "slug", "description", "sort_order", "is_active"
        ),
    )


def update_product_category(
    db,
    *,
    category_id: int,
    actor_admin_id: int,
    name=_UNSET,
    slug=_UNSET,
    description=_UNSET,
    sort_order=_UNSET,
    is_active=_UNSET,
    now=None,
):
    """更新分类；存在已上架商品时禁止停用。"""
    from ..models import Product, ProductCategory

    actor = _require_actor(
        db,
        actor_admin_id=actor_admin_id,
        action_type=MallAuditActionType.CATEGORY_UPDATE,
        permission_message=CATALOG_PERMISSION_MESSAGE,
    )
    category = _lock_entity(
        db, ProductCategory, category_id, field_name="商品分类编号"
    )
    new_name = category.name if name is _UNSET else _normalize_required_text(
        name, field_name="分类名称", maximum_length=120
    )
    new_slug = category.slug if slug is _UNSET else _normalize_slug(slug)
    _ensure_unique(
        db,
        ProductCategory,
        checks=(
            (ProductCategory.name, new_name, "分类名称已存在"),
            (ProductCategory.slug, new_slug, "分类 slug 已存在"),
        ),
        exclude_id=category.id,
    )
    values = {
        "name": new_name,
        "slug": new_slug,
        "description": (
            category.description
            if description is _UNSET
            else _normalize_optional_text(
                description, field_name="分类说明", maximum_length=5000
            )
        ),
        "sort_order": (
            category.sort_order
            if sort_order is _UNSET
            else _normalize_nonnegative_integer(
                sort_order, field_name="分类排序"
            )
        ),
        "is_active": (
            category.is_active
            if is_active is _UNSET
            else _normalize_boolean(is_active, field_name="分类启用状态")
        ),
    }
    if category.is_active is True and values["is_active"] is False:
        published = db.query(Product.id).filter(
            Product.category_id == category.id,
            Product.status == ProductStatus.PUBLISHED.value,
        ).first()
        if published is not None:
            raise ValueError("存在已上架商品的分类不能停用")
    changed_fields = []
    for field_name, value in values.items():
        _set_if_changed(category, field_name, value, changed_fields)
    operation_time = _current_time(db, now)
    return _finish_update(
        db,
        entity=category,
        actor=actor,
        action_type=MallAuditActionType.CATEGORY_UPDATE,
        target_type="product_category",
        description_prefix=f"更新商品分类：{category.name}（{category.slug}）",
        changed_fields=changed_fields,
        operation_time=operation_time,
    )


def create_supplier(
    db,
    *,
    actor_admin_id: int,
    name,
    contact_name=None,
    contact_phone=None,
    remark=None,
    is_active=True,
    now=None,
):
    """创建供应商并生成稳定公开编号。"""
    from ..models import Supplier

    actor = _require_actor(
        db,
        actor_admin_id=actor_admin_id,
        action_type=MallAuditActionType.SUPPLIER_CREATE,
        permission_message=SUPPLIER_PERMISSION_MESSAGE,
    )
    normalized_name = _normalize_required_text(
        name, field_name="供应商名称", maximum_length=160
    )
    _ensure_unique(
        db,
        Supplier,
        checks=((Supplier.name, normalized_name, "供应商名称已存在"),),
    )
    operation_time = _current_time(db, now)
    supplier = Supplier(
        supplier_public_id=_generate_public_id(
            db, Supplier, Supplier.supplier_public_id, "SUP"
        ),
        name=normalized_name,
        contact_name=_normalize_optional_text(
            contact_name, field_name="供应商联系人", maximum_length=80
        ),
        contact_phone=_normalize_optional_text(
            contact_phone, field_name="供应商联系电话", maximum_length=40
        ),
        remark=_normalize_optional_text(
            remark, field_name="供应商备注", maximum_length=5000
        ),
        is_active=_normalize_boolean(is_active, field_name="供应商启用状态"),
        created_at=operation_time,
        updated_at=operation_time,
    )
    db.add(supplier)
    db.flush()
    action_log = _record_audit(
        db,
        actor=actor,
        action_type=MallAuditActionType.SUPPLIER_CREATE,
        target_type="supplier",
        target_id=supplier.id,
        description=(
            f"创建商城供应商：{supplier.name}（{supplier.supplier_public_id}）"
        ),
        operation_time=operation_time,
    )
    return CatalogMutationResult(
        entity=supplier,
        action_log=action_log,
        changed=True,
        changed_fields=(
            "name", "contact_name", "contact_phone", "remark", "is_active"
        ),
    )


def update_supplier(
    db,
    *,
    supplier_id: int,
    actor_admin_id: int,
    name=_UNSET,
    contact_name=_UNSET,
    contact_phone=_UNSET,
    remark=_UNSET,
    is_active=_UNSET,
    now=None,
):
    """更新供应商；已上架商品仍使用的供应商不能停用。"""
    from ..models import Product, ProductSku, Supplier

    actor = _require_actor(
        db,
        actor_admin_id=actor_admin_id,
        action_type=MallAuditActionType.SUPPLIER_UPDATE,
        permission_message=SUPPLIER_PERMISSION_MESSAGE,
    )
    supplier = _lock_entity(
        db, Supplier, supplier_id, field_name="供应商编号"
    )
    new_name = supplier.name if name is _UNSET else _normalize_required_text(
        name, field_name="供应商名称", maximum_length=160
    )
    _ensure_unique(
        db,
        Supplier,
        checks=((Supplier.name, new_name, "供应商名称已存在"),),
        exclude_id=supplier.id,
    )
    values = {
        "name": new_name,
        "contact_name": (
            supplier.contact_name
            if contact_name is _UNSET
            else _normalize_optional_text(
                contact_name, field_name="供应商联系人", maximum_length=80
            )
        ),
        "contact_phone": (
            supplier.contact_phone
            if contact_phone is _UNSET
            else _normalize_optional_text(
                contact_phone, field_name="供应商联系电话", maximum_length=40
            )
        ),
        "remark": (
            supplier.remark
            if remark is _UNSET
            else _normalize_optional_text(
                remark, field_name="供应商备注", maximum_length=5000
            )
        ),
        "is_active": (
            supplier.is_active
            if is_active is _UNSET
            else _normalize_boolean(is_active, field_name="供应商启用状态")
        ),
    }
    if supplier.is_active is True and values["is_active"] is False:
        published_sku = (
            db.query(ProductSku.id)
            .join(Product, Product.id == ProductSku.product_id)
            .filter(
                ProductSku.supplier_id == supplier.id,
                ProductSku.is_active.is_(True),
                Product.status == ProductStatus.PUBLISHED.value,
            )
            .first()
        )
        if published_sku is not None:
            raise ValueError("存在已上架商品在售 SKU 的供应商不能停用")
    changed_fields = []
    for field_name, value in values.items():
        _set_if_changed(supplier, field_name, value, changed_fields)
    operation_time = _current_time(db, now)
    return _finish_update(
        db,
        entity=supplier,
        actor=actor,
        action_type=MallAuditActionType.SUPPLIER_UPDATE,
        target_type="supplier",
        description_prefix=(
            f"更新商城供应商：{supplier.name}（{supplier.supplier_public_id}）"
        ),
        changed_fields=changed_fields,
        operation_time=operation_time,
    )


def _require_active_category(db, category_id):
    from ..models import ProductCategory

    category = _lock_entity(
        db, ProductCategory, category_id, field_name="商品分类编号"
    )
    if category.is_active is not True:
        raise ValueError("商品必须归属启用中的分类")
    return category


def create_product(
    db,
    *,
    actor_admin_id: int,
    category_id: int,
    name,
    subtitle=None,
    description=None,
    sort_order=0,
    now=None,
):
    """创建草稿商品；公开编号和初始状态不可由调用方指定。"""
    from ..models import Product

    actor = _require_actor(
        db,
        actor_admin_id=actor_admin_id,
        action_type=MallAuditActionType.PRODUCT_CREATE,
        permission_message=CATALOG_PERMISSION_MESSAGE,
    )
    category = _require_active_category(db, category_id)
    operation_time = _current_time(db, now)
    product = Product(
        product_public_id=_generate_public_id(
            db, Product, Product.product_public_id, "PRD"
        ),
        category_id=category.id,
        name=_normalize_required_text(
            name, field_name="商品名称", maximum_length=200
        ),
        subtitle=_normalize_optional_text(
            subtitle, field_name="商品副标题", maximum_length=300
        ),
        description=_normalize_optional_text(
            description, field_name="商品说明", maximum_length=10000
        ),
        status=ProductStatus.DRAFT.value,
        sort_order=_normalize_nonnegative_integer(
            sort_order, field_name="商品排序"
        ),
        published_at=None,
        created_at=operation_time,
        updated_at=operation_time,
    )
    db.add(product)
    db.flush()
    action_log = _record_audit(
        db,
        actor=actor,
        action_type=MallAuditActionType.PRODUCT_CREATE,
        target_type="product",
        target_id=product.id,
        description=f"创建草稿商品：{product.name}（{product.product_public_id}）",
        operation_time=operation_time,
    )
    return CatalogMutationResult(
        entity=product,
        action_log=action_log,
        changed=True,
        changed_fields=(
            "category_id", "name", "subtitle", "description", "sort_order"
        ),
    )


def update_product(
    db,
    *,
    product_id: int,
    actor_admin_id: int,
    category_id=_UNSET,
    name=_UNSET,
    subtitle=_UNSET,
    description=_UNSET,
    sort_order=_UNSET,
    now=None,
):
    """更新商品资料；状态必须通过专用上下架操作变更。"""
    from ..models import Product

    actor = _require_actor(
        db,
        actor_admin_id=actor_admin_id,
        action_type=MallAuditActionType.PRODUCT_UPDATE,
        permission_message=CATALOG_PERMISSION_MESSAGE,
    )
    product = _lock_entity(db, Product, product_id, field_name="商品编号")
    new_category_id = product.category_id
    if category_id is not _UNSET:
        new_category_id = _require_active_category(db, category_id).id
    values = {
        "category_id": new_category_id,
        "name": (
            product.name
            if name is _UNSET
            else _normalize_required_text(
                name, field_name="商品名称", maximum_length=200
            )
        ),
        "subtitle": (
            product.subtitle
            if subtitle is _UNSET
            else _normalize_optional_text(
                subtitle, field_name="商品副标题", maximum_length=300
            )
        ),
        "description": (
            product.description
            if description is _UNSET
            else _normalize_optional_text(
                description, field_name="商品说明", maximum_length=10000
            )
        ),
        "sort_order": (
            product.sort_order
            if sort_order is _UNSET
            else _normalize_nonnegative_integer(
                sort_order, field_name="商品排序"
            )
        ),
    }
    changed_fields = []
    for field_name, value in values.items():
        _set_if_changed(product, field_name, value, changed_fields)
    operation_time = _current_time(db, now)
    return _finish_update(
        db,
        entity=product,
        actor=actor,
        action_type=MallAuditActionType.PRODUCT_UPDATE,
        target_type="product",
        description_prefix=(
            f"更新商品：{product.name}（{product.product_public_id}）"
        ),
        changed_fields=changed_fields,
        operation_time=operation_time,
    )


def _active_sellable_sku_count(db, *, product_id):
    from ..models import ProductSku, Supplier

    return (
        db.query(ProductSku.id)
        .join(Supplier, Supplier.id == ProductSku.supplier_id)
        .filter(
            ProductSku.product_id == product_id,
            ProductSku.is_active.is_(True),
            Supplier.is_active.is_(True),
        )
        .count()
    )


def publish_product(
    db,
    *,
    product_id: int,
    actor_admin_id: int,
    now=None,
):
    """将草稿或已下架商品上架；必须存在可售 SKU。"""
    from ..models import Product, ProductCategory

    actor = _require_actor(
        db,
        actor_admin_id=actor_admin_id,
        action_type=MallAuditActionType.PRODUCT_PUBLISH,
        permission_message=CATALOG_PERMISSION_MESSAGE,
    )
    product = _lock_entity(db, Product, product_id, field_name="商品编号")
    if product.status == ProductStatus.PUBLISHED.value:
        return CatalogMutationResult(product, None, False, ())
    if product.status not in {
        ProductStatus.DRAFT.value,
        ProductStatus.UNPUBLISHED.value,
    }:
        raise ValueError("当前商品状态不能上架")
    category = _lock_entity(
        db, ProductCategory, product.category_id, field_name="商品分类编号"
    )
    if category.is_active is not True:
        raise ValueError("停用分类中的商品不能上架")
    if _active_sellable_sku_count(db, product_id=product.id) == 0:
        raise ValueError("商品至少需要一个启用且供应商有效的 SKU 才能上架")
    operation_time = _current_time(db, now)
    product.status = ProductStatus.PUBLISHED.value
    product.published_at = operation_time
    product.updated_at = operation_time
    db.flush()
    action_log = _record_audit(
        db,
        actor=actor,
        action_type=MallAuditActionType.PRODUCT_PUBLISH,
        target_type="product",
        target_id=product.id,
        description=f"上架商品：{product.name}（{product.product_public_id}）",
        operation_time=operation_time,
    )
    return CatalogMutationResult(
        product, action_log, True, ("status", "published_at")
    )


def unpublish_product(
    db,
    *,
    product_id: int,
    actor_admin_id: int,
    now=None,
):
    """下架已上架商品；草稿不能借下架操作跳过首次上架。"""
    from ..models import Product

    actor = _require_actor(
        db,
        actor_admin_id=actor_admin_id,
        action_type=MallAuditActionType.PRODUCT_UNPUBLISH,
        permission_message=CATALOG_PERMISSION_MESSAGE,
    )
    product = _lock_entity(db, Product, product_id, field_name="商品编号")
    if product.status == ProductStatus.UNPUBLISHED.value:
        return CatalogMutationResult(product, None, False, ())
    if product.status != ProductStatus.PUBLISHED.value:
        raise ValueError("只有已上架商品可以下架")
    operation_time = _current_time(db, now)
    product.status = ProductStatus.UNPUBLISHED.value
    product.updated_at = operation_time
    db.flush()
    action_log = _record_audit(
        db,
        actor=actor,
        action_type=MallAuditActionType.PRODUCT_UNPUBLISH,
        target_type="product",
        target_id=product.id,
        description=f"下架商品：{product.name}（{product.product_public_id}）",
        operation_time=operation_time,
    )
    return CatalogMutationResult(product, action_log, True, ("status",))


def _require_active_supplier(db, supplier_id):
    from ..models import Supplier

    supplier = _lock_entity(db, Supplier, supplier_id, field_name="供应商编号")
    if supplier.is_active is not True:
        raise ValueError("SKU 必须归属启用中的供应商")
    return supplier


def create_product_sku(
    db,
    *,
    product_id: int,
    supplier_id: int,
    actor_admin_id: int,
    sku_code,
    name,
    points_price,
    cost_price,
    supplier_sku_code=None,
    low_stock_threshold=0,
    is_active=True,
    sort_order=0,
    now=None,
):
    """为商品创建 SKU；SKU 编码由服务规范为大写。"""
    from ..models import Product, ProductSku

    actor = _require_actor(
        db,
        actor_admin_id=actor_admin_id,
        action_type=MallAuditActionType.SKU_CREATE,
        permission_message=CATALOG_PERMISSION_MESSAGE,
    )
    product = _lock_entity(db, Product, product_id, field_name="商品编号")
    supplier = _require_active_supplier(db, supplier_id)
    normalized_code = _normalize_code(
        sku_code, field_name="SKU 编码", maximum_length=64
    )
    normalized_name = _normalize_required_text(
        name, field_name="SKU 名称", maximum_length=160
    )
    normalized_supplier_code = _normalize_optional_text(
        supplier_sku_code, field_name="供应商货号", maximum_length=100
    )
    if normalized_supplier_code is not None:
        normalized_supplier_code = normalized_supplier_code.upper()
    checks = [
        (ProductSku.sku_code, normalized_code, "SKU 编码已存在"),
    ]
    with db.no_autoflush:
        duplicate_name = db.query(ProductSku.id).filter(
            ProductSku.product_id == product.id,
            ProductSku.name == normalized_name,
        ).first()
        duplicate_supplier_code = None
        if normalized_supplier_code is not None:
            duplicate_supplier_code = db.query(ProductSku.id).filter(
                ProductSku.supplier_id == supplier.id,
                ProductSku.supplier_sku_code == normalized_supplier_code,
            ).first()
    _ensure_unique(db, ProductSku, checks=tuple(checks))
    if duplicate_name is not None:
        raise ValueError("同一商品下的 SKU 名称已存在")
    if duplicate_supplier_code is not None:
        raise ValueError("该供应商货号已被使用")
    operation_time = _current_time(db, now)
    sku = ProductSku(
        product_id=product.id,
        supplier_id=supplier.id,
        sku_code=normalized_code,
        name=normalized_name,
        supplier_sku_code=normalized_supplier_code,
        points_price=_normalize_price(
            points_price, field_name="SKU 积分售价", allow_zero=False
        ),
        cost_price=_normalize_price(
            cost_price, field_name="SKU 人民币成本", allow_zero=True
        ),
        low_stock_threshold=_normalize_nonnegative_integer(
            low_stock_threshold, field_name="低库存阈值"
        ),
        is_active=_normalize_boolean(is_active, field_name="SKU 启用状态"),
        sort_order=_normalize_nonnegative_integer(
            sort_order, field_name="SKU 排序"
        ),
        created_at=operation_time,
        updated_at=operation_time,
    )
    db.add(sku)
    db.flush()
    action_log = _record_audit(
        db,
        actor=actor,
        action_type=MallAuditActionType.SKU_CREATE,
        target_type="product_sku",
        target_id=sku.id,
        description=(
            f"创建商品 SKU：{sku.sku_code}；商品 {product.product_public_id}；"
            f"供应商 {supplier.supplier_public_id}"
        ),
        operation_time=operation_time,
    )
    return CatalogMutationResult(
        entity=sku,
        action_log=action_log,
        changed=True,
        changed_fields=(
            "product_id", "supplier_id", "sku_code", "name",
            "supplier_sku_code", "points_price", "cost_price",
            "low_stock_threshold", "is_active", "sort_order",
        ),
    )


def update_product_sku(
    db,
    *,
    sku_id: int,
    actor_admin_id: int,
    supplier_id=_UNSET,
    name=_UNSET,
    supplier_sku_code=_UNSET,
    points_price=_UNSET,
    cost_price=_UNSET,
    low_stock_threshold=_UNSET,
    is_active=_UNSET,
    sort_order=_UNSET,
    now=None,
):
    """更新 SKU 当前资料；商品归属和稳定 SKU 编码不可修改。"""
    from ..models import Product, ProductSku

    actor = _require_actor(
        db,
        actor_admin_id=actor_admin_id,
        action_type=MallAuditActionType.SKU_UPDATE,
        permission_message=CATALOG_PERMISSION_MESSAGE,
    )
    sku = _lock_entity(db, ProductSku, sku_id, field_name="SKU 编号")
    product = _lock_entity(db, Product, sku.product_id, field_name="商品编号")
    new_supplier_id = sku.supplier_id
    if supplier_id is not _UNSET:
        new_supplier_id = _require_active_supplier(db, supplier_id).id
    new_name = sku.name if name is _UNSET else _normalize_required_text(
        name, field_name="SKU 名称", maximum_length=160
    )
    new_supplier_code = sku.supplier_sku_code
    if supplier_sku_code is not _UNSET:
        new_supplier_code = _normalize_optional_text(
            supplier_sku_code, field_name="供应商货号", maximum_length=100
        )
        if new_supplier_code is not None:
            new_supplier_code = new_supplier_code.upper()
    with db.no_autoflush:
        duplicate_name = db.query(ProductSku.id).filter(
            ProductSku.id != sku.id,
            ProductSku.product_id == sku.product_id,
            ProductSku.name == new_name,
        ).first()
        duplicate_supplier_code = None
        if new_supplier_code is not None:
            duplicate_supplier_code = db.query(ProductSku.id).filter(
                ProductSku.id != sku.id,
                ProductSku.supplier_id == new_supplier_id,
                ProductSku.supplier_sku_code == new_supplier_code,
            ).first()
    if duplicate_name is not None:
        raise ValueError("同一商品下的 SKU 名称已存在")
    if duplicate_supplier_code is not None:
        raise ValueError("该供应商货号已被使用")
    values = {
        "supplier_id": new_supplier_id,
        "name": new_name,
        "supplier_sku_code": new_supplier_code,
        "points_price": (
            sku.points_price
            if points_price is _UNSET
            else _normalize_price(
                points_price, field_name="SKU 积分售价", allow_zero=False
            )
        ),
        "cost_price": (
            sku.cost_price
            if cost_price is _UNSET
            else _normalize_price(
                cost_price, field_name="SKU 人民币成本", allow_zero=True
            )
        ),
        "low_stock_threshold": (
            sku.low_stock_threshold
            if low_stock_threshold is _UNSET
            else _normalize_nonnegative_integer(
                low_stock_threshold, field_name="低库存阈值"
            )
        ),
        "is_active": (
            sku.is_active
            if is_active is _UNSET
            else _normalize_boolean(is_active, field_name="SKU 启用状态")
        ),
        "sort_order": (
            sku.sort_order
            if sort_order is _UNSET
            else _normalize_nonnegative_integer(sort_order, field_name="SKU 排序")
        ),
    }
    if (
        product.status == ProductStatus.PUBLISHED.value
        and sku.is_active is True
        and values["is_active"] is False
    ):
        other_active_count = db.query(ProductSku.id).filter(
            ProductSku.product_id == product.id,
            ProductSku.id != sku.id,
            ProductSku.is_active.is_(True),
        ).count()
        if other_active_count == 0:
            raise ValueError("已上架商品不能停用最后一个启用 SKU")
    changed_fields = []
    for field_name, value in values.items():
        _set_if_changed(sku, field_name, value, changed_fields)
    operation_time = _current_time(db, now)
    return _finish_update(
        db,
        entity=sku,
        actor=actor,
        action_type=MallAuditActionType.SKU_UPDATE,
        target_type="product_sku",
        description_prefix=f"更新商品 SKU：{sku.sku_code}",
        changed_fields=changed_fields,
        operation_time=operation_time,
    )
