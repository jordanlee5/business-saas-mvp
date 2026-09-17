# 迁移版本

`0001_current_schema_baseline` 固化 `ee34b97` 的原有结构；`0002_mall_core_foundation` 建立商城渠道字段以及会员、积分核心结构；`0003_member_activation_security` 建立不保存明文的一次性激活凭据结构；`0004_catalog_foundation` 建立商品分类、供应商、商品和 SKU 目录基础；`0005_product_media` 建立商品主图、轮播图与详情图结构；`0006_inventory_foundation` 建立 SKU 库存余额与不可变流水结构；`0007_order_foundation` 建立订单、订单项快照与订单积分批次分配结构；`0008_order_reservation` 建立订单幂等与可重算的库存预占流水；`0009_order_shipping_completion` 建立订单手工物流与完成证据。后续通过审阅的 Alembic revision 文件统一保存在此目录，并保持单一线性迁移链。
