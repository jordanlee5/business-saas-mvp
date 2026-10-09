"""小程序公开目录的只读投影；不暴露供应商和采购成本。"""

from sqlalchemy import func

from .domain import ProductStatus


def list_public_categories(db):
    from ..models import ProductCategory

    with db.no_autoflush:
        rows = (db.query(ProductCategory)
                .filter(ProductCategory.is_active.is_(True))
                .order_by(ProductCategory.sort_order, ProductCategory.id).all())
        return [dict(name=row.name, slug=row.slug) for row in rows]


def list_public_products(db, *, category_slug=None, page=1, page_size=20):
    from ..models import (
        InventoryBalance, Product, ProductCategory, ProductMedia,
        ProductSku, Supplier,
    )

    if isinstance(page, bool) or not isinstance(page, int) or page < 1:
        raise ValueError("页码无效")
    if isinstance(page_size, bool) or not isinstance(page_size, int) or not 1 <= page_size <= 50:
        raise ValueError("每页数量无效")
    if category_slug is not None and (
        not isinstance(category_slug, str) or not category_slug
        or len(category_slug) > 80
    ):
        raise ValueError("分类标识无效")

    with db.no_autoflush:
        valid_skus = (db.query(ProductSku.product_id)
                      .join(Supplier, Supplier.id == ProductSku.supplier_id)
                      .filter(ProductSku.is_active.is_(True), Supplier.is_active.is_(True)))
        query = (db.query(Product)
                 .join(ProductCategory, ProductCategory.id == Product.category_id)
                 .filter(ProductCategory.is_active.is_(True),
                         Product.status == ProductStatus.PUBLISHED.value,
                         Product.id.in_(valid_skus)))
        if category_slug is not None:
            query = query.filter(ProductCategory.slug == category_slug)
        total = query.with_entities(func.count(Product.id)).scalar() or 0
        products = (query.order_by(Product.sort_order, Product.id)
                    .offset((page - 1) * page_size).limit(page_size).all())
        if not products:
            return dict(items=[], page=page, page_size=page_size, total=total)

        ids = [product.id for product in products]
        categories = {
            row.id: row.slug for row in db.query(ProductCategory)
            .filter(ProductCategory.id.in_({product.category_id for product in products}))
        }
        sku_rows = (db.query(ProductSku, InventoryBalance)
                    .join(Supplier, Supplier.id == ProductSku.supplier_id)
                    .outerjoin(InventoryBalance, InventoryBalance.sku_id == ProductSku.id)
                    .filter(ProductSku.product_id.in_(ids),
                            ProductSku.is_active.is_(True), Supplier.is_active.is_(True))
                    .all())
        skus_by_product = {product_id: [] for product_id in ids}
        for sku, balance in sku_rows:
            skus_by_product[sku.product_id].append((sku, balance))
        media_rows = (db.query(ProductMedia)
                      .filter(ProductMedia.product_id.in_(ids),
                              ProductMedia.media_role == "MAIN",
                              ProductMedia.is_active.is_(True)).all())
        images = {
            row.product_id: row.image_path
            for row in media_rows
            if row.image_path.startswith("/uploads/mall_products/")
        }
        items = []
        for product in products:
            choices = skus_by_product[product.id]
            items.append(dict(
                product_public_id=product.product_public_id,
                name=product.name,
                subtitle=product.subtitle,
                category_slug=categories[product.category_id],
                min_points_price=f"{min(sku.points_price for sku, _ in choices):.2f}",
                in_stock=any(
                    balance is not None
                    and balance.on_hand_quantity > balance.reserved_quantity
                    for _, balance in choices
                ),
                main_image_url=images.get(product.id),
            ))
        return dict(items=items, page=page, page_size=page_size, total=total)
