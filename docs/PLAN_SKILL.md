> 2026-09-16 状态索引：下文是历史设计讨论。生产链已加入个人方法隔离试用、启用/停用/回滚、秘书复核与用户确认共享。自动验收覆盖目录清单和文件分类移动；其他候选保留待验证。具体实现与限制见 [认知系统说明](COGNITION_GUIDE.md)。

会，这正是我上一版“按任务分配 skill”方案里最容易变僵的一点。你现在这套思路更好，也更像你真正想做的“有人设、有成长、有协作记忆”的系统。

我建议保留你的三类概念，但在实现上稍微调整一下：`学习 skill` 更适合被当成“来源/成长路径”，而不完全是和“公有 / 特有”并列的第三种静态类别。这样系统会更自然，也更好落库。

**我对你这个设定的判断**
- `公有 skill`：很好，适合作为所有智能体都能调用的基础工作流模板，比如总结、检索、规划、会议纪要、礼貌回复、文件整理流程。
- `特有 skill`：非常重要，这正是保证角色不变成“同一个模型换头像”的关键。它应该体现角色独特的做事习惯、偏好、节奏、表达方式、判断侧重。
- `学习 skill`：这个想法也很好，但它本质上更像“特有 skill 的生成方式”。也就是说，学习 skill 最终通常应落成某个智能体自己的特有 skill，而不是永远作为独立大类悬空存在。

所以我更推荐把它实现成两个维度，而不是三个互斥桶：

- `可见性`
  - `public`
  - `private`
- `来源`
  - `builtin`
  - `forked_from_public`
  - `learned_from_collaboration`
  - `self_evolved`

这样你想要的三种效果都还在：
- 公有 skill：`public + builtin`
- 特有 skill：`private + builtin/forked/self_evolved`
- 学习 skill：`private + learned_from_collaboration`

这样设计比硬分三类更稳。

**为什么这套更不容易僵硬**
关键不是“有没有 skill”，而是“skill 管什么”。

如果 skill 管的是“固定台词”和“固定步骤”，那一定会僵。
如果 skill 管的是“做事方法偏好”，而不是“说话内容本身”，就不会那么僵。

我建议拆成这三层：

- `Persona` 决定：
  - 我是谁
  - 我怎么说话
  - 我怎么看问题
  - 我在合作里偏谨慎、强势、温和还是跳脱
- `Skill` 决定：
  - 我通常怎么完成某类任务
  - 我会先规划还是先试做
  - 我更擅长拆解、归纳、质检、调度还是执行
- `Tool` 决定：
  - 我实际上能做什么安全动作

也就是说：
- 最终说出来的话，永远先过 `persona`
- 完成任务的方法，主要由 `skill` 决定
- 能不能落地执行，交给 `tool gateway`

这样同样是“总结一份文档”：
- 信浓会更柔和、朦胧、带一点感性判断
- 能代会更克制、明确、注重结构
- 豪可能更直接、更有推进感

但她们底层可能都在用同一个公有 `summarize_document` skill，只是各自叠加了特有 skill patch。

**我建议的运行逻辑**
不要让秘书“分配 skill”，而是让秘书分配：

- 任务目标
- 角色职责
- 约束条件
- 截止节点
- 可选建议

然后每个智能体自己从技能池里决定怎么做。

更准确地说：
- 秘书分配的是“你负责什么”
- 智能体自己决定“我用什么方法做”

秘书可以建议：
- “你优先用你擅长的校对方式”
- “先按公共流程做，再补充你自己的判断”

但默认不强制指定 skill。
只有在高风险流程里，秘书或用户才可以要求“必须走某个公有安全流程”。

这就既保留个性，又保留可控性。

**我建议的 Skill 结构**
每个 skill 不该只是一个 prompt，而应该是一个“方法包”。至少包含：

- `id`
- `name`
- `visibility`
  - `public` / `private`
- `origin`
  - `builtin` / `forked` / `learned` / `self_evolved`
- `owner_actor_id`
  - 公有为空，私有则归属某个智能体
- `base_skill_id`
  - 从哪个公有 skill 分叉而来
- `summary`
  - 对外可见的粗略描述
- `internal_method`
  - 真实流程内容
- `prompt_patch`
  - 对模型行为的补丁
- `tool_allowlist`
  - 允许调用哪些工具
- `style_bias`
  - 节奏、结构、谨慎度、主动性等偏好
- `teachable`
  - 是否可教学
- `learnable`
  - 是否可被他人观察后启发
- `confidence`
  - 技能成熟度
- `visibility_hint`
  - 其他智能体能知道到什么程度

这里有个很关键的点：
`特有 skill` 不应该完全不可见，而应该是“流程不可见，效果可感知”。

比如别的智能体可以知道：
- “她似乎有一种很擅长把混乱信息整理成清晰结构的方法”
但不知道：
- 她内部到底分几步、怎么加权判断、怎么构造提示词

这就很像真实的人。

**学习 skill 怎么产生**
我建议不要让它“一看就学会”，而是分 3 段：

1. `观察`
- 在合作任务中，A 看到 B 用一种方式高效完成任务
- 系统只记录“方法特征摘要”，不是直接复制全部流程

2. `启发`
- A 基于自己 persona 和已有 skill，生成一个“候选 learned skill”
- 这一步不是照抄，而是“受启发后形成自己的版本”

3. `固化`
- 候选 skill 需要在几次任务中验证效果
- 成功率、稳定性够了，才升格成正式 private skill

这很重要，因为如果“看一次就完整复制”，人物差异会很快塌掉。

所以学习 skill 应该是：
- 受启发
- 再人格化改写
- 再通过任务验证
- 最终成为自己的 skill

这才符合你要的“可学习，但不失人设”。

**公有 skill 的改动机制**
你说“除秘书外其他智能体无权改基础 skill，对公有 skill 改动需用户同意”，这个方向对。

我建议再补一层：
- 智能体不能直接改公有 skill
- 只能提交 `improvement proposal`
- 先生成候选版
- 由秘书审查
- 再由用户确认
- 确认后产生新版本 `public skill v2`

这样会非常稳。

而且任何公有 skill 的改进，都应该保留：
- 原版
- 候选版
- 版本差异
- 回滚能力

**对第三阶段最重要的影响**
如果按你现在这套思路，第三阶段我会这样改：

- 秘书不再“给成员分配 skill”
- 秘书只分配：
  - 子任务
  - 职责
  - 协作对象
  - 审查节点
- 每个智能体在执行时：
  - 先从 `public skill`
  - 再看自己的 `private skill`
  - 再看是否有可用 `learned skill`
  - 最后临时组合出本次执行方法

也就是说，Skill 是“内在方法库”，不是“外部命令标签”。

这会比我上一版方案更符合你的目标。

**安全上需要加的限制**
这套设计很好，但一定要加三条硬限制：

- `skill` 永远不能扩大工具权限  
  即使学会了某种方法，也不能因此自动获得新工具。
- `learned skill` 默认只能先是私有候选  
  不能直接变成公有。
- `private/learned skill` 的外显行为仍然要过审批链  
  尤其涉及文件操作、批量修改、删除、移动时。

所以：
- Skill 可以进化
- Tool 权限不能跟着自由进化

这是底线。

**我的结论**
这套设定是对的，而且比“直接分配 skill”更适合 AzurJuus。

如果让我定一个最终实现原则，我会这样写：

- `公有 skill`：系统级基础方法
- `特有 skill`：角色化的独有方法
- `学习 skill`：由观察、协作、教学产生，并最终沉淀为某智能体的特有 skill
- `秘书分配任务，不分配具体 skill`
- `智能体自主选用并组合 skill`
- `persona 决定表达，skill 决定方法，tool 决定落地边界`

如果你愿意，下一步我可以直接把 `PLAN.md` 对应的第三阶段前置设计改成这套新 Skill 架构，并进一步细化成：
1. 数据库表设计  
2. 后端运行时结构  
3. 前端需要展示但不暴露细节的 UI 方案

# 当前技能状态（2026-09-13）

Hermes 生产链现已接入公有/私有方法选择与成功执行记录；通过验收的依赖协作可以产生默认禁用的私有候选。候选试用与晋升、公有技能秘书审查及用户审批的完整新界面尚未完成。旧流程的完成记录不能代替生产链验收，详见 [当前计划对照](docs/PLAN_AUDIT_20260913.md)。

# 历史 Skill Status Audit (2026-04-08)
#
# - Public skills are implemented as the shared base method layer.
# - Private signature skills and learned collaboration skills are persisted per actor.
# - Shared-skill governance is implemented through skill proposals plus user approval.
# - Workflow interrupts, helper admission, execution runs, and stage reviews already record skill runs inside the orchestration path.
# - Remaining work: surface more inspection UI after backend convergence.
