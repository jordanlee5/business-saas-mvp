# SKU 库存余额与流水基础

## 1. 本轮范围

M4-5 为每个商品 SKU 建立库存余额和不可变流水，并提供受权限、审计和幂等保护的入库与人工调整领域服务：

- `inventory_balances`：每个 SKU 最多一条余额，保存现存数量、预占数量和余额版本；
- `inventory_movements`：保存每次变动前后数量、增量、顺序版本、原因、操作者、稳定流水编号和幂等键；
- `receive_inventory()`：只接受正整数入库；
- `adjust_inventory()`：只接受非零整数增量调整；
- `audit_inventory_balance()`：按流水版本重算现存数量并核对余额缓存；
- `assert_inventory_balance_consistent()`：发现余额或流水断链时失败关闭。

本轮不增加库存管理页面、Excel 导入导出、订单预占、释放、出库、退回或小程序商品 API。

## 2. 数量与状态规则

- 库存管理到 SKU，不在商品 SPU 层保存可售数量；
- `on_hand_quantity`、`reserved_quantity` 和余额版本必须为非负整数；
- `reserved_quantity` 不得超过 `on_hand_quantity`；本轮只预留字段，不开放预占写入口；
- 可售数量为 `on_hand_quantity - reserved_quantity`；
- 可售数量为零时状态为 `OUT_OF_STOCK`；
- 可售数量大于零且不高于 SKU 的 `low_stock_threshold` 时为 `LOW_STOCK`；
- 其余为 `IN_STOCK`。

迁移不会为既有 SKU 自动创建零余额行。未发生过库存操作的 SKU 在只读计算中视为现存、预占和可售数量均为零。

## 3. 写入、幂等与事务

每次有效写入必须：

1. 校验启用中的超级管理员或运营管理员权限；
2. 锁定操作者与目标 SKU，并在 PostgreSQL 上锁定余额行；
3. 校验幂等键；相同完整请求安全重放，不重复写库存或日志；
4. 验证调整后现存数量不低于已预占数量；
5. 增加余额版本并追加一条对应版本流水；
6. 追加 `mall_inventory_receive` 或 `mall_inventory_adjust` 管理员日志。

领域服务不自行提交事务。调用方必须把余额、流水和管理员日志整体提交；任何权限、校验或数据库异常都必须整体回滚。

## 4. 账实核对

流水按 `balance_version` 从 1 连续递增。每一条必须同时满足：

- `quantity_before` 等于上一条的 `quantity_after`；
- `quantity_after = quantity_before + quantity_delta`；
- 流水版本连续且最终版本等于余额版本；
- 最后一条流水数量等于余额表的现存数量。

审计函数只报告一致性，不修复或覆盖余额。发现异常时必须保留证据并停止后续库存写入。

## 5. 迁移与测试

新迁移链为：

```text
0005_product_media -> 0006_inventory_foundation
```

`0006` 只新增两张空表。真实数据库升级前仍需停止应用、保留数据库与 `uploads/` 备份，并先运行副本升级演练。

专项测试：

```powershell
python -m unittest -v `
    test_mall_domain.py `
    test_mall_governance.py `
    test_inventory_service.py `
    test_inventory_migration.py `
    test_schema_readiness.py `
    test_migration_upgrade_rehearsal.py `
    test_postgresql_migration.py
```

真实 PostgreSQL 上线前还必须在独立 `_test` 数据库验证同一 SKU 并发写、唯一幂等键和余额版本冲突；SQLite 测试不能代替该验收。
