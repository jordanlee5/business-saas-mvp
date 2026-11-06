# GitHub 微信告警核对与提交防护

日期：2026-11-06。用户提供邮件截图，类型为 `Tencent WeChat API App ID`，指向 `96003b7f` 的 `tools/rehearse_concurrent_mall.py:26`。

## 核对结论

已读取该历史提交：命中的是人为编写的虚拟压测标识 `wx0123456789abcdef`。脚本在独立测试库中生成虚拟微信绑定，配套身份交换也使用测试替身。它不是本项目实际公司小程序AppID，更不是AppSecret。已在代码旁明确标注用途；保留格式以便测试真实输入校验，不通过拆字符串或关闭扫描规避告警。

本机所有可达引用含main、origin/main及已有标签，扫描到1779个Git文件版本。使用私钥头、GitHub令牌、云访问密钥、微信秘密字面量规则检查，并仅在内存中将本机私有目录中的2项非空秘密（微信AppSecret、会员会话签名密钥）逐一比对，未发现命中。没有显示、复制到报告或上传秘密值。

另核对数据库连接串示例：README与4份测试文件中的固定占位值用于文档或“拒绝连接/禁止打印秘密”的负向测试。检查器仅为这些已核对的文件路径和完整URL指纹设置精确例外；同文件中的新值仍会被检查。没有对全部测试目录免检。

这是有明确范围的检查，不能证明远端已删除分支、PR引用、附件、CI历史日志或每一种未知凭据都没有问题。GitHub连接器未提供告警管理接口，备用只读API请求连接失败；没有声称读取或关闭了远端告警。邮箱截图仅留本机。

## 这条告警如何处理

进入[仓库秘密扫描告警](https://github.com/jordanlee5/business-saas-mvp/security/secret-scanning)，打开邮件对应的告警，再次核对类型、提交和文件位置一致。对于这一条已确认的虚拟值，可选择关闭告警的“Used in tests／用于测试”理由，并备注“独立压测虚拟AppID，不是注册应用或AppSecret”。如果页面内容是其他值或类型，则应重新核查，不能批量关闭。

仅因这一条虚拟标识告警无需重置真实AppSecret。今后若发现真正的AppSecret、私钥或访问令牌曾进入历史，应先撤销/轮换，再安排历史清理；删掉最新文件或关闭告警不能使旧秘密失效。[GitHub秘密扫描说明](https://docs.github.com/en/code-security/concepts/secret-security/secret-scanning)

## 本轮新增防护

- `.gitignore` 补充环境配置变体、本地微信配置、私钥容器、开发者私有配置及运行目录等；它不能移除已经提交的文件，因此仍需内容扫描。
- `tools/check_secrets.py` 检查Git已跟踪、暂存或历史内容。报告仅输出文件路径、对象编号和规则，不输出匹配原文。AppID是标识符，不作为秘密拦截；AppSecret、私钥和令牌仍检查。
- `.githooks/pre-commit` 对暂存内容检查，失败即阻止提交；本机解释器路径只写在本地Git配置，不进Git。团队其他设备需自行启用钩子并配置Python。
- GitHub Actions使用只读仓库权限运行相同检查，并禁用checkout凭据持久化。CI发生在推送之后，不能取代推送前检查或GitHub的推送保护。继续保留GitHub秘密扫描；本轮未修改远端扫描设置。

启用方式（Python路径填本机实际解释器，不含任何秘密）：

```text
git config --local core.hooksPath .githooks
git config --local secrets.python PATH_TO_PYTHON
python tools/check_secrets.py --mode staged
```

检查器是针对本项目的基础检查，无法穷尽所有秘密格式，不能作为允许提交未知密钥的依据。源码、演示包、测试证据与本机私密配置继续分开存放。
