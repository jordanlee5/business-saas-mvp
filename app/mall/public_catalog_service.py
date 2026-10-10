"""小程序公开目录的只读投影；不暴露供应商和采购成本。"""

from sqlalchemy import func, or_

from .domain import ProductStatus


def list_public_categories(db):
    from ..models import ProductCategory

    with db.no_autoflush:
        rows = (db.query(ProductCategory)
                .filter(ProductCategory.is_active.is_(True))
                .order_by(ProductCategory.sort_order, ProductCategory.id).all())
        return [dict(name=row.name, slug=row.slug) for row in rows]


def list_public_products(db, *, category_slug=None, search_term=None,
                         page=1, page_size=20):
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
    if search_term is not None:
        if not isinstance(search_term, str) or len(search_term) > 80:
            raise ValueError("搜索词无效")
        search_term = search_term.strip()
        if not search_term:
            raise ValueError("搜索词无效")

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
        if search_term is not None:
            # Escape LIKE wildcards; a user's '%' or '_' is literal text.
            escaped = (search_term.lower().replace("/", "//")
                       .replace("%", "/%").replace("_", "/_"))
            pattern = f"%{escaped}%"
            query = query.filter(or_(
                func.lower(Product.name).like(pattern, escape="/"),
                func.lower(Product.subtitle).like(pattern, escape="/"),
            ))
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


def get_public_product_detail(db, *, product_public_id):
    """Return an on-sale product's public SKU and media projection, or None."""
    from ..models import (
        InventoryBalance, Product, ProductCategory, ProductMedia,
        ProductSku, Supplier,
    )

    if (not isinstance(product_public_id, str)
            or not 1 <= len(product_public_id) <= 32
            or not all(char in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-"
                       for char in product_public_id)):
        raise ValueError("商品公开编号无效")

    with db.no_autoflush:
        row = (db.query(Product, ProductCategory)
               .join(ProductCategory, ProductCategory.id == Product.category_id)
               .filter(Product.product_public_id == product_public_id,
                       Product.status == ProductStatus.PUBLISHED.value,
                       ProductCategory.is_active.is_(True)).first())
        if row is None:
            return None
        product, category = row

        sku_rows = (db.query(ProductSku, InventoryBalance)
                    .join(Supplier, Supplier.id == ProductSku.supplier_id)
                    .outerjoin(InventoryBalance, InventoryBalance.sku_id == ProductSku.id)
                    .filter(ProductSku.product_id == product.id,
                            ProductSku.is_active.is_(True),
                            Supplier.is_active.is_(True))
                    .order_by(ProductSku.sort_order, ProductSku.id).all())
        if not sku_rows:
            return None

        skus = [dict(
            sku_code=sku.sku_code,
            name=sku.name,
            points_price=f"{sku.points_price:.2f}",
            in_stock=(balance is not None
                      and balance.on_hand_quantity > balance.reserved_quantity),
        ) for sku, balance in sku_rows]

        media_rows = (db.query(ProductMedia)
                      .filter(ProductMedia.product_id == product.id,
                              ProductMedia.is_active.is_(True))
                      .order_by(ProductMedia.sort_order, ProductMedia.id).all())
        image_prefix = f"/uploads/mall_products/{product_public_id}/"
        images = [dict(role=media.media_role.lower(), url=media.image_path,
                       alt_text=media.alt_text)
                  for media in media_rows
                  if media.image_path.startswith(image_prefix)
                  and media.media_role in ("MAIN", "CAROUSEL", "DETAIL")]
        main_image_url = next((image["url"] for image in images
                               if image["role"] == "main"), None)

        return dict(
            product_public_id=product.product_public_id,
            name=product.name,
            subtitle=product.subtitle,
            description=product.description,
            category_slug=category.slug,
            min_points_price=f"{min(sku.points_price for sku, _ in sku_rows):.2f}",
            in_stock=any(sku["in_stock"] for sku in skus),
            main_image_url=main_image_url,
            images=images,
            skus=skus,
        )
