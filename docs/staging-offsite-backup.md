# 自动异机备份与保留轮换

2026-11-11，从 `1802c08` 接续。用户暂无 COS 或第二台常在线服务器，明确选择备份到管理电脑。本轮已安装并验证云端已有恢复点经 SSH 自动拉到 Windows 私密目录，不暂停验收应用、不连接真实业务库、不开放公网域名。

## 已安装行为

| 项目 | 当前设置 |
| --- | --- |
| 云端原有备份 | 每日北京时间 04:00 后随机延迟不超过 10 分钟；本轮未改时间，也未手动触发原应用备份 |
| Windows 任务 | `SaaS-Staging-Offsite-Backup`，每小时一次，并在当前用户登录时触发；错过后允许补跑 |
| 运行条件 | 电脑联网、开机且安装任务的 Windows 用户已登录；锁屏可以运行，注销、关机、休眠期间不能运行 |
| 拉取内容 | 最近一次成功的完整恢复点，含数据库导出、业务文件和清单；电脑恢复运行时补拉最新点，不逐一补拉离线期间所有历史点 |
| 本机保留 | 最近 14 个校验通过的恢复点，同一个恢复点重试不重复创建 |
| 云端保留 | 最近 7 个已收到本机校验回执的恢复点；未获回执的点保留，不因电脑离线自动删除 |
| 异机告警 | 来源备份时间或异机成功回执任一超过 36 小时，或回执缺失/无效，接入现有企业微信故障与恢复通知 |

Windows 安装目录在仓库外，子目录 `copies` 保存备份，`runner` 保存运维代码，`config.json` 只保存连接参数与密钥路径。目录 ACL 限当前用户、SYSTEM 和管理员；SSH 私钥继续使用原本机文件，未复制到服务器或仓库。传输强制校验主机指纹，非交互执行。这里使用访问权限保护，并非新增磁盘加密。

## 完整性及轮换保护

先在临时目录接收，只接受普通文件/目录；拒绝路径穿越、链接、重复条目、混合恢复点及超限传输。对所有清单文件核对摘要、项目身份和文件集合，通过后才原子发布，并更新成功记录。失败保留上一成功副本，错误记录不输出连接命令或凭据。

云端收到匹配清单摘要的回执后才允许清理旧副本。本机完成回执后再轮换。云端导出、回执和夜间备份共用锁；撞上正在备份时跳过，下一次重试。不会为拉取而停止应用。未知目录、损坏点、未确认点不在自动删除范围内，因此“7/14”不是磁盘用量硬上限。低磁盘告警继续生效，异常保留点须人工检查。

2026-11-11 从 `05c03e1` 补齐重复传输优化：先经已验证主机身份的 SSH 获取最新恢复点编号和清单摘要。来源端仍在备份锁下核验文件；本机已有同一恢复点时重新核验所有文件及清单摘要，完整才跳过备份包下载并发送回执。新恢复点仍完整下载，不是差异备份。小量 SSH 控制通信和两端磁盘校验仍存在，不能把 `archive_bytes: 0` 理解成完全无网络/磁盘开销。

已有副本缺文件、内容损坏或摘要冲突时失败并保留现场，不自动覆盖、不发送成功回执、不轮换。详情见本机 `last-run.json`；当前不额外立即发群消息，持续无法获得有效回执会由云端 36 小时异机过期巡检告警。旧来源点不会因重复确认而掩盖过期。探测后出现新恢复点时，下载并核验实际收到的完整点，再确认对应编号。

默认传输上限 20 GiB、传输截止 15 分钟、至少预留 1 GiB 空闲。2 Mbps 出网下新备份大文件仍可能超时，数据增加后应评估备份大小和窗口，再升级常在线存储。不得把本轮小数据验证视为容量保证。

## 验证及证据

见[脱敏验证证据](validation/staging-offsite-backup-20261111.json)。

- Windows 首次手动任务成功，随后 16:42:37（北京时间）自然定时触发也成功，退出码 0；最新回执 16:42:40。
- 云端真实已有备份拉到本机后清单与摘要校验通过。原验收应用容器 ID、启动时间保持一致，健康，公网端口绑定仍为空；备份和巡检计时器继续启用。
- 独立本机 Docker PostgreSQL 项目使用虚拟会员、SKU、积分、文件生成备份，再从接收目录恢复到新项目。会员 1、SKU 2、积分 170、文件标记、健康及登录页面均一致；恢复后兑换预占、重复请求幂等、取消返还积分/库存通过。
- 故障/恢复通知逻辑使用虚拟时钟和捕获发送器验证，未制造云端真实故障，也未在本轮重新发送企业微信群演练消息。现有真实通知送达已由上一轮用户确认。
- 31 项组合测试：Windows 通过（1 项平台专属跳过），WSL Linux 全部通过；PowerShell 安装器语法检查通过。

**范围说明：** 实际云端副本本轮完成跨机传输和完整性验证；恢复演练使用另建的本机虚拟源和目标。云端应用镜像与本机同名镜像的内容 ID 不同，没有把本机演练冒充实际云端副本恢复。

## 重复传输优化验证（2026-11-11）

见[本轮脱敏证据](validation/staging-offsite-efficient-20261111.json)。新增 9 项测试，组合测试共 40 项：Windows 39 项通过、1 项平台专属跳过；Linux 40 项全部通过。覆盖首次下载、复用、新恢复点、来源/本机损坏、缺文件、非法描述、并发备份锁及回执失败重试。

独立 PostgreSQL 虚拟演练通过 8 个检查点；实际子进程协议首次传输完整备份，再次校验后备份包传输量为 0。接收副本恢复到独立新项目后，会员、SKU、积分、业务文件及兑换/幂等/取消返还均正常。故障和恢复通知以捕获发送器验证，不打扰真实群。

服务器运维文件先升级，Windows 私密 runner 后升级；仅原子替换 `staging_offsite_backup.py`，保留旧版 `.previous`。Windows 已安装任务于北京时间 17:01:52 手动触发成功，退出码 0，记录 `reused_verified`、`archive_bytes: 0`，未删除恢复点。八项云端巡检正常；验收应用 ID 和启动时间不变，端口绑定仍为空，夜间备份及巡检计时器继续运行。未重新执行夜间停机备份。

旧服务器尚不支持 `describe` 时新客户端会报探测失败，因此升级应服务器先、客户端后。回退先还原客户端，再还原服务器运维文件，不改业务配置或数据库。

## 复现与安装

只在隔离环境运行，不能传入真实数据库配置：

```sh
python -m unittest test_staging_offsite_backup test_staging_backup_job test_staging_backup_watch -q
TMPDIR=<仓库外持久私密目录> STAGING_COMPOSE_BIN=/usr/local/bin/docker-compose python3 -m tools.rehearse_offsite_backup --image <已验证的本机测试镜像>
```

演练新建两个随机命名的 `saas-pg-test-offsite-*` 项目，退出时只停止这两个项目；私密原始证据与数据卷保留，路径在命令末尾输出，不提交 GitHub。TMPDIR 应预先建立并限制权限；不要依赖 WSL 的 /tmp 跨会话保留，本轮持久演练目录在 WSL /root/saas-rehearsal-evidence 下。

Windows 安装器为 `deploy/staging-postgres/install-offsite-pull.ps1`。参数使用实际本机路径：

```powershell
./deploy/staging-postgres/install-offsite-pull.ps1 `
  -PrivateRoot '<仓库外新建私密目录>' -Python '<可用python.exe完整路径>' `
  -KeyFile '<已有SSH私钥完整路径>' -KnownHosts '<已验证的known_hosts文件>' `
  -SshHost '<登录用户@测试服务器>'
```

安装器拒绝覆盖已有目录或同名任务。它不保存 Windows 密码，使用当前用户登录会话；没有创建无人登录也运行的服务。需要已验证服务器主机指纹，以及该 SSH 用户现有的免交互 sudo 权限。当前以既有管理员 SSH 身份拉取；后续正式环境可改为仅允许导出/回执的专用账户。

服务器运维目录 `/srv/saas-staging/offsite-ops-20261111/tools` 安装同版 `staging_offsite_backup.py`、`staging_backup_job.py`、`staging_pg_backup.py`、`staging_backup_watch.py`、`staging_monitor.py`，root 所有，目录 755、文件 644。它们只有代码，不包含密钥。先验证 Windows 首次拉取成功，再在现有巡检服务增加覆盖项：

```ini
# /etc/systemd/system/saas-staging-backup-watch@saas-pg-test-cvm.service.d/zz-offsite.conf
[Service]
WorkingDirectory=/srv/saas-staging/offsite-ops-20261111
Environment=STAGING_REQUIRE_OFFSITE=1
```

执行 `systemctl daemon-reload`，仅启动巡检 oneshot 核对结果，不重启应用或运行夜间备份。夜间备份仍使用原来的运维目录。新巡检八项均正常后再完成交接。

## 日常查看、暂停与恢复准备

```powershell
Get-ScheduledTaskInfo -TaskName 'SaaS-Staging-Offsite-Backup'
Disable-ScheduledTask -TaskName 'SaaS-Staging-Offsite-Backup'
# 恢复自动拉取
Enable-ScheduledTask -TaskName 'SaaS-Staging-Offsite-Backup'
Start-ScheduledTask -TaskName 'SaaS-Staging-Offsite-Backup'
```

暂停只阻止未来触发，正在拉取的任务让其完成。暂停超过 36 小时会有异机过期提醒。要完整撤回本轮自动化，应停用 Windows 任务，并删除上述**唯一巡检覆盖文件**后重新加载 systemd；保留备份目录与原有夜间备份/巡检。不要删除业务卷。

恢复时使用现有 `tools.staging_pg_backup` 的隔离目标恢复流程，要求精确匹配源镜像内容 ID、另行保管的私密配置和数据库凭据。备份含业务与会话签名材料，不能公开分享；不包含可替代所有私密配置的万能恢复包。本机、服务器均离线时不能接收告警；服务器整机失联还需独立外部存活监测。

后续：常在线 COS/第二服务器、备份加密与独立密钥管理、精确镜像归档及实际云端副本跨主机恢复、独立外部监测。备案通过前继续仅 SSH 转发验收，不开放域名、不提交微信审核。
