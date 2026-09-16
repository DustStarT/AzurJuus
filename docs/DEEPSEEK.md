# DeepSeek 实际验收

## 2026-09-13：任务交付与协作回归

使用原始 `launch_azurjuus.bat`、相对状态目录、PySide6/QWebEngine 与保存的 DeepSeek 配置，在新数据库和合成工作区中通过：普通“你好”、报告写入与读回、原始“工作区内的文件都有些什么？”查询，以及能代 → 信浓的文件依赖交接与秘书复核。任务群消息、原会话摘要和技能记录均实际落库。完整报告见 `validation/batch-execution/extended-report.json`。

运行中的退出测试耗时 2.469 秒，退出码 0，无测试子进程残留，未完成任务持久化为暂停。本轮修复还处理了范围说明误判失败与模型漏交 deliver；自动补交的只读权限及失败回复保留由模型桩回归验证。有限合成任务不代表普遍任务成功率。

下面保留上一轮文档、社交等验收记录。

2026-09-12 使用本地保存的 `api.deepseek.com` / `deepseek-flash` 配置。API Key 由 DPAPI 解密后只用于发出请求，没有打印，也没有写入 Hermes 配置文件。所有输入均为隔离的合成数据；社交内容只写验收报告，没有发布到用户朋友圈。

## 已通过

| 测试 | 实际结果 | 报告 |
| --- | --- | --- |
| 模型列表与连接 | HTTP 200，保存的模型可用 | `validation/provider-report.json` |
| 普通对话 | 返回 AZUR_OK，约 0.73 秒 | 同上 |
| 工具往返 | 模型请求加法，接收实际 3+4 结果后回答 7 | 同上 |
| 流式输出 | 完整接收 STREAM_OK，约 0.77 秒 | 同上 |
| Hermes 闲聊 | 返回 AZUR_CHAT_OK，工具调用数为 0 | `validation/cloud-chat-report.json` |
| 单人文件执行 | 读取两种水果数量，写入并读回报告，秘书独立复核；10 次调用全部成功，总数 7 | `validation/cloud-report.json` |
| 两成员协作 | 分析员读取 TXT、DOCX、XLSX，生成 totals.json；秘书依赖前项生成 report.txt，再独立复核；25 次调用全部成功，总数 18 | `validation/cloud-collaboration-report.json` |
| 社交生成 | 实际应用模型路径生成中文动态和评论，约 2.42 / 3.11 秒 | `validation/provider-social-report.json` |
| 正式 HTTP 执行链 | 经聊天接口提交任务，后台实际完成 13+8=21 的报告；重复请求返回同一任务，用户/结果消息各一条，产物下载字节一致，启动快照不泄漏密钥 | `validation/cloud-api-report.json` |

延迟为本次请求的观测值，不是服务延迟保证。协作产物人工复核：苹果 3、橙子 4、梨 5、香蕉 6；三个来源小计 7、5、6，总数 18；最终报告引用了实际 totals.json 的 SHA256。

## 测试发现与修复

1. **直接请求成功，Hermes 却报 401。** 上游自定义接口不会把通用 `OPENAI_API_KEY` 自动转发给任意主机，原适配器只设置环境变量，最终发出了无密钥占位值。现在在模型配置中显式引用 `${OPENAI_API_KEY}`，不落盘明文密钥。实际进程回归增加了请求鉴权检查。
2. **审查成员尝试不必要的命令。** 原 MCP 目录向所有阶段描述全部工具，审查提示还遗漏了原始用户约束。现在规划、执行、审查、闲聊各自只暴露获准动作；审查接收原始目标与限制，并明确使用自身成功读取的调用证据。
3. **权限原因被 HTTP 错误掩盖。** MCP 现在转发具体权限错误，避免模型只看到笼统的 500/403。
4. **文本与哈希可能来自不同次读取。** read_file 现在一次读取原始字节，再从同一份内容生成文本、SHA256 和字节数。独立审查无需为这些元数据追加命令。

2026-09-13 增加实际批处理入口、QWebEngine 回复显示与运行中关闭验收，见 [桌面调查记录](DESKTOP_FIX_20260913.md)。后续已定位用户无回复根因：相对 HERMES_HOME 随子进程 cwd 改变，导致生成的 DeepSeek 配置没有被加载。已固定绝对路径，按 `.env` 同样的相对状态路径完成“能代／你好”和文件任务验收。密钥无需重填。最新完整回归见 `validation/pytest.xml`。

## 复现

在项目目录使用已保存配置运行，测试会调用真实云端模型：

```powershell
.venv\Scripts\python.exe -X utf8 tools/provider_acceptance.py
.venv\Scripts\python.exe -X utf8 tools/provider_acceptance.py --social-only
.venv\Scripts\python.exe -X utf8 tools/cloud_acceptance.py --chat
.venv\Scripts\python.exe -X utf8 tools/cloud_acceptance.py
.venv\Scripts\python.exe -X utf8 tools/cloud_acceptance.py --collaborative
.venv\Scripts\python.exe -X utf8 tools/cloud_api_acceptance.py
```

DeepSeek 思考模式的工具消息字段规范参见 [官方说明](https://api-docs.deepseek.com/guides/thinking_mode/)。本轮完整工具循环由锁定版本 Hermes 处理；基础接口探针显式关闭思考，二者分别验证。
