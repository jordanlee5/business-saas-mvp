# 业务数据管理 SaaS

这是一个面向业务上传方与运营管理员的业务数据管理 SaaS。当前系统已经完成现金返现核销 MVP：从业务清单上传与承接，到凭证 OCR、两级审核、核销金额控制、经营统计、结算与凭证下载形成完整闭环。

当前项目主要用于本地演示、业务流程验证和后续迭代，不等同于已经完成生产级部署加固。

## 当前版本

- 版本：**v0.4.3-M4-1 — 商品目录数据基础**
- M0/v0.3.0 收口日期：2026-08-27
- 本轮修改前稳定代码基线：`313b04377df8722359eba033bc71112bb857ff16`
- 基线提交：`313b043 feat: add member points detail and export`

M0/v0.3.0 已完成现有版本、文档和商城规划收口，收口变化见 [CHANGELOG.md](CHANGELOG.md)。M1 已建立商城领域规则、迁移机制、PostgreSQL 验证、小程序 API 路由骨架、商城权限审计以及会员与积分核心表结构。M2 在上传时拆分现金返现和商城积分渠道，并保证商城记录不进入原有凭证链路。M3 已建立一次性激活码、微信会员绑定、积分账本、到期维护、人工纠错及管理员只读查询导出。M4-1 开始建立商品分类、供应商、商品和 SKU 数据基础；商品后台页面、媒体、库存、订单及小程序业务接口仍未开放。

## 当前产品边界与术语

- **现金返现核销**：当前已经实现的业务链路，包括凭证上传、OCR 匹配、初审、二级复核、核销金额控制、统计和结算。
- **本次核销金额**：一条匹配审核记录实际分配给对应业务的金额，不等同于整张凭证金额。
- **已通过凭证金额**：一条业务下最终审核通过的本次核销金额合计。
- **核销状态**：按已通过金额与业务金额区分未付款、部分付款、已付清及金额异常。
- **积分商城兑换**：已支持在上传时选择的另一条积分使用渠道；内部已具备受保护的激活、积分账本和商品目录数据基础，但尚未开放会员端领取 API、商品浏览或下单。

同一业务未来只能选择现金返现核销或积分商城兑换之一。商城规划不得让一条业务同时进入两条核销链路。规则状态见 [商城业务规则决策记录](docs/mall-business-rules-decisions.md)。

## 已完成功能

### 1. 账号、管理员与权限

- 管理员与上传方登录及数据隔离；
- 超级管理员、初审管理员、复核管理员和运营管理员分工；
- 管理员账号创建、编辑、启用与停用；
- 上传方账号创建、编辑和费率配置；
- 首次登录强制修改初始密码；
- 关键管理操作日志与权限拦截。

### 2. 业务上传、承接与数据管理

- 上传方通过 Excel 上传业务清单；
- 上传时必须选择现金返现或商城积分渠道，商城批次必须设置独立领取截止日；
- 现金返现每行必须填写姓名、手机号、车牌号、积分金额和银行卡号；商城积分允许银行卡号留空，其余字段仍全部必填；
- Excel 采用整批原子校验，任意一行校验失败时整份清单不写入数据库，也不生成上传通知；
- 批次和所属业务记录保存一致的不可变渠道快照；
- 上传批次按待承接、已承接和已拒绝管理；
- 承接或拒绝后仅在尚未产生下游业务时允许填写原因撤销；凭证审核、积分权益或商城领取处理一旦发生即禁止撤销；
- 只有已承接的现金业务进入经营统计、凭证识别和核销链路；商城业务保持在待激活权益链路；
- 批次、业务列表、详情、筛选和 Excel 导出显示积分使用渠道；
- 仅具备批次管理权限的管理员可在承接前纠正错选渠道，批次与全部业务记录同步修改并写入审计日志；
- 批次列表隐藏历史零成功批次，长文件名采用极简缩略展示并通过悬停查看完整名称；
- 公开业务单号生成、历史回填和新旧编号兼容搜索；
- 业务列表、详情、筛选、分页和 Excel 导出；
- 上传批次筛选、分页及上下文保持；
- 费率计算方式与历史费率快照隔离。

### 3. 凭证 OCR、匹配与两级审核

- PNG、JPG、JPEG 凭证上传及 OCR 文字、金额识别；
- 按姓名、银行卡号、金额等信息生成匹配候选；
- 凭证文件哈希去重及上传批次关联；
- 初审通过后进入待复核，复核通过后才成为最终已通过；
- 初审人与复核人不得为同一账号；
- 单条、批量和跨页批量审核；
- 已足额业务不再进入审核池；
- 单凭证禁止跨业务归属，并保留历史异常只读审计与受控修复工具。

### 4. 核销金额、状态与异常控制

- 每条审核记录保存本次核销金额；
- 同时校验业务剩余额度和凭证剩余额度；
- 阻止超额核销并对缺失金额、预占超额和已通过超额失败关闭；
- 统一展示业务金额、已通过凭证金额、剩余金额和核销状态；
- 业务列表、详情、经营数据和结算口径保持一致；
- 支持金额异常提示、筛选和统计下钻。

### 5. 统计、结算与凭证下载

- 管理员经营数据看板及 Excel 导出；
- 上传方结算报表及 Excel 导出；
- 外扣、内扣费率使用统一 Decimal 计算与四舍五入规则；
- 上传方只能下载与自身已承接业务相关、且最终审核通过的凭证；
- ZIP 目录和凭证文件名统一使用公开业务单号；
- 页面、筛选和导出时间统一使用 UTC+8，格式为 `YYYY-MM-DD HH:MM:SS`。

### 6. 通知、宣传页与界面

- 上传方成功上传业务后，向具备承接权限的管理员创建站内通知；
- 工作台右上角以堆叠通知卡片显示未读待办并支持跳转、标记已读；
- 保险公司宣传页后台配置、图片管理、草稿、发布、下线和预览；
- 已发布宣传页的公开访问路由；
- 管理员与上传方使用区分明确的常驻侧边栏和统一紧凑页面样式。

## 技术栈

- Python、FastAPI、Starlette
- Jinja2 Templates、HTML、CSS
- SQLAlchemy、SQLite
- pandas、openpyxl
- Pillow、pytesseract、RapidOCR

## 本地启动

以下命令在项目根目录执行。Windows PowerShell 用户先激活项目虚拟环境：

```powershell
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements-dev.txt
python -m alembic -c alembic.ini upgrade head
python -m app.init_admin
uvicorn app.main:app --reload
```

默认访问地址为 `http://127.0.0.1:8000`，健康检查地址为 `http://127.0.0.1:8000/health`。

以上 `upgrade head` 只适用于空数据库或已经按下文完成基线接入和副本演练的数据库；不得对来源不明的历史数据库直接运行。初始化管理员前请先检查 `app/init_admin.py` 的本地配置，不要把真实密码写入仓库或提交记录。应用启动与管理员初始化都会校验数据库必须处于当前 Alembic head，不再隐式建表。

## 小程序 API 路由骨架

M1 已建立独立于 `app/main.py` 页面路由的小程序 JSON API v1 入口。当前只开放不读取业务数据的版本状态端点：

```text
GET /api/miniprogram/v1/status
```

固定响应为 `status=ok`、`api_version=v1` 和 `service=mall-miniprogram-api`。该前缀不复用现有管理员 Cookie 会话；以后新增的激活、积分、商品或订单接口必须各自接入小程序短期令牌认证，不得因为位于该前缀下而默认公开。本轮没有增加任何小程序商城业务接口或会员端页面。

## 商城后台权限与审计基础

M1 已为商城后台敏感操作建立独立细权限函数和稳定审计动作类型；M3-5 首次接入会员积分只读页面与受审计导出：

- 超级管理员与运营管理员可管理商品、SKU、库存、订单和供应商，并生成待确认的供应商结算；
- 积分人工调整与供应商结算确认仅允许超级管理员；
- 初审管理员、复核管理员、上传方、缺少级别或未知级别的账号均没有商城写权限；
- 商品上下架、SKU、库存调整、订单取消/发货/退款、积分调整、供应商维护和结算操作均使用已登记的审计动作类型；
- 未知或尚未完成权限分级的审计动作失败关闭，不能默认继承传统运营权限。
- 超级管理员和运营管理员可查看会员积分并导出单会员对账明细；初审、复核、上传方及无效账号不能进入页面或导出；每次成功生成导出均写入稳定操作日志。

`OPEN-004` 已确认一期使用由上传方安全交付的一次性激活码，并为后续短信验证码保留统一安全因子接口。`OPEN-005` 已确认同一会员可以持有多个积分批次，各批次独立记录余额与到期日。M3-1 已实现一次性码校验后的首笔 `GRANT` 入账；M3-2 提供统一入账和按流水重算余额的审计能力；M3-3 新增 `EXPIRE` 到期扣减、默认只读的维护命令及即将到期查询；M3-4 新增仅限启用中超级管理员调用的 `ADJUST` 人工纠错领域服务；M3-5 新增管理员侧会员积分汇总、批次、流水及单会员 Excel 对账导出。预占、消费与退款仍在后续订单阶段实现。

会员积分页面入口为 `/member-points`。列表支持会员状态、会员公开编号、来源业务单号、姓名、手机号和车牌号检索；详情按到期时间展示独立积分批次，并同时显示账户缓存与不可变流水的一致性。页面客户信息脱敏且不显示微信 `openid/unionid`；Excel 导出不包含微信身份、激活凭据或流水幂等键，公式型文本会转义。具体运行边界见 [会员积分查看与导出说明](docs/member-points-operations.md)。

人工调整当前只提供内部领域服务，不增加后台页面、命令行或公网 API。正向调整只能把既有批次恢复到原始授予上限，负向调整只能扣减可用余额，不修改预占余额、原始授予额、到期时间或来源业务。调用、审计和后续接入要求见 [积分调整服务说明](docs/points-adjustment-service.md)。

积分维护命令：

```powershell
python -m app.points_expiry_task
python -m app.points_expiry_task --upcoming-days 30
```

以上命令只读，不执行迁移或扣分。执行扣减、分页、阻止原因和退出码见 [积分到期操作说明](docs/points-expiry-operations.md)。本轮没有安装计划任务；未来下单仍必须实时校验积分批次到期时间，不能仅依赖缓存余额或定时任务运行频率。

## 商城核心与商品目录表结构

`0002_mall_core_foundation` 在初始基线之后建立会员与积分核心结构；`0003_member_activation_security` 新增激活安全基础；`0004_catalog_foundation` 新增商品目录基础：

- `upload_batches` 新增渠道默认值和独立激活截止日，`business_records` 新增不可变渠道快照与商城领取状态；历史记录统一回填为 `CASH_REBATE`，领取状态保持空值；
- 新增 `members` 与 `member_wechat_bindings`，会员公开编号不承担登录凭证职责；
- 新增 `points_accounts`、`points_grants` 与 `points_ledger_entries`，积分统一为 `NUMERIC(18, 2)`，同一会员可以持有多个独立到期批次，同一来源业务只能生成一个积分批次；
- 渠道、领取状态、非负余额、有效期、流水类型、非零流水及人工调整审计要求均由数据库约束失败关闭；
- 新增 `member_activation_credentials`，一条商城业务只保留一个当前激活凭据；一次性码只保存随机盐和 PBKDF2-SHA256 摘要，不保存明文，支持重签版本、失败次数、锁定、使用、过期和撤销状态；
- 只有已承接、未过领取截止日且仍待激活的商城业务可以签发；现金业务、待承接/已拒绝批次和已生成积分权益的业务失败关闭；
- 激活时同时校验公开业务单号、手机号、车牌号、一次性码和服务端微信身份；预期失败返回统一文案，错误尝试可持久化并在达到上限后锁定；
- 成功激活在同一事务创建或复用微信会员、积分账户，新增独立积分批次与首笔不可变 `GRANT` 流水，并把业务及凭据置为已激活/已使用；同一业务的唯一约束和流水幂等键共同阻止重复入账；
- 首笔 `GRANT` 只能通过积分流水服务追加；服务同步更新账户和批次缓存，并在写入前后使用流水重算结果进行一致性校验，发现缓存漂移时失败关闭且不静默覆盖；
- 人工调整只追加 `ADJUST` 流水并关联管理员操作日志；同一请求号可安全重放，不允许越过批次原始授予上限、扣减预占积分、调整已到期或冻结批次；
- 管理员会员积分页面只读取已保存的账户、批次和流水；发现余额缓存漂移时显示异常但不修复。单会员导出包含汇总、积分批次和积分流水三个工作表，并记录导出审计；
- 新增 `product_categories`、`suppliers`、`products` 和 `product_skus`；商品必须归属分类，SKU 必须归属商品和供应商；稳定公开编号、名称、编码、状态及排序使用数据库唯一与检查约束失败关闭；
- 商品状态只允许草稿、上架和下架，上架必须记录发布时间；SKU 积分售价与人民币成本使用 `NUMERIC(18, 2)`，售价为正、成本和低库存阈值非负；
- `OPEN-006` 已确认为退款退回原积分批次，原批次已到期时进入人工处理且不自动延期；`OPEN-007` 已确认为第一版全部包邮。这两项只作为后续订单约束，本轮没有实现退款或运费代码；
- `SMS_OTP` 已登记为安全因子扩展值，但在短信供应商、发送频控和回执校验接入前由服务明确拒绝；
- 公网激活 API 尚未开放。未来会员端接口必须先完成微信登录态、请求级限流和审计，不能把 `openid` 当作客户端可自由填写字段；
- M4-2 已新增分类、供应商、商品与 SKU 的受控写服务：超级管理员和运营管理员可以创建与编辑目录，所有实际写入同步生成管理员操作日志；商品只能通过专用状态操作上下架，上架前必须具有启用分类和至少一个有效供应商 SKU；
- 商品与供应商公开编号及 SKU 编码保持稳定，价格、成本、排序、唯一字段和停用边界失败关闭。当前服务不自行提交事务，调用方必须整体提交或回滚；完整边界见 [商品目录写服务说明](docs/catalog-service.md)；
- 商品后台页面、HTTP 写接口、媒体、库存、订单、退款及供应商结算仍按后续切片独立实现。迁移后未调用写服务时四张目录表为空仍是预期结果；M4-1 数据结构边界见 [商品目录基础说明](docs/catalog-foundation.md)。

## 数据库、上传目录与迁移边界

- 应用从进程环境变量 `DATABASE_URL` 读取数据库连接地址；未设置或只包含空白时，仍使用项目根目录下的本地 SQLite 文件 `sqlite:///./saas_mvp.db`；
- SQLite 连接继续使用现有的跨线程兼容参数；其他数据库方言不会接收 SQLite 专属参数；
- 凭证及宣传页图片保存在本地 `uploads/` 目录；
- `saas_mvp.db`、`uploads/` 和 `.env` 均不应提交到 Git；
- 应用和管理员初始化不再调用 `create_all`；数据库不是当前 Alembic head 或版本标记与结构不一致时，启动会明确失败；
- 仓库根目录的 `add_*.py` 是历史阶段迁移脚本，不是统一迁移框架；
- 升级历史数据库前，必须同时备份 `saas_mvp.db` 与 `uploads/`，再按来源版本选择并验证所需迁移；
- 审计和修复脚本必须先以只读预演方式核对，不能当作日常迁移脚本批量执行；
- v0.3.0 本次文档收口没有数据库迁移，不需要运行任何 `add_*.py`。

进入商城订单、库存和积分并发扣减阶段前，需要建立可重复迁移机制和 PostgreSQL 集成测试。当前 SQLite 与本地文件目录只适合开发、演示和小规模业务验证；生产部署还需要数据库备份恢复、对象存储、访问控制、HTTPS、监控和并发验证。

M1 已建立数据库 URL 配置入口、Alembic 迁移环境、现有结构基线和商城核心 revision。`0001_current_schema_baseline` 只固化原有的 10 张业务表，`0002_mall_core_foundation` 新增商城渠道字段与会员积分核心表，`0003_member_activation_security` 新增激活凭据表，`0004_catalog_foundation` 新增商品目录四张表。迁移开发依赖包含 Psycopg 3 二进制驱动；PostgreSQL 离线迁移 SQL 纳入默认测试，真实连接验证则必须使用独立、可清空且名称以 `_test` 结尾的测试数据库。

迁移开发环境使用单独的依赖入口：

```powershell
python -m pip install -r requirements-dev.txt
python -m alembic -c alembic.ini heads
python -m unittest -v test_migration_environment.py
python -m unittest -v test_initial_schema_migration.py
python -m unittest -v test_mall_core_migration.py
python -m unittest -v test_member_activation_migration.py
python -m unittest -v test_member_activation_service.py
python -m unittest -v test_points_ledger_service.py
python -m unittest -v test_points_expiry.py
python -m unittest -v test_points_adjustment_service.py
python -m unittest -v test_member_points_service.py test_member_points_routes.py
python -m unittest -v test_catalog_foundation_migration.py test_mall_domain.py
python -m unittest -v test_catalog_service.py test_mall_governance.py
python -m unittest -v test_migration_upgrade_rehearsal.py
python -m unittest -v test_schema_readiness.py
python -m unittest -v test_postgresql_migration.py
```

`test_postgresql_migration.py` 默认只验证 PostgreSQL 配置安全规则和离线迁移 SQL，不连接外部数据库。需要执行真实 PostgreSQL 迁移往返测试时，必须使用独立的空测试库，并在当前 PowerShell 进程中显式授权：

```powershell
$env:POSTGRES_TEST_DATABASE_URL = "postgresql+psycopg://tester:replace_me@127.0.0.1:5432/business_saas_test"
$env:POSTGRES_TEST_ALLOW_RESET = "1"
python -m unittest -v test_postgresql_migration.PostgreSQLMigrationIntegrationTests
Remove-Item Env:POSTGRES_TEST_DATABASE_URL
Remove-Item Env:POSTGRES_TEST_ALLOW_RESET
```

测试入口会拒绝非 `postgresql+psycopg` 地址、名称不以 `_test` 结尾的数据库，以及与当前 `DATABASE_URL` 相同的数据库。它会在测试库中执行 `upgrade head`、结构一致性检查和 `downgrade base`，因此严禁填写开发共享库或生产库。

`migrations/env.py` 复用 `DATABASE_URL` 并加载当前 SQLAlchemy metadata。空数据库可执行 `upgrade head` 建立当前结构；尚未接入 Alembic 的已有业务数据库不得直接执行 `upgrade` 或 `stamp`，应先运行以下默认只读检查：

```powershell
python -m app.migration_baseline
```

检查通过后仍需先备份数据库与 `uploads/`，并审阅数据库来源和检查结果；只有明确需要接入基线时，才运行 `python -m app.migration_baseline --apply`。该命令再次校验原始结构，只写入 `0001_current_schema_baseline` 版本标记，不创建、删除或改写业务表。历史 SQLite 兼容范围只包含已经审计并固化的类型、默认值、索引和外键差异，同时会校验审核人引用、凭证上传批次引用、布尔值与费率模式；任何未知差异或异常数据都会拒绝写入版本标记。工具只支持包含 0001 的单一线性迁移链，并只会接入初始基线，禁止绕过它直接执行 `alembic stamp`。`alembic downgrade base` 只允许用于无业务数据的临时测试库，严禁对现有业务数据库执行。

在考虑对已有数据库写入版本标记前，必须先停止应用并运行副本演练：

```powershell
python -m app.migration_rehearsal
```

该命令只支持本地 SQLite：它会在 `database_backups/migration_baseline_rehearsals/` 中分别保存未改动的原始快照和仅用于写入基线标记的演练副本，校验 SQLite 完整性，并比较排除 `alembic_version` 后的业务结构与数据指纹。源数据库不会写入版本标记；演练通过也不代表已经获准操作真实数据库。应用运行中或业务指纹发生变化时，本次结果无效。

已经稳定停留在 `0001_current_schema_baseline`、`0002_mall_core_foundation` 或 `0003_member_activation_security` 的 SQLite 数据库，在升级到当前结构前必须停止应用，并运行：

```powershell
python -m app.migration_upgrade_rehearsal
```

该命令会在 `database_backups/mall_core_upgrade_rehearsals/` 保存升级前快照和独立演练副本，只对演练副本重复执行 `upgrade head` 与结构漂移检查。它逐表记录源版本的原字段定义、行数和字段值指纹，确认升级后全部不变，并核验演练副本真实处于 `0004_catalog_foundation`。源库版本和业务指纹保持不变才会通过。审阅输出并按需使用演练副本完成验证后，才可另行批准对真实库执行 `python -m alembic -c alembic.ini upgrade head`；数据库文件与 `uploads/` 备份必须继续保留。

## 测试基线

在当前 M4-1 工作副本上，完整依赖环境中的回归命令为：

```powershell
python -m compileall app migrations
python -m unittest discover -v
```

- Python 静态编译：通过；
- 全量单元测试：`Ran 432 tests ... OK (skipped=1)`；本轮新增 7 项商品状态、目录迁移、数据库约束、结构就绪及 `0003` 副本升级测试；未配置独立 PostgreSQL 测试库时，只跳过真实连接往返测试；
- `test_ocr_env.py` 还会检查本机 OCR 依赖；若没有测试图片，只会提示文件不存在；
- 每轮功能提交仍需执行相关专项测试、全量测试和对应页面冒烟测试；
- 现金返现链路的回归测试必须长期保留，商城开发不得减少或绕过现有测试。

## 项目结构

```text
business-saas-mvp/
├─ app/
│  ├─ main.py                       # FastAPI 应用与现有页面路由
│  ├─ api/                          # 版本化小程序 JSON API 路由
│  ├─ models.py                     # SQLAlchemy 数据模型
│  ├─ admin_permissions.py          # 管理员权限规则
│  ├─ mall/                          # 商城领域、审计动作与规则骨架
│  ├─ match_review_workflow.py      # 初审、复核与冲突控制
│  ├─ voucher_allocation.py         # 核销金额计算与边界校验
│  ├─ settlement_calculator.py      # 统一费率及结算计算
│  ├─ notification_service.py       # 工作台业务上传通知
│  ├─ promotion_page_service.py     # 宣传页业务规则
│  ├─ migration_baseline.py          # 已有数据库的只读检查与安全接入
│  ├─ migration_rehearsal.py         # SQLite 副本基线接入演练
│  ├─ migration_upgrade_rehearsal.py # SQLite 副本 0001→head 升级演练
│  ├─ schema_readiness.py            # 启动时数据库版本与结构校验
│  ├─ templates/                    # 服务端页面模板
│  └─ static/                       # 样式、脚本与图片资源
├─ docs/
│  ├─ catalog-foundation.md         # M4-1 商品目录数据边界与迁移说明
│  ├─ catalog-service.md            # M4-2 商品目录写服务与状态边界
│  └─ mall-business-rules-decisions.md
├─ migrations/                     # Alembic 环境与后续迁移版本
├─ alembic.ini                     # Alembic 项目配置
├─ add_*.py                         # 历史阶段数据库迁移脚本
├─ audit_*.py / repair_*.py         # 历史异常审计与受控修复工具
├─ test_*.py                        # 单元、迁移、审计和环境测试
├─ requirements.txt
├─ requirements-dev.txt             # 迁移开发工具依赖
├─ CHANGELOG.md
└─ README.md
```

## 后续路线

旧路线图功能已完成，历史核销异常审计已经收口，不再扩展。后续开发从 M1/v0.4.0 开始，依次推进商城领域骨架与迁移基础、上传渠道拆分、会员激活与积分账本、商品库存、纯积分订单、履约结算和微信小程序；阶段顺序与验收门槛以已确认的新路线图为准。
