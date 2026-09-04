# 数据库迁移目录

本目录由 Alembic 管理。迁移版本统一放入 `versions/`，并必须同时提供可审阅的 `upgrade()` 与 `downgrade()`。

`0001_current_schema_baseline` 固化稳定基线 `ee34b97` 的 10 张既有业务表，不包含商城字段或数据变更。空数据库可通过它重建当前结构；已有数据库必须先运行 `python -m app.migration_baseline` 做只读结构检查，完成备份与审阅后才允许使用 `--apply` 写入版本标记。`downgrade base` 只用于无业务数据的临时测试库，严禁对已有业务数据库执行。

`0002_mall_core_foundation` 新增上传批次与业务记录的商城渠道字段，并建立会员、微信绑定、积分账户、积分批次和积分流水五张核心表。历史批次与业务记录全部回填为 `CASH_REBATE`。真实 SQLite 数据库从 0001 升级前，必须停止应用并运行 `python -m app.migration_upgrade_rehearsal`；只有副本升级、历史原字段数据指纹、重复升级与结构漂移检查全部通过后，才可另行批准真实升级。

`0003_member_activation_security` 新增商城业务激活凭据表，激活码仅保存带随机盐的版本化摘要，并由数据库约束安全因子、生命周期、错误次数、重签版本和有效期。升级演练同时支持从 `0001_current_schema_baseline` 或 `0002_mall_core_foundation` 到当前 head；源库与升级前快照不会被演练修改。

已有 SQLite 数据库在考虑写入版本标记前，还必须停止应用并运行 `python -m app.migration_rehearsal`。演练只修改单独副本，并保留原始快照；演练通过不等于获准修改真实数据库。历史兼容画像只接受已审计的等价类型、默认值、三个索引和三个外键差异，并要求审核人、凭证上传批次、布尔值与费率模式完整性检查全部通过。

应用启动不再执行 `Base.metadata.create_all()`。当前代码只接受真实处于 `0003_member_activation_security` 且必需表、字段完整的数据库，版本落后或虚假 stamp 都会失败关闭。
