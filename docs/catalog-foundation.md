# 商品目录基础说明

## 1. 本切片边界

M4-1 只建立商城商品目录的数据库基础和状态规则，不提供后台页面或业务写入口。新增结构为：

| 表 | 作用 | 本轮关键约束 |
| --- | --- | --- |
| `product_categories` | 商品分类 | 名称和 slug 唯一且非空，排序非负，可停用 |
| `suppliers` | 供货主体 | 稳定公开编号和名称唯一且非空，可停用 |
| `products` | 商品 SPU | 稳定公开编号唯一，必须归属分类，状态只能为草稿、上架或下架 |
| `product_skus` | 可售最小单位 | 必须归属商品和供应商，SKU 编码唯一，积分价为正、人民币成本非负 |

本轮明确不包含商品图片与轮播、后台增删改查、库存余额和流水、商品/库存 Excel 导入导出、订单、小程序商品 API。数据库升级后以上四张表为空是预期结果。

## 2. 状态与价格规则

- 商品状态值固定为 `DRAFT`、`PUBLISHED`、`UNPUBLISHED`，未知值失败关闭；
- `PUBLISHED` 商品必须同时保存 `published_at`；下架不会删除商品或 SKU；
- SKU 的 `points_price` 与 `cost_price` 均使用 `NUMERIC(18, 2)`，当前积分售价必须大于零，人民币成本可以为零但不能为负；
- 分类、商品和 SKU 的排序值必须为非负整数，数值越小越靠前；
- `low_stock_threshold` 只保存预警阈值，本轮没有库存余额，不能据此判断真实可售状态；
- M5 订单明细必须保存商品、SKU、积分售价、人民币成本和供应商快照，后续修改目录价格不得回写历史订单。

## 3. 已确认的订单前置规则

- 一期全部包邮，订单不得收取积分或现金运费；
- 退款按原消费批次退回；如果原批次在退款时已经到期，则进入人工处理，不自动延长有效期或新建临时退款批次；
- 两项规则只在本轮固化为后续约束，M4-1 没有实现订单、扣分或退款代码。

## 4. 数据库升级

M4-1 完成时的迁移 head 为 `0004_catalog_foundation`；当前 head 已由后续切片推进，具体版本见仓库根目录 README。处于 `0001`、`0002`、`0003` 或 `0004` 的已有 SQLite 数据库升级前必须停止应用、保留数据库与上传目录备份，并先运行：

```powershell
python -m app.migration_upgrade_rehearsal
```

演练只升级单独副本，并核对源库版本、业务数据指纹和升级后结构。只有演练通过并完成人工审阅后，才可另行批准真实数据库执行：

```powershell
python -m alembic -c alembic.ini upgrade head
```

升级后的只读结构核验：

```powershell
python -c "from app.database import engine; from app.schema_readiness import assert_database_schema_ready; r=assert_database_schema_ready(engine); print('结构就绪版本:', r.revision); print('必需表数量:', len(r.checked_tables))"
```

M4-1 当时的预期版本为 `0004_catalog_foundation`、必需表数量为 20；当前版本与数量见根目录 README。该检查不创建商品数据。
