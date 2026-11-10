# 测试环境周期备份准备

2026-11-10，从 `8b37687` 接续。周期备份执行工具和 systemd 示例已完成本机隔离验证，**尚未在云服务器安装或开启定时任务**。本轮不改真实业务库、不开放公网入口、不提交微信审核。

## 云端安装更新（2026-11-10）

从 `3037c8d` 接续的云端安装已完成，上述“尚未安装”描述为上一轮状态。现已启用凌晨定时备份与独立巡检，外部企业微信接入和首轮到点执行仍待验证。详见[云端备份告警](cloud-backup-watch.md)。下文保留通用部署示例。

## 执行行为

`tools.staging_backup_job` 调用现有 PostgreSQL 备份工具，先检查应用和数据库健康、磁盘余量，再短暂停止应用、导出数据库并复制业务文件，校验清单与摘要后恢复应用。只有应用和数据库恢复健康才登记成功。它不是不停机备份，须安排在无人测试的时段。

- 仅接受 `saas-pg-test-*` 项目，拒绝外部 `DATABASE_URL`；沿用项目所属容器、卷和数据库身份核对。
- 备份根目录必须在代码仓库外，并绑定一个项目；禁止符号链接。Linux 目录权限为 700，结果文件为 600。
- 同一备份根目录使用操作系统文件锁。重复触发会跳过，进程退出会释放锁；不删除锁文件。
- 每轮生成新目录，不覆盖旧备份，不自动删除备份。失败目录保留供检查。
- `runs/<运行编号>.json` 留存每次结果；`last-run.json` 是最近一次结果；`last-success.json` 只指向校验成功且服务恢复健康的备份。后续失败不替换上一次成功记录。
- 日志只输出固定阶段码，不输出数据库密码、私钥、环境变量或原始异常文本。备份本身包含业务数据和签名密钥，必须留在私有目录，不能提交 GitHub。

## 手动执行一次

以下是部署示例，路径、项目和镜像须按目标环境核对。不能把示例当作已安装状态。

```sh
export STAGING_SECRET_DIR=/srv/saas-private/staging-secrets
export STAGING_APP_IMAGE=your-locally-available-tested-image
python3 -m tools.staging_backup_job \
  --project saas-pg-test-first \
  --root /srv/saas-backups/scheduled/saas-pg-test-first
```

默认运行前要求至少 5 GiB 空闲，恢复健康等待最多 120 秒；可用 `--min-free-gib`、`--health-wait` 调整。空闲阈值不是备份大小预估，仍须监测实际增长。

| 退出码 | 含义 | 处理 |
| --- | --- | --- |
| 0 | 备份校验和服务恢复均成功 | 核对 `last-success.json` |
| 1 | 磁盘不足、备份或服务恢复失败 | 查看固定阶段码、目标项目状态，保留上一恢复点 |
| 2 | 配置无效或状态文件无法写入 | 核对私有目录、项目名、配置与权限 |
| 75 | 同一目录已有任务运行 | 不启动另一份任务，确认现有任务进度 |

恢复时读取 `last-success.json` 的 `backup` 相对路径，再用原有 `tools.staging_pg_backup verify/restore` 检查并恢复到**全新项目**。只读巡检 `tools.staging_monitor` 的备份参数目前仍需提供这一具体备份目录，不能直接传计划任务根目录。

## Ubuntu 定时任务示例（后续安装）

模板位于 `deploy/staging-postgres/saas-staging-backup@.service` 和对应 `.timer`。模板假定经过核对的代码在 `/srv/saas-staging/current`，每个项目的私有配置在 `/srv/saas-private/backup-<项目名>.env`。此文件由管理员创建为仅 root 可读，内容只引用现有秘密目录，不复制密码到代码：

```text
STAGING_SECRET_DIR=/srv/saas-private/staging-secrets
STAGING_APP_IMAGE=your-locally-available-tested-image
```

如只安装了独立 Compose 程序，再填写实际 `STAGING_COMPOSE_BIN` 绝对路径。不要使用 `export`；不要添加外部 `DATABASE_URL`。镜像必须与在运行的测试应用一致。更新部署版本时同步核对工作目录和镜像。

安装时以 644 权限复制两个模板到 `/etc/systemd/system/`，私有配置为 600；先执行 `systemd-analyze verify`、`systemctl daemon-reload`，再手动运行一次对应 service 并核对成功记录，最后才启用 timer。不要在测试体验期间手动触发停机备份。

```sh
sudo systemctl start saas-staging-backup@saas-pg-test-first.service
sudo journalctl -u saas-staging-backup@saas-pg-test-first.service --no-pager -n 30
sudo systemctl enable --now saas-staging-backup@saas-pg-test-first.timer
sudo systemctl list-timers 'saas-staging-backup@*'
```

模板每天北京时间 04:00 后随机延迟最多 10 分钟，`Persistent=false`，服务器白天启动时不会补跑并打断测试。错过备份需靠巡检发现。停止后续计划运行可用 `systemctl disable --now ...timer`，这不会取消已在执行的 service。

## 验证及边界

[脱敏证据](validation/staging-scheduled-backup-20261110.json)：26 项专项及兼容测试通过；本机独立 Docker 项目以虚拟会员、SKU、积分和文件完成 7 个备份/故障/恢复检查点。演练使用已有 `b4f939a` 应用镜像及本轮主机工具，不代表最新应用全部功能或云服务器已经复验。演练容器结束后停止，私有备份和卷保留；未发布容器端口。

模板已做语法和北京时间调度表达式验证，未安装到本机或服务器。Windows 挂载盘权限与 Linux 不同，实际安装必须使用上述 Linux 文件权限。

当前仍待推进：云端安装与首轮值守、周期巡检和通知接入、备份自动保留策略、异机副本与定期恢复演练。备份不包含数据库密码目录、HTTPS 证书卷或其他仓库外配置，须另外受限保管。

锁仅保护使用**同一个根目录**的本工具调用；手动备份、恢复、部署和另一个根目录的任务必须错开。底层 Docker 操作目前没有整体超时，模板不会在应用暂停期间强杀任务；若运行长期停滞，先检查服务和磁盘，不能直接删除锁文件。进程被强杀或主机掉电后，记录可能停留在 `running`，应用也可能仍停止，应人工核对再恢复运行。本轮没有自动修复、后台通知或自动清理。
