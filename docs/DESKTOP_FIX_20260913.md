# 桌面无回复与退出阻塞调查（2026-09-13）

用户入口为双击 `launch_azurjuus.bat`。此前接口验收通过不能证明桌面正常，本次增加实际批处理入口、QWebEngine 消息显示和运行中关闭的验证。

## 后续截图定位：相对路径导致配置丢失

用户“能代／你好”运行 `run-12b2ae02b74248e0bebebd1c99a18992` 在 2026-09-13 02:46 UTC 启动，约 120 秒后暂停。`.env` 与 `.env.example` 使用 `AZURJUUS_WORKSPACE_STATE_PATH=.azurjuus/workspace-v2.json`。

原实现把相对路径一路传给 `HermesBridge.home`，在应用工作目录写入 `config.yaml`，随后以 `.vendor/hermes-agent` 为 cwd 启动子进程，并传递同样的相对 `HERMES_HOME`。Hermes 因此进入 `.vendor/hermes-agent/.azurjuus/hermes/<run-id>/chat`，找不到应用生成的配置，走向默认配置。证据是预期目录仅有 `config.yaml` 和 `bridge.log`，而 `state.db`、`projects.db`、错误日志都在错误目录；错误日志包含默认 OpenRouter 路径，没有 DeepSeek。

这解释了之前的假阴性：隔离测试覆盖普通对话和文件任务，但一直使用绝对状态路径。已在 `backend/config.py` 将相对配置路径以应用目录为基准解析，`HermesBridge` 在变更子进程 cwd 前再次固定绝对 home，同时固定相对上游源代码路径。用户不必修改 `.env` 或重新保存密钥，历史任务仍从原应用目录读取。误放的上游文件作为调查证据保留，不据此补造完成结果。

新增路径单测，并让真实 Hermes 七轮工具测试直接接收相对 home；批处理验收默认也改用相对状态路径。在这个条件下，“能代／你好”已显示正常角色回复，文件任务生成并核验报告，运行中退出约 0.64 秒，子进程全部退出，且状态文件与配置都落在预期目录。见 `validation/batch-execution/greeting-report.json` 和 `chat.png`。检查过实际截图，回复来自智能体消息而非用户输入。

## 已修复

- `desktop.py` 不再在 Qt 主线程中 join 后端线程。关闭时显示保存提示，后台停止运行，Qt 事件循环保持响应；页面先释放，缓存配置对象随后随窗口释放。
- 后端先取消模型与待审批工具请求，再让 Uvicorn 等待 HTTP 请求结束，避免审批请求与 lifespan 清理相互等待。并发关闭共用一次清理，运行任务保存为暂停。
- 实际批处理测试捕获 Uvicorn 0.52.4 默认 SansIO WebSocket 在页面关闭时重复发送关闭帧，报 `InvalidState: connection is closing`。当前明确选用已安装的 `websockets` 实现；依赖仍锁定，升级时须重新验证。此实现有弃用警告，后续应在上游修复得到验证后迁回。
- Redis 监听器退出正确处理 `CancelledError`。
- 界面显示检查环境、启动核心、准备会话、初始化执行、等待模型等真实阶段。`prompt.submit` 只确认排队，因此等待模型状态改为收到 `message.start` 后显示。暂停的闲聊也有检查及继续入口。模型返回空文本显示失败，不再标记完成却没有回复；闲聊等待预算为 120 秒，超时可继续。
- 每个执行阶段保存脱敏 `bridge.log`，记录进程标识、协议阶段与错误，不记录提示或 RPC 正文；屏蔽模型密钥和工具会话令牌。默认位置为 `.azurjuus/hermes/<run-id>/<phase>/bridge.log`。

## 验证证据

- 修复相对路径后全量后端回归：63 passed，53.31 秒。3 项警告为 Starlette/AnyIO 与所选 WebSocket 实现的弃用提示，记录见 `validation/pytest.xml`。
- `npm run build` 通过。
- `tools/qt_execution_acceptance.py`：合成闲聊、文件任务；保留可选服务配置的测试通过。运行中关闭：关闭调用不阻塞，后台约 0.83 秒停止，500ms Qt 心跳不中断，任务暂停保存。见 `validation/qt-execution/active-close-report.json`。
- `tools/batch_execution_acceptance.py`：启动原始批处理文件，通过仅绑定本机的 CDP 操作实际桌面；等待对应 `<run-id>-result` 的智能体消息，避免误匹配用户提示中的标记。读取合成输入并生成、核验报告后，再在新对话运行时用 Windows 关闭消息退出。最终通过：主进程约 0.69 秒退出，任务暂停保存，无残留测试子进程和异常堆栈；已查看两张截图确认实际回复。最终结果和截图见 `validation/batch-execution/`。
- 测试数据库、浏览器配置与文件都位于项目内的新隔离目录。云端仅使用合成任务与文件，读取已保存的模型配置以连接用户授权测试的 DeepSeek，不复制用户历史。

## 验收边界

本次截图对应的故障已定位到相对路径导致的配置丢失。本轮结果证明当前代码在本机相对配置路径、普通问候及合成文件任务的批处理入口验证结果，不能替代长任务或任意 Windows 环境验收。

再次发生时，聊天中的具体阶段、任务错误及上述日志可区分核心启动停滞、模型等待与消息回写问题。用户原有历史和授权工作目录未被此次合成验收修改。
