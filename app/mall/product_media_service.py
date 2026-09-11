"""受权限和审计保护的商品媒体写服务。"""

from dataclasses import dataclass
from pathlib import PurePosixPath

from .audit import MallAuditActionType
from .catalog_service import (
    CATALOG_PERMISSION_MESSAGE,
    _current_time,
    _lock_entity,
    _normalize_boolean,
    _normalize_nonnegative_integer,
    _normalize_optional_text,
    _normalize_required_text,
    _record_audit,
    _require_actor,
)
from .domain import ProductMediaRole, normalize_product_media_role
from .product_media_storage import PRODUCT_IMAGE_URL_PREFIX


_UNSET = object()


@dataclass(frozen=True)
class ProductMediaMutationResult:
    entity: object
    action_log: object | None
    changed: bool
    changed_fields: tuple[str, ...]
    previous_image_path: str | None = None


def _normalize_image_path(value, *, product_public_id: str) -> str:
    image_path = _normalize_required_text(
        value,
        field_name="商品图片路径",
        maximum_length=500,
    )
    parsed_path = PurePosixPath(image_path)
    expected_parent = (
        PurePosixPath(PRODUCT_IMAGE_URL_PREFIX) / product_public_id
    )
    if (
        "\\" in image_path
        or str(parsed_path) != image_path
        or parsed_path.parent != expected_parent
        or parsed_path.suffix.lower() != ".webp"
        or not parsed_path.stem
    ):
        raise ValueError("商品图片路径不属于当前商品的独立存储目录")
    return image_path


def save_product_media_record(
    db,
    *,
    product_id: int,
    actor_admin_id: int,
    media_role: ProductMediaRole | str,
    image_path: str,
    alt_text=None,
    sort_order: int = 0,
    is_active: bool = True,
    now=None,
) -> ProductMediaMutationResult:
    """Create media, or atomically replace the single main-image record."""
    from ..models import Product, ProductMedia

    actor = _require_actor(
        db,
        actor_admin_id=actor_admin_id,
        action_type=MallAuditActionType.PRODUCT_MEDIA_CREATE,
        permission_message=CATALOG_PERMISSION_MESSAGE,
    )
    product = _lock_entity(db, Product, product_id, field_name="商品编号")
    role = normalize_product_media_role(media_role)
    normalized_path = _normalize_image_path(
        image_path,
        product_public_id=product.product_public_id,
    )
    normalized_alt = _normalize_optional_text(
        alt_text,
        field_name="图片替代文字",
        maximum_length=200,
    )
    normalized_sort = _normalize_nonnegative_integer(
        sort_order,
        field_name="图片排序",
    )
    normalized_active = _normalize_boolean(
        is_active,
        field_name="图片启用状态",
    )
    operation_time = _current_time(db, now)

    existing = None
    if role is ProductMediaRole.MAIN:
        with db.no_autoflush:
            existing = (
                db.query(ProductMedia)
                .filter(
                    ProductMedia.product_id == product.id,
                    ProductMedia.media_role == ProductMediaRole.MAIN.value,
                )
                .with_for_update()
                .populate_existing()
                .one_or_none()
            )

    if existing is not None:
        previous_path = existing.image_path
        existing.image_path = normalized_path
        existing.alt_text = normalized_alt
        existing.sort_order = normalized_sort
        existing.is_active = normalized_active
        existing.uploaded_by_id = actor.id
        existing.updated_at = operation_time
        db.flush()
        action_log = _record_audit(
            db,
            actor=actor,
            action_type=MallAuditActionType.PRODUCT_MEDIA_UPDATE,
            target_type="product_media",
            target_id=existing.id,
            description=f"替换商品主图：{product.name}（{product.product_public_id}）",
            operation_time=operation_time,
        )
        return ProductMediaMutationResult(
            entity=existing,
            action_log=action_log,
            changed=True,
            changed_fields=(
                "image_path",
                "alt_text",
                "sort_order",
                "is_active",
                "uploaded_by_id",
            ),
            previous_image_path=previous_path,
        )

    media = ProductMedia(
        product_id=product.id,
        media_role=role.value,
        image_path=normalized_path,
        alt_text=normalized_alt,
        sort_order=normalized_sort,
        is_active=normalized_active,
        uploaded_by_id=actor.id,
        created_at=operation_time,
        updated_at=operation_time,
    )
    db.add(media)
    db.flush()
    action_log = _record_audit(
        db,
        actor=actor,
        action_type=MallAuditActionType.PRODUCT_MEDIA_CREATE,
        target_type="product_media",
        target_id=media.id,
        description=(
            f"新增商品{role.value}图片："
            f"{product.name}（{product.product_public_id}）"
        ),
        operation_time=operation_time,
    )
    return ProductMediaMutationResult(
        entity=media,
        action_log=action_log,
        changed=True,
        changed_fields=(
            "product_id",
            "media_role",
            "image_path",
            "alt_text",
            "sort_order",
            "is_active",
        ),
    )


def update_product_media_record(
    db,
    *,
    media_id: int,
    actor_admin_id: int,
    alt_text=_UNSET,
    sort_order=_UNSET,
    is_active=_UNSET,
    now=None,
) -> ProductMediaMutationResult:
    """Update media metadata while keeping product, role and file immutable."""
    from ..models import Product, ProductMedia

    actor = _require_actor(
        db,
        actor_admin_id=actor_admin_id,
        action_type=MallAuditActionType.PRODUCT_MEDIA_UPDATE,
        permission_message=CATALOG_PERMISSION_MESSAGE,
    )
    media = _lock_entity(db, ProductMedia, media_id, field_name="商品图片编号")
    product = _lock_entity(db, Product, media.product_id, field_name="商品编号")
    values = {
        "alt_text": (
            media.alt_text
            if alt_text is _UNSET
            else _normalize_optional_text(
                alt_text,
                field_name="图片替代文字",
                maximum_length=200,
            )
        ),
        "sort_order": (
            media.sort_order
            if sort_order is _UNSET
            else _normalize_nonnegative_integer(
                sort_order,
                field_name="图片排序",
            )
        ),
        "is_active": (
            media.is_active
            if is_active is _UNSET
            else _normalize_boolean(
                is_active,
                field_name="图片启用状态",
            )
        ),
    }
    changed_fields = tuple(
        field_name
        for field_name, value in values.items()
        if getattr(media, field_name) != value
    )
    if not changed_fields:
        return ProductMediaMutationResult(media, None, False, ())
    for field_name in changed_fields:
        setattr(media, field_name, values[field_name])
    operation_time = _current_time(db, now)
    media.updated_at = operation_time
    db.flush()
    action_log = _record_audit(
        db,
        actor=actor,
        action_type=MallAuditActionType.PRODUCT_MEDIA_UPDATE,
        target_type="product_media",
        target_id=media.id,
        description=(
            f"更新商品{media.media_role}图片："
            f"{product.name}（{product.product_public_id}）；"
            f"字段：{', '.join(changed_fields)}"
        ),
        operation_time=operation_time,
    )
    return ProductMediaMutationResult(
        media,
        action_log,
        True,
        changed_fields,
    )


def delete_product_media_record(
    db,
    *,
    media_id: int,
    actor_admin_id: int,
    now=None,
) -> ProductMediaMutationResult:
    """Delete one media record and return its path for post-commit cleanup."""
    from ..models import Product, ProductMedia

    actor = _require_actor(
        db,
        actor_admin_id=actor_admin_id,
        action_type=MallAuditActionType.PRODUCT_MEDIA_DELETE,
        permission_message=CATALOG_PERMISSION_MESSAGE,
    )
    media = _lock_entity(db, ProductMedia, media_id, field_name="商品图片编号")
    product = _lock_entity(db, Product, media.product_id, field_name="商品编号")
    image_path = media.image_path
    media_role = media.media_role
    target_id = media.id
    operation_time = _current_time(db, now)
    db.delete(media)
    db.flush()
    action_log = _record_audit(
        db,
        actor=actor,
        action_type=MallAuditActionType.PRODUCT_MEDIA_DELETE,
        target_type="product_media",
        target_id=target_id,
        description=(
            f"删除商品{media_role}图片："
            f"{product.name}（{product.product_public_id}）"
        ),
        operation_time=operation_time,
    )
    return ProductMediaMutationResult(
        entity=media,
        action_log=action_log,
        changed=True,
        changed_fields=("deleted",),
        previous_image_path=image_path,
    )
