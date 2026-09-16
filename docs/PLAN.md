# AzurJuus 多智能体协作系统核心开发方案

> 历史设计存档：实际执行采用 Hermes＋AzurJuus 调度、Vue 和本地 SQLite，不再以本文中的 LangGraph、必需 PostgreSQL/Redis 为当前要求。新增人格、关系和成长实现见 [认知系统说明](COGNITION_GUIDE.md)，验收以实际记录为准。

## Summary
以现有 `桌面壳 + 前端聊天客户端` 为外层，重建一个本地优先、结构接近最终服务器版的后端核心：`FastAPI + WebSocket + PostgreSQL + Redis + 向量记忆库 + OpenAI 兼容模型层`。  
本次方案按三阶段完整骨架设计，但实施顺序仍保持你拟定的三阶段推进，确保第一阶段就能跑通真实模型、消息流、工具安全边界，同时后续可以无缝接入秘书智能体、动态建群、阶段审核、朋友圈与社会化行为。

默认设计选择：
- 本地开发环境即按最终形态搭：`PostgreSQL + Redis + 向量库`
- 大模型接入统一走 `OpenAI-compatible API`
- 本地工具执行永远在本地客户端/本地后端内完成，不允许智能体直接拿到 shell
- 第一版人类端只支持一个本地用户身份，真实多用户登录延后到后续版本
- 秘书智能体为显式可配置的单个主秘书，允许后续扩展为“每任务临时秘书”

## Key Changes

### 1. 总体架构重组
将当前 `server.py` 的静态 HTTP 服务升级为正式后端应用，拆成 6 个子系统：

- `API Gateway`
  - FastAPI 提供 REST + WebSocket
  - 继续兼容现有前端已有接口名，并补齐实时事件流
  - 统一返回会话、消息、任务、审批、朋友圈、配置快照

- `Realtime Messaging`
  - 会话、群聊、系统通知、任务事件全部抽象为统一消息流
  - WebSocket 负责前端实时渲染
  - Redis Pub/Sub 用于房间广播、任务事件广播、跨进程扩展预留

- `Agent Runtime`
  - 每个智能体本质上是一个具有人设、工具权限、说话风格和状态的“账户”
  - 模型调用通过统一 provider 适配层，先支持 OpenAI-compatible
  - 人设输入由现有角色导入 JSON/prompt 继续提供

- `Workflow / Orchestration`
  - 以“任务”为中心建立状态机与执行图
  - 第一阶段只跑单智能体任务路径
  - 第二阶段引入事件型社交行为
  - 第三阶段接入 LangGraph，用于秘书智能体组队、并行子任务、审查、断点等待、用户插嘴中断

- `Memory & Persona`
  - 静态数据进 PostgreSQL：用户、智能体、群、任务、工具权限、审批记录、朋友圈、评论
  - 动态记忆进向量库：任务摘要、聊天摘要、协作印象、外部知识片段
  - 建立“静态设定 + 动态记忆 + 当前任务上下文”的组装管线

- `Tool Safety Gateway`
  - 所有本地能力封装成白名单工具，不暴露任意命令执行
  - 所有路径操作强制经过授权工作区校验
  - 高风险操作必须经过“执行智能体 -> 秘书 -> 用户”审批链
  - 所有可逆文件操作默认生成 undo/snapshot 记录

### 2. 与现有前端的对接方式
保留现有桌面壳和前端主体，不推翻重做，重点把 mock 适配器替换为真实后端。

前端已有接口将升级为真实实现：
- `GET /api/bootstrap`
- `POST /api/conversations/open`
- `POST /api/messages/send`
- `POST /api/workflows/dispatch`
- `POST /api/agents/favorite`
- `POST /api/conversations/background`
- `POST /api/groups/roles`
- `POST /api/posts/like`
- `POST /api/posts/comment`
- `POST /api/posts/publish`
- `POST /api/personas/compose`
- `POST /api/window/preset`
- 保留 `/api/workspace/load` 与 `/api/workspace/save`，但改为读写正式后端状态快照

新增实时接口：
- `GET /ws`
- 事件类型至少包括：
  - `conversation.message.created`
  - `conversation.message.updated`
  - `conversation.typing`
  - `workflow.created`
  - `workflow.stage.changed`
  - `workflow.review.requested`
  - `workflow.review.resolved`
  - `workflow.agent.help_requested`
  - `post.created`
  - `post.comment.created`
  - `tool.approval.requested`
  - `tool.execution.completed`

前端调用边界要求：
- 聊天、群聊、朋友圈、任务审批、工具授权、秘书组队都必须有明确 API/事件入口
- 所有“未来要给智能体调用的功能”必须同时提供后端 service 接口，不允许只写前端按钮逻辑

### 3. 数据模型与核心对象
统一数据模型，避免“聊天系统”和“任务系统”分裂。

核心实体：
- `Actor`
  - 可是 human user，也可是 agent
  - 字段：账号、显示名、头像、类型、人设摘要、权限组、可用工具、状态
- `Conversation`
  - 私聊、群聊、任务群聊、系统通知频道统一建模
- `ConversationMember`
  - 包含角色：群主、管理员、成员、秘书、观察者
- `Message`
  - 纯文本、富文本、系统事件、任务卡片、审批卡片、工具执行卡片
- `Workflow`
  - 对应一个任务实例
- `WorkflowStage`
  - 支持阶段推进与断点审查
- `WorkflowAssignment`
  - 记录每个智能体的子任务、状态、依赖、是否允许并行
- `ApprovalRequest`
  - 用户或秘书审批记录
- `ToolExecutionLog`
  - 工具调用参数、结果、快照、回滚点
- `SocialPost` / `SocialComment`
  - 朋友圈与互动
- `MemoryChunk`
  - 动态记忆摘要及向量索引
- `AgentRelationship`
  - 智能体间关系强度、互动倾向、允许主动评论概率等

重要行为规则：
- 用户发给秘书的任务会先创建 `Workflow`
- 秘书选择的团队会自动形成一个任务群聊
- 编外求助必须先创建 `help_request` 事件，待秘书或用户批准后才可拉入上下文
- 阶段推进必须满足：执行完成 -> 秘书审查通过 -> 用户审查通过
- 同阶段独立任务可并行，但共享全局任务状态与审查门禁

### 4. 三阶段实施方案

#### 第一阶段：单用户 + 单智能体 + 真实模型闭环
目标：跑通“像聊天软件一样对话”的真实后端闭环。

实现内容：
- FastAPI 服务替换当前 `server.py` 简易服务
- PostgreSQL/Redis/向量库本地运行方案落地
- `Actor / Conversation / Message / WorkspaceSetting` 基础表
- WebSocket 实时消息推送
- `OpenAI-compatible` 模型适配层
- 单智能体 DM 对话链
- 角色导入结果接入智能体档案
- 工具白名单框架先落地，但只开放低风险只读工具
- 前端改用 `HttpAzurJuusAdapter`
- 保留当前桌面壳与本地设置持久化，但状态以数据库为主、工作区快照为辅

第一阶段成功标准：
- 用户能在桌面客户端与指定智能体真实对话
- 消息重启后保留
- 智能体人设、头像、账户信息来自本地角色配置
- 模型、API Key、秘书候选、授权工作区等设置可持久化
- 实时消息不再依赖 mock

#### 第二阶段：账号体系 + 朋友圈 + 空闲社交
目标：让系统从“能聊天”升级为“有社会感”。

实现内容：
- 本地用户 profile 与 agent account 完整化
- 朋友圈总览、详情、评论、点赞走正式数据库
- 定时任务框架接入
- 空闲状态下随机唤醒智能体发朋友圈
- 关系驱动评论机制
- 动态记忆写入与检索初版
- 会话消息与朋友圈内容写摘要进入向量库
- 用户可主动评论、收到回复提醒
- 朋友圈内容类型支持：日常感想、任务感想、带图动态、系统生成任务总结

第二阶段成功标准：
- 智能体能在无任务时主动发动态
- 其他智能体能基于关系和人设发表评论
- 用户端可查看、评论、追踪历史互动
- 朋友圈内容与评论在重启后保留

#### 第三阶段：秘书智能体 + 动态组队 + 阶段审查
目标：实现你定义的核心协作能力。

实现内容：
- LangGraph 正式接入任务工作流
- 秘书智能体任务入口
- 智能体筛选与组队
- 自动建群并写入任务群上下文
- 子任务并行执行
- 用户插嘴中断与状态更新
- 阶段性秘书审查与用户审查断点
- 编外求助审批流
- 任务总结、结果回传、朋友圈任务总结感想联动
- 群聊中保留“任务消息”和“闲聊消息”并存能力
- 秘书可监督、提醒、纠偏、重新分工

第三阶段成功标准：
- 用户向秘书布置任务后，能看到真实组队、群聊讨论、阶段卡片、审批卡片
- 同阶段独立子任务并行执行
- 用户中途修改意见能改变后续执行
- 阶段未获审批不能进入下一阶段
- 任务结束后秘书汇总并回传最终结果

### 5. 安全设计
安全作为一等能力设计，不做后补。

工具边界：
- 绝不提供 `execute_cmd` / 任意 shell
- 只暴露高封装工具，例如：
  - `list_dir`
  - `read_text_file`
  - `write_text_file`
  - `create_folder`
  - `move_file`
  - `copy_file`
  - `search_text`
  - `create_snapshot`
  - `restore_snapshot`
- 每个工具必须声明：
  - 风险等级
  - 可访问路径范围
  - 是否需要秘书审批
  - 是否需要用户审批
  - 是否支持回滚

工作区禁锢：
- 用户在设置中指定 `authorized_workspace_root`
- 后端工具层统一校验 `resolved_path.is_relative_to(root)`
- 超界立即拒绝，不由模型决定

审批与回滚：
- 高风险写操作先生成《操作计划》
- 执行智能体不能直接落地高风险变更
- 必须经过秘书审核，必要时弹到用户审批卡片
- 执行前创建 snapshot / undo log
- 用户可在聊天中发起“撤销上一步整理”

权限规则：
- 编外智能体不默认进入任务上下文
- 请求外援时只能暴露最小必要信息
- 是否允许外援由秘书或用户批准
- 敏感工具只允许明确授权的专职智能体使用

## Test Plan
需要从第一阶段起建立自动化验证，避免后面流程复杂后失控。

API / 后端测试：
- `bootstrap` 返回本地用户、已接入智能体、会话列表、设置快照
- `messages/send` 能写库、触发模型回复、广播 WS 事件
- `workflows/dispatch` 能创建任务与阶段状态
- `posts/publish` / `posts/comment` / `posts/like` 状态一致
- 角色配置变更后，旧角色保留历史，新角色生成欢迎消息

实时链路测试：
- 单聊消息能实时广播
- 任务群聊能收到系统消息与智能体消息
- 审批事件能推送前端
- 用户插嘴能写入工作流状态

安全测试：
- 超出授权目录的路径全部拒绝
- 未审批高风险操作不执行
- 执行后可用 undo 恢复
- 工具调用日志可审计

工作流测试：
- 秘书能选人建群
- 并行子任务互不阻塞
- 阶段未获双审不能推进
- 编外求助未经批准不可发生
- 用户修改需求后，秘书能重分配或重规划

持久化测试：
- 退出重启后保留设置、会话、任务、朋友圈、审批记录
- WebSocket 重连后能恢复增量同步
- 本地数据库损坏时可提示恢复或重建

UI 验收场景：
- 单聊、群聊、任务群聊、朋友圈、审批卡片都能完整显示
- 长文本、多人群聊、系统卡片、并行进度卡片不破版
- 任务过程对用户可见，不是黑盒执行

## Assumptions
- 第一版不做真正的远程多用户登录系统，只做一个本地用户 + 多智能体账户
- 本地开发环境使用 `Docker Compose` 拉起 PostgreSQL、Redis、向量库；桌面壳和 FastAPI 本地运行
- 向量库优先选 `Chroma` 或同类本地易部署方案；若后续要更强扩展性再切 Milvus
- LangGraph 只在第三阶段正式承接任务图执行，前两阶段先保留 workflow service 边界与数据模型
- 现有前端保留为主，只做必要结构调整，不整站重写
- 智能体“闲聊”和“朋友圈”默认允许轻量随机生成，但必须支持全局开关和频率限制
- 秘书智能体为设置里的显式配置项，默认唯一主秘书
# 当前状态说明（2026-09-13）

后续已确认采用 Hermes 执行核心、AzurJuus 持久化调度、Vue 界面与 SQLite 默认存储。本文早期技术选型和下方历史完成声明不再代表当前生产链。

本轮补齐任务群、成员实际消息、只读查询交付、漏交补交及记录删除；仍待迁移的外援入群、阶段双审与技能治理详见 [当前计划对照](docs/PLAN_AUDIT_20260913.md)。

# 历史 Status Audit (2026-04-08)
#
# - Phase 1: implemented and regression-covered.
# - Phase 2: implemented with persisted social posts/comments, idle social runtime, and memory writeback.
# - Phase 3: main path implemented; approvals, help resolution, interrupt revisions, runtime ticks, and stage re-entry now run through orchestration-first action plans.
# - Skill model: aligned to the revised public/private/learned design, including proposal -> approval -> effective public skill overlay.
# - Safety gate: high-risk tool plans now require true secretary + user dual approval before execution.
# - Current priority: finish backend convergence and then return to UI polish/performance as the remaining major gap.
