# 数据库迁移目录

本目录由 Alembic 管理。迁移版本统一放入 `versions/`，并必须同时提供可审阅的 `upgrade()` 与 `downgrade()`。

`0001_current_schema_baseline` 固化稳定基线 `ee34b97` 的 10 张既有业务表，不包含商城字段或数据变更。空数据库可通过它重建当前结构；已有数据库必须先运行 `python -m app.migration_baseline` 做只读结构检查，完成备份与审阅后才允许使用 `--apply` 写入版本标记。`downgrade base` 只用于无业务数据的临时测试库，严禁对已有业务数据库执行。
