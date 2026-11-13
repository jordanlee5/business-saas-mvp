# 候选镜像离线生成与归档（2026-11-13）

本轮继续候选发布准备，**没有升级云端或现有验收环境**。用户确认控制台窗口不再弹出，决定仍用当前 Windows 电脑接收异机备份，暂不换机；备份计划没有调整。

## 已完成的内容

- 业务候选仍固定为 `677ce4b355edc4280c6c9e9bf79f6c0ccada6955`，原源码清单摘要 `d4d05ebabc4d7016de8fe71e370bd962df5b0ff9c50a381ad90cc784c0c21e74`、226文件保持。
- 本机已有旧镜像 `sha256:d3e433edec4aedc975f05cf735d8af5cddd6ef3fa705449633870b43c1f308c2`；其173个运行文件与 `b4f939a` Git对象逐文件及完整文件集合核对一致。候选与旧提交的 Dockerfile、两份依赖文件字节相同。
- 在全新Linux私密目录以此镜像为基础，仅替换运行源码。构建与核对容器都断网、限0.5CPU/512MiB，未执行 apt/pip 安装、拉取镜像、改已有标签或重启既有容器。新镜像没有覆盖旧镜像，临时核对容器退出后自行删除；原容器ID集合保持。
- 新镜像 `sha256:494444cbe4f4bcf2b92add4a0845856517a51ed66e15868d1382a7d9b71d2cae` 的213个运行文件及文件集合与固定候选清单一致。运行包54个，名称/版本集合摘要 `8b013c167d7f0b7aacce05def2fb89526d479a3f5e694fa04e77dca7d63622b0` 与原镜像相同；Python为3.12.16。
- 新归档 892417024 字节（约851MiB），整体SHA256 `f4eaa4b23c46bdde2fa39c34b17efaa84a5615b17ac0a286f423b3d722b90ff3`。Docker保存后重新校验清单、配置ID及全部层内容，配置ID与镜像ID一致。原恢复点、旧镜像归档均未读取或覆盖。

这是**固定旧依赖上的源码候选**，并非重新安装得到的新依赖环境。构建文件不含采购或真实短信配置。本轮没有做依赖安全更新，也不把依赖未变解释为没有漏洞。

## 不再需要伪造备份来归档候选镜像

此前 `create` 需要真实已验证恢复点才能确定镜像，候选尚未上线时并没有这样的数据库恢复点。本轮复用原归档实现，增加两个明确入口：

```sh
python3 -m tools.staging_image_archive create-image --image-id sha256:<完整64位内容编号> --bundle /private/NEW-candidate-archive
python3 -m tools.staging_image_archive verify-image --image-id sha256:<同一完整内容编号> --bundle /private/NEW-candidate-archive
```

拒绝可变标签、非完整编号、混用备份参数和已有输出目录；失败保留私密临时归档。格式与已有恢复目录检查兼容，`verify-image` 不连接Docker。`create-image` 只证明归档与明确指定镜像相符，不自行证明源码出处；本轮另以逐文件比对建立出处证据。

原 `create/verify/load --backup ...` 行为保留，恢复加载仍必须由已验证备份确定镜像，不能拿候选编号绕过旧备份匹配。没有新增定时归档或删除任务，没有把约851MiB镜像加入小时数据拉取。

## 本轮构建方法与材料

完整执行脚本留在本机协作目录 `.runtime/build-candidate-image.py`；新Linux私密目录保存源码副本、构建上下文、Dockerfile、image-id、build.log、result.json和 `archive/`，实际位置由本机 `.runtime/candidate-image-20261113-result.json` 指示。原始归档只保存在本机，不进Git。

步骤为：原Windows源码包校验 → 仅在新Linux副本恢复清单权限并再校验 → 旧镜像源码/依赖核对 → 离线构建 → 新镜像源码/依赖核对 → 内容编号归档/完整层校验 → 原容器ID集合及源码包复核。若任一环节失败，保留现场并停止，重试另建目录。

生成的Dockerfile以已核验的旧镜像完整ID为 `FROM`，只在新镜像层移除 `/app/app`、`/app/migrations`、`/app/tools`，再复制候选对应目录和两份依赖文件、alembic.ini，写入候选提交及源码清单摘要标签，最终用户保持 `10001:10001`。清除旧目录防止已删除的旧文件残留；没有运行安装命令。标签仅辅助追溯，实际通过文件内容核对确认版本。

私密构建命令等效为：

```sh
DOCKER_BUILDKIT=0 docker build --pull=false --network none --memory 512m --cpu-period 100000 --cpu-quota 50000 --iidfile /private/NEW-run/image-id /private/NEW-run/context
```

不得直接以未核验工作区代替固定源码包，也不得用同名标签代替完整镜像ID。若依赖文件或Dockerfile变化，须停止复用本方法，另安排不影响测试的依赖构建和验证。

## 验证与剩余门槛

Windows归档28项通过；Linux归档/恢复目录组合40项通过，覆盖候选无需备份、标签/错ID拒绝、不能覆盖、离线校验，以及旧备份/镜像关联和加密点无需私钥清点。Windows组合检查因本机无cryptography失败，未安装依赖，改用已有Linux环境完成组合验证。NTFS执行权限差异在新Linux副本解决，原Windows包保持；失败过程不计为通过。详见[脱敏证据](validation/candidate-image-archive-20261113.json)。

下一轮用这个固定镜像做隔离虚拟数据HTTP及业务联验、旧客户端兼容核对。本轮仅完成镜像生成与归档，尚未进行这些业务检查或重复冷恢复；也没有建立执行时一致恢复点、全范围恢复依赖清单或发布窗口。因此仍不等于可直接上线。备案、企业主页待验收、真实短信和微信接入边界保持。
