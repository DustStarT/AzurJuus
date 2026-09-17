# 2026-09-17：气泡对齐、协作对象与冗余清理

## 修改及原因

- 气泡：原 Vue 跨组件选择器编译后没有命中内部 speech-stack。现在 App 按实际用户编号传递 self，由 SpeechBubbles 内部控制靠右；角色保持靠左。
- 协作：最终表达明确传递当前群、实际参与者和最近四条已完成公开讨论；认知关系只选当前对象。原始 Hermes 文字在默认表达开关下已经进入工作记录，本轮增加回归测试，并未将其重新送入聊天。
- 清理：静态检查生产模块、测试、工具中的调用，并递归追踪服务层内部调用，移除 40 个无调用者旧方法。旧 WorkflowEngine 删除，保留只读 workflow_view 展示历史流程。清理无效定时配置、导入和常量；未操作真实数据库、用户配置、容器或卷。
- 文档：重写 CODE_WALKTHROUGH、INTERVIEW_GUIDE，新增 TECHNICAL_REPORT，修正 README 中技能尚未试用及旧兼容执行路径的过时说明。

## 本轮实际验证

| 检查 | 结果与范围 |
| --- | --- |
| 后端全量 | `python -m pytest tests -q -p no:cacheprovider`：82 通过，63.88 秒；3 条依赖弃用警告 |
| Hermes 接入 | 真实锁定子进程连接本地模型桩，连续七轮工具调用；不是云端任务验收 |
| 前端 | `npm run check`、`npm run build` 通过 |
| 浏览器 | 用户短句、长句的右边缘一致，角色在左；1280、1600、1920 三种宽度通过；并发消息队列、立即显示、历史不重播通过 |
| 短时性能 | 360 帧和三次抽屉交互，帧间隔 P95 16.8ms；不代表长期稳定性 |
| 真实模型表达 | deepseek-flash，三段合成协作表达通过规则校验，55/71/70 字，耗时 2.61/4.36/2.05 秒；只测表达，不执行文件任务 |

测试修复：Windows 临时目录在 C 盘、项目在 D 盘时，相对路径测试此前直接报错。现在先切到临时目录的父目录，并使用绝对 Hermes 源路径，仍然验证相对运行目录能正确解析。

浏览器证据：[报告](../validation/terminal-ui/report.json)、[1280 截图](../validation/terminal-ui/alignment-1280.png)、[1600 截图](../validation/terminal-ui/alignment-1600.png)、[1920 截图](../validation/terminal-ui/alignment-1920.png)。

模型证据：[三段原文与耗时](../validation/collaboration-expression/report.json)，复现命令为 `python -X utf8 tools/collaboration_expression_acceptance.py`。它只读真实模型配置，在独立数据目录使用合成场景与基础终端角色卡，不覆盖用户人设。

## 仍有限制

三段输出短于 120 字，不等于自然度已通过。例如首句仍有“先认同再解释”的模式，答复提出了场景之外需要进一步确认的扫描原因；这些不能写成已知工具事实，也不能据此宣称人物问题已经解决。后续应继续做匿名对照评价，当前人工验收仍待完成。

本轮没有重复完整 bat 桌面入口、全部云端文件任务、三档 DPI 和长时间性能验收；此前结果见 [终端验收](TERMINAL_ACCEPTANCE_20260917.md)，不得当作本次新增证据。

## 当前规模和阅读入口

按目录顶层自有源码物理行统计，包含注释和空行：后端 35 文件、8,289 行；前端 15 文件、4,763 行；测试 17 文件、约 1,920 行；工具 19 文件、2,858 行。不计 vendor、依赖环境、构建产物及文档，也不是有效业务语句数。services.py 从 3,511 行降为 1,566 行。

后端仍是桌面运行核心，Docker 仅为可选部署方式。先读 [技术报告](TECHNICAL_REPORT.md) 建立全貌，再按 [代码讲解](CODE_WALKTHROUGH.md) 跟请求；面试组织见 [面试指南](INTERVIEW_GUIDE.md)，清理取舍见 [项目组成](PROJECT_COMPONENTS.md)。
