# 商品媒体说明

## 1. 本切片范围

M4-4 在商品目录后台接入商品主图、轮播图和详情图：

- 新增 `product_media` 表及 `0005_product_media` 线性迁移；
- 商品主图每个商品最多一张，重复上传会原位替换记录；
- 轮播图和详情图允许多张，可独立设置排序、启用状态和替代文字；
- 图片只允许 JPG、PNG、WebP，单文件不超过 5 MB；
- 服务端核验实际图片格式、扩展名、尺寸与像素总量，统一转为最长边不超过 2400 像素的 WebP；
- 所有数据库写操作重新校验当前管理员并记录独立审计动作。

本切片不建立库存余额或流水，不实现商品/库存 Excel、订单、小程序商品接口或商城横幅。

## 2. 存储边界

商品图片保存在 `uploads/mall_products/<商品公开编号>/`，访问前缀为 `/uploads/mall_products/`。该目录与 `uploads/promotion_pages/` 完全隔离，宣传页素材不能作为商品媒体记录写入，商品媒体删除也不会处理宣传页目录中的文件。

文件名由服务端根据用途和随机编号生成，不采用客户端文件名。删除操作会先解析专用 URL 前缀，再确认目标仍位于商品图片根目录下，拒绝路径穿越和目录外删除。

## 3. 数据与事务边界

- `media_role` 只允许 `MAIN`、`CAROUSEL`、`DETAIL`；
- `image_path` 全局唯一且不能为空，排序不得为负；
- 数据库使用条件唯一索引保证每个商品最多一张主图；
- 上传时先验证并写入新文件，再在同一数据库事务中保存媒体记录和管理员日志；数据库失败时删除本次新文件；
- 主图替换只有在数据库提交成功后才删除旧文件，避免失败时丢失当前主图；
- 删除时先提交媒体记录与审计日志，再清理对应文件。若提交后文件系统清理失败，页面会明确提示检查磁盘权限，数据库不会伪装回滚；
- 更新替代文字、排序或启用状态时不改商品归属、图片用途或文件路径；无实际变化时不新增审计日志。

当前上架条件仍沿用 M4-2：启用分类和至少一个启用、供应商有效的 SKU。M4-4 不追溯修改已存在商品的上架条件；会员端商品接口开放前再统一固化“可见商品必须具备启用主图”等展示规则。

## 4. 权限与审计

超级管理员与运营管理员可以上传、替换、编辑和删除商品图片。初审、复核、上传方、停用账号、不存在账号及未知权限级别失败关闭。

实际变化分别记录：

- `mall_product_media_create`；
- `mall_product_media_update`；
- `mall_product_media_delete`。

页面权限不能代替领域服务权限；即使直接调用服务，仍会从数据库重新锁定并核验操作者。

## 5. 迁移与升级

已有 `0004_catalog_foundation` SQLite 数据库升级前必须停止应用，备份 `saas_mvp.db` 与整个 `uploads/`，再运行：

```powershell
python -m app.migration_upgrade_rehearsal
```

副本演练会验证真实 0004 目录表和关键字段，证明原表结构与字段值未变，并只在副本中升级到 `0005_product_media`。演练通过后仍需单独批准真实升级。

## 6. 测试入口

```powershell
python -m compileall app migrations
python -m unittest -v `
    test_product_media_storage.py `
    test_product_media_service.py `
    test_product_media_migration.py `
    test_catalog_routes.py `
    test_migration_upgrade_rehearsal.py `
    test_schema_readiness.py `
    test_mall_governance.py `
    test_postgresql_migration.py
```

专项测试覆盖格式伪装、大小和尺寸限制、路径穿越、WebP 转换、主图唯一与替换、多张轮播/详情图、权限、审计、无变化、删除、页面上传流程、0004 副本演练、虚假结构拒绝及 PostgreSQL 离线迁移 SQL。
