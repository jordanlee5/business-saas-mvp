# M7-16 会员积分批次私有列表

`GET /api/miniprogram/v1/points/grants?page=1&page_size=20` 只接受 M7-14 签发的有效 Bearer 会话。每次请求重新验证绑定、AppID 和会员启用状态；管理员 Cookie 不授予访问权。`page` 为 1 至 1000000，`page_size` 为 1 至 50，默认分别为 1 和 20。

只查询当前会员账户的批次，按激活时间降序、内部 ID 降序稳定分页。响应包括 `items`、`page`、`page_size`、`total`；每项仅含 `granted_points`、`available_points`、`reserved_points`、`activated_at`、`expires_at` 与存储状态 `status`。积分为两位小数字符串，日期明确使用 `+08:00`。不返回数据库 ID、业务编号、姓名、电话、车牌、微信身份、激活码或管理员审计内容。没有批次时返回空列表和 `total=0`。

读取前以不可变流水核对账户和全部批次的缓存余额。账户缺失或任一缓存漂移返回通用 503，不泄露余额或试图修复。本接口不执行到期维护；`status` 是已存储状态，过了 `expires_at` 不会由读取动作自动改变。响应设置 `Cache-Control: no-store`。不新增迁移、不写真实数据库，也不改变现有访客小程序页面。真实微信登录、权益激活和客户端令牌管理留待独立阶段验收。
