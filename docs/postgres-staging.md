# PostgreSQL 测试部署、备份与恢复

2026-11-05，最初从 `96003b7` 接续，后续加入共享会话与 OCR 隔离。仅用于独立测试环境，真实业务库继续只读，不提交微信审核。当前用户按指南准备服务器和域名，采购建议以[最新指南](test-server-purchase.md)为准。

## 给业务方的说明

本轮把上一轮验证过的 PostgreSQL 业务路径做成可重复部署的测试包，并提供配套备份恢复工具。数据库、商品图片和私有凭证分别存放；备份时暂时停止应用写入，将数据库和附件保存为同一恢复点。恢复时建立一套全新的测试环境，检查成功后才能考虑切换访问入口。

目前由用户准备云账号、公司域名和平台本人核验，服务器按 4 核 8GB 测试方案选配。开通后补目标服务器上的图片、OCR 混合负载及公网复测。当前包不会自动搬迁电脑里的真实业务数据；密码和私钥保管在用户本机。

## 部署结构与保护

入口在 `deploy/staging-postgres/compose.yaml`，与原 SQLite 测试包独立。应用通过 `python -m tools.staging_postgres serve` 启动，数据库为 PostgreSQL 16。共享会话小轮增加 `STAGING_WEB_WORKERS`，默认 1，允许 1～4；虚拟环境已验证两个 worker。[共享会话说明](shared-browser-sessions.md)

- 数据库只加入内部 Docker 网络；数据库和应用均不发布主机端口。启用 `https` profile 后，只有 Caddy 网关发布 80/443。
- OCR 默认在独立 `ocr` 服务执行，应用 2 CPU / 4GiB、数据库 1 CPU / 1GiB、识别服务 1 CPU / 1GiB。OCR 只加入内部 recognition 网络，没有业务卷或数据库秘密资料；每次只处理一张图片，超时终止。应用启动等待其健康，运行中识别故障不停止会员服务。[隔离说明与验证](ocr-isolation.md)
- 数据库管理账号与应用账号使用不同随机密码，通过 Compose secrets 文件提供。应用角色不能创建角色、数据库，也不是数据库超级用户。
- 秘密文件保存在仓库外的私有目录。目录权限 0700；文件为 0444，以便只有被授权挂载的容器内用户读取。宿主目录仍应只允许部署账号访问；Compose 文件挂载不等于秘密已加密。
- 数据库使用项目独立命名卷；`/data` 保存上传文件、会员会话签名密钥和配送配置。应用签名密钥需纳入备份，不能每次重启重新生成。
- 初始化只接受固定的 `database:5432/saas_staging` 与 `saas_staging` 应用角色，拒绝继承外部 `DATABASE_URL`。在数据库锁内核对空库，再建立结构；已有库或有资料的卷不允许重新初始化。
- 启动检查数据库身份、角色权限、结构版本、签名密钥和管理员密码。首次创建管理员必须隐藏输入密码，第一次登录需修改密码。
- 从 `0017_backoffice_sessions` 起，后台登录会话保存在共享数据库，普通进程重启保留有效登录。恢复历史备份时会清空后台会话，运营重新登录；会员签名密钥保留后，尚未过期的会员令牌继续按原规则验证。

参考：[Docker Compose secrets](https://docs.docker.com/compose/how-tos/use-secrets/)、[PostgreSQL 16 pg_dump](https://www.postgresql.org/docs/16/app-pgdump.html)。数据库导出使用一致性快照；为同时保证附件一致，本工具仍停止应用写入后再备份。

## 开发方操作步骤

以下从仓库根目录执行，要求 Linux、Python 3、Docker Engine 与 Compose 已可用。所有项目名使用 `saas-pg-test-` 前缀；命令中的目录是测试服务器上的仓库外私有位置，不指向已有业务资料。

### 首次部署

```sh
python3 -m tools.staging_pg_backup secrets /srv/saas-private/pg-test-secrets
export STAGING_SECRET_DIR=/srv/saas-private/pg-test-secrets
export STAGING_APP_IMAGE=business-saas-pg-staging:local
docker compose -p saas-pg-test-first -f deploy/staging-postgres/compose.yaml build app
docker compose -p saas-pg-test-first -f deploy/staging-postgres/compose.yaml up -d --wait database
docker compose -p saas-pg-test-first -f deploy/staging-postgres/compose.yaml run --rm app python -m tools.staging_postgres initialize
docker compose -p saas-pg-test-first -f deploy/staging-postgres/compose.yaml run --rm app python -m tools.staging_postgres admin
docker compose -p saas-pg-test-first -f deploy/staging-postgres/compose.yaml up -d --wait app
```

路径须由部署账号创建且有权限。密码由工具随机生成，不需在聊天、命令行或 Git 中填写。管理员密码在交互终端隐藏输入。若初始化中途失败，保留现场检查；工具不会覆盖旧密钥或自动清空数据库。

应用启动后才配置实际域名、备案接入及防火墙。设置 `STAGING_DOMAIN` 为已核对的测试子域名，确认 DNS 和 80/443 可达后启用网关：

```sh
export STAGING_DOMAIN=mall-test.example.com
docker compose -p saas-pg-test-first -f deploy/staging-postgres/compose.yaml --profile https up -d --wait
```

示例域名必须替换。本轮只在内部容器网络验证 PostgreSQL 部署，未重新声称取得公网 HTTPS 证据。网关沿用既有配置，目标服务器还需核对真实证书、续期及手机访问。

该配置默认未接入真实微信秘密资料。正式会员微信换码、隐私状态及受托方配置仍按独立接入步骤核对；不要把本轮虚拟令牌验证当作真实微信平台验收。

### 备份

```sh
python3 -m tools.staging_pg_backup backup /srv/saas-backups/test-20261105 --project saas-pg-test-first
python3 -m tools.staging_pg_backup verify /srv/saas-backups/test-20261105
```

备份目录必须是尚不存在的仓库外目录。工具核对项目拥有的卷、容器标签和数据库身份，停止应用后用数据库容器内同版本 `pg_dump` 导出，再复制 `/data`。完成后写入文件大小与 SHA-256 清单；原本在运行的应用会重新启动。失败目录保留但不应视为成功恢复点。证书卷、数据库秘密目录和后续微信秘密资料需另外受限备份，不在此应用恢复包中。

OCR 服务仅处理临时图片，不直接写业务库和业务卷，没有需要备份的任务队列。停止应用后 OCR 即使尚在结束本次计算也不会另写凭证。恢复启动应用时 Compose 会同时建立独立 OCR 服务；原上传中断的图片仍需运营重传。

本机备份只能解决误操作恢复，不能替代服务器外的备份。实际部署时须另留异机副本、限制读取权限，并安排周期恢复演练。本轮未替用户开通存储或上传任何备份。

### 恢复到新项目

使用与备份记录完全相同的应用镜像 ID。若本地标签被更新，先取回该恢复点对应镜像并将 `STAGING_APP_IMAGE` 指向它。数据库密码可以重新生成；会员签名密钥从备份恢复。

```sh
python3 -m tools.staging_pg_backup secrets /srv/saas-private/pg-restored-secrets
export STAGING_SECRET_DIR=/srv/saas-private/pg-restored-secrets
python3 -m tools.staging_pg_backup restore /srv/saas-backups/test-20261105 --project saas-pg-test-restored
```

工具先核对备份清单，不允许缺文件、额外文件、摘要改变或符号链接。目标必须没有容器和卷；同名未标记的卷也拒绝使用。空白数据库通过 `pg_restore --single-transaction --exit-on-error` 恢复，恢复对象归属应用角色。文件复制完成后，临时维护容器只在新项目的 `/data` 卷修复账号归属；正常应用仍不拥有这些维护权限。核对文件摘要和启动前检查通过后再启动应用。

恢复不更改源项目、源备份或域名。新版本在恢复完成后、应用启动前清空后台会话表，避免已经注销的旧登录随历史数据复活；不清理业务记录。失败时保留新目标供检查，不自动删除或覆盖后重试。恢复成功也必须复核后台重新登录、商品图片、会员订单与积分、私有凭证权限，再安排独立的访问入口切换。

## 本轮留证

验证包括全新部署、初始化重复拒绝、缺管理员拒绝、数据库及应用容器重建、备份到全新项目恢复，以及恢复后继续创建与取消订单。数据库按表核对全部业务行摘要，附件逐文件核对摘要；会员令牌、后台重新登录、公开商品图、授权凭证读取及其他会员拒绝均复验。

[本轮验证记录](validation/postgres-staging-20261105.json) · [并发容量说明](concurrency-readiness.md)
