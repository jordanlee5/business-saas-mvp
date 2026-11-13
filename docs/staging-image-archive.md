# 恢复点对应的应用镜像归档

本轮从 `f617b95` 接续，为异机恢复补齐应用镜像的归档、校验与加载工具。数据库和图片有备份，还需要与该恢复点匹配的应用版本；同名镜像标签可能指向不同内容，不能用“标签相同”代替版本校验。

## 使用边界

- 只对已完整校验的测试备份操作，读取其中的 `app_image_id`，按精确内容 ID 导出。不会构建新版本、修改已有镜像标签、自动部署或重启应用。
- 归档只含一个无标签应用镜像。校验外层大小和 SHA-256、配置内容 ID、每层内容 SHA-256；拒绝多镜像、标签、路径越界、链接和多余文件。不在宿主机解压归档。
- 支持经典 Docker save 及单镜像 OCI 布局的未压缩 tar。OCI 根清单必须匹配恢复点中的 ID，配置与每个层的大小、摘要、顺序必须同时匹配 OCI 描述符及 Docker 兼容清单。压缩层、多根清单、额外对象和标签仍拒绝处理。
- 输出目录必须在仓库外且路径不经过符号链接/Windows 重解析点。Linux 目录 700、文件 600；Windows 使用者还须通过目录 ACL 限制其他用户读取。镜像可能含构建时配置或历史层，视为私密材料，绝不提交 GitHub。
- 校验用于发现损坏和版本错配，不是来源签名或加密。只使用可信来源的备份及镜像；备份清单本身也须受保护。
- 上限 20 GiB，导出前检查磁盘余量（镜像大小的两倍加 1 GiB），Docker 操作限时 15 分钟。导出完成后再次检查实际大小；并非运行中磁盘配额。失败保留 `.partial-*`，不当作成功归档，不覆盖已有目录。

## 操作顺序

在有目标应用镜像的 Docker 主机上运行（仓库只提供工具，备份和归档在私密目录）：

```sh
python3 -m tools.staging_image_archive create --backup /private/recovery-point --bundle /private/application-image
python3 -m tools.staging_image_archive verify --backup /private/recovery-point --bundle /private/application-image
```

归档完成后，应将整个 `application-image` 目录通过已有受保护传输渠道复制到恢复主机。以下命令先重新校验，再导入 Docker 并核对导入后的内容 ID：

```sh
python3 -m tools.staging_image_archive load --backup /private/recovery-point --bundle /private/application-image
```

输出仅含结果、操作、镜像 ID、配置 ID、加载后的运行时 ID 和大小。新版 Docker 可用 OCI 清单摘要标识镜像，经典引擎用配置摘要；二者不同不代表代码版本不同，但必须由完整归档的内容关系证明。恢复时将 `STAGING_APP_IMAGE` 设置为 load 返回的 `runtime_image_id`，再执行 `python3 -m tools.staging_pg_backup restore /private/recovery-point --project saas-pg-test-cold-UNIQUE --image-bundle /private/application-image`；必须是全新 `saas-pg-test-*` 项目、独立数据卷和私密凭据。不要把恢复目标指向现有验收项目。该流程会废止备份中的历史后台登录会话。

应用与 OCR 共用这个镜像；PostgreSQL、HTTPS 网关镜像及私密配置须另行准备。本工具不是整机备份。所有引用此镜像的恢复点退役前保留归档；本轮不自动删除镜像，也不将大镜像加入每小时数据拉取任务。

## 虚拟数据验证

```sh
python3 -m unittest test_staging_image_archive test_staging_postgres test_staging_offsite_backup test_staging_backup_job test_staging_backup_watch -q
TMPDIR=<仓库外持久私密目录> STAGING_COMPOSE_BIN=/usr/local/bin/docker-compose python3 -m tools.rehearse_offsite_backup --image <本机测试镜像> --archive-image
```

演练创建两个隔离 PostgreSQL 项目：虚拟数据备份、异机传输协议、镜像归档/校验/加载、使用不可变镜像 ID 恢复、健康和登录检查、兑换/重复请求/取消返还。通知使用本地捕获，不发送群消息。结束只停止演练项目，原始备份、归档和证据留在仓库外。

本轮不更换正在验收的云端应用，不安装新的定时任务。最初工具轮次仅验证本机已有镜像，见 `validation/staging-image-archive-20261111.json`。后续从 `66882d9` 接续的实际云端镜像归档、异机空引擎恢复及重启验证，单独记录于[云端恢复演练](staging-cold-recovery.md)，不混用两轮证据。


## 2026-11-12 发布前恢复依赖检查

后续发布或计划清理前，先按[恢复点与镜像关联检查](staging-recovery-catalog.md)核对保留点与候选版本的归档覆盖。检查不通过先补齐材料；未提供的手动或长期封存目录须单独核对，任何未引用标记都不等于删除授权。本轮未发布新应用、安装镜像定时任务或更改既有恢复点。


## 2026-11-13 候选镜像归档

新增 `create-image/verify-image --image-id`，用于尚无数据库恢复点的候选镜像；只接受完整内容ID，沿用原私密目录、完整层核验和拒绝覆盖保护。恢复 `load` 仍要求对应已验证备份。本轮实际生成新候选并归档，详见[候选镜像生成与证据](candidate-image-archive.md)。
