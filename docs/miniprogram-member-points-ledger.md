# M7-20 会员积分流水私有分页接口

`GET /api/miniprogram/v1/points/ledger?page=1&page_size=20` 只接受有效的会员 Bearer 会话。每次读取重新核对绑定、AppID 和会员启用状态；管理员 Cookie 不能代替会员令牌。`page` 范围 1–1000000，`page_size` 范围 1–50，默认分别为 1 和 20。越界参数由 FastAPI 返回 422。

只读取当前会员账户下的积分流水，按创建时间降序、内部流水 ID 降序稳定分页。响应为 `items`、`page`、`page_size` 和 `total`。每条仅返回 `entry_type`、`available_points_delta`、`reserved_points_delta`、`created_at`；变化量采用带正负号的两位小数字符串，时间带 `+08:00`。类型包括 `GRANT`、`RESERVE`、`RELEASE`、`CONSUME`、`REFUND`、`EXPIRE`、`ADJUST`。空账户有账户记录时返回空列表，不合并不同会员的流水。

不暴露数据库 ID、批次 ID、幂等键、业务编号、引用、管理员、调整原因、姓名、电话、车牌、微信身份或激活码。读取前用不可变流水核对账户和全部批次缓存；账户不存在、缓存漂移或重算失败时返回通用 503。接口不写入、修复或执行到期维护。响应设 `Cache-Control: no-store`，使用现有只读请求会话的 rollback/close 生命周期。

本轮不修改数据库结构和小程序页面。没有真实已绑定会员时，运行态只验证无令牌 401 和既有权限回归；真实会员正向流水留待有合法 AppID、HTTPS 域名与会员数据后独立验收，记为 `DEFERRED`，不可用管理员 Cookie 或伪造凭证代替。
