# AzurJuus

AzurJuus 是一个在 Windows 本机运行的 JUUS 风格 Agent 桌面应用。它把角色化聊天、群聊协作、朋友圈和受控的工作任务放在同一个界面中；模型负责理解请求和提出下一步，应用负责路由、权限、执行记录和状态恢复。

## 项目定位

这是一个个人兴趣和学习项目，重点是研究“角色化交流”和“可核验的任务执行”如何共存。聊天可以保持轻量的角色扮演，涉及文件、命令、浏览器或桌面操作时，则进入独立的任务流程。

项目采用分层角色扮演：

1. **身份与世界层**：保存角色的基础资料、原作背景、世界观和用户编辑内容。这些内容决定角色是谁，但不会凭空生成共同经历。
2. **认知与经历层**：记录角色实际看到的对话、完成的任务、关系判断、目标和生活状态。认知结果可以影响注意重点和做事方式，但不能改写已经发生的事实。
3. **表达层**：负责把当前可见事实组织成角色消息。表达模型不接触工具权限，也不把执行引擎的内部步骤直接当成台词。
4. **任务执行层**：任务调度器、DeepSeek Harness 和工具权限网关负责计划、调用和交付。模型可以提出动作，应用仍会检查授权范围；高风险操作需要秘书复核和用户确认。
5. **界面与记录层**：聊天、工作记录、审批、产物和运行事件都持久化，界面显示可追溯的状态，而不是只显示一段模型回复。

这种分层让“角色在聊天中怎么说”和“系统实际做了什么”保持边界：闲聊不会自动获得工具权限，任务结果也不会绕过验收直接变成角色台词。

## 当前界面

- 左侧深海蓝导航在“通讯”和“动态”之间切换；顶部显示 JUUSTAGRAM 和本地服务连接状态。
- 通讯页由会话列表、聊天面板和底部输入区组成。私聊会以角色立绘作为低对比度背景，群聊使用独立的协作场景。
- 蓝白色半透明面板、圆角卡片、分层阴影和短暂的进入反馈组成统一的视觉主题。输入区、表情菜单、附件菜单和 @提及菜单保持在消息层之上。
- 私聊可以调整立绘的水平位置、垂直位置和缩放，设置按角色保存；窗口变窄时切换为单列聊天，输入区仍保持可见。
- 消息支持流式显示、历史分批加载、附件、表情和群聊提及。本人消息、路由类型和工作状态会单独标识。
- “工作记录”抽屉展示任务计划、阶段、秘书复核、审批、分工、产物和验收意见；“方法与成长”面板展示已验证的方法及其隔离试用记录。
- “动态”页支持查看分页动态、打开详情、发布、点赞和评论，并按分享范围区分可互动内容和仅供查看的记录。
- 设置面板集中管理模型连接、工作目录、连接成员、任务参与权限、人物资料、认知状态和外观选项。

桌面默认使用软件渲染，主要用于规避部分 WebView/GPU 合成造成的花屏。软件模式保留蓝色半透明材质，但不对每个气泡和卡片实时模糊背景；将 AZURJUUS_DESKTOP_RENDERER=auto 写入 .env 后可改用 Qt 自动选择的完整磨砂渲染。系统或应用开启“减少动态效果／减少透明度”时，会相应减少动画或改用更不透明的面板。

## 功能范围

| 模块 | 当前能力 | 使用边界 |
| --- | --- | --- |
| 私聊与群聊 | 自动判断闲聊、结果追问、任务、协作或澄清，也可手动选择处理方式 | 自动判断只负责路由，不直接授予工具权限 |
| 任务与协作 | 计划、依赖、阶段执行、成员分工、外援、暂停、恢复、交付和最终复核 | 任务结果仍需要按实际产物和证据验收 |
| 文件与文档 | 读取和修改文本，读写 DOCX/XLSX，读取普通及扫描 PDF | OCR、复杂版式、低清或手写内容需要人工核对 |
| 编程与命令 | 在授权工作区内修改文件、执行命令、查看输出和退出码 | 命令、覆盖写入和其他高风险操作按条审批 |
| 浏览器与桌面 | Playwright 浏览器操作，Windows UI Automation 和资源管理器操作 | 不承诺任意网站稳定自动化，不包含 Office/WPS GUI 操作 |
| 人物状态 | 角色卡、可见经历、关系、长期目标、在线生活和方法偏好 | 这是可修正的运行状态，不是对模型内部思维的展示 |
| 朋友圈 | 人物自主发布或回应动态，用户可发布、点赞和评论有权限的内容 | 私下活动不会自动公开；分享范围由参与者分别决定 |

## 技术结构

| 层 | 主要技术 | 作用 |
| --- | --- | --- |
| 桌面壳 | Python、PySide6、Qt WebEngine | 无边框窗口、缩放、软件／自动渲染切换和本地服务生命周期 |
| 前端 | Vue 3、TypeScript、Vite | 通讯、动态、设置、工作抽屉、人物和认知面板 |
| 后端 API | FastAPI、Uvicorn | 消息、任务、审批、事件、附件和 WebSocket 接口 |
| 调度与执行 | RunCoordinator、官方 DeepSeek Harness SDK、Cordis | 任务状态推进、模型会话、原生插件和工具调用 |
| 权限与存储 | Python 权限网关、SQLAlchemy、SQLite、RunStore、FTS5 | 工作区授权、审批、聊天、运行事件、产物和检索 |
| 文档与自动化 | pypdf、pypdfium2、RapidOCR、python-docx、openpyxl、Playwright、pywinauto | 文档处理、扫描 PDF OCR、浏览器和 Windows 自动化 |

模型会话由官方 Harness 运行时管理；本地 Python 服务保存任务和事件，并在工具调用前检查路径、命令和授权。项目没有额外的 MCP 中转层。内置 Cordis 内核负责插件、事件和清理，前端通过 HTTP/WebSocket 读取同一份运行记录。

## 环境要求

- Windows 11（当前开发和桌面验证环境）
- Python 3.11–3.13（已验证 Python 3.12）
- Node.js 22 LTS
- Git
- 一个支持工具调用的模型服务；使用视觉定位时还需要模型支持图像输入

## 安装

在项目根目录执行：

~~~powershell
git clone https://github.com/DustStarT/AzurJuus.git
cd AzurJuus
python tools/setup_runtime.py
~~~

安装脚本会创建项目虚拟环境、安装 Python 和前端依赖、准备浏览器运行时并构建前端。安装后可以运行环境自检：

~~~powershell
.\.venv\Scripts\python.exe -X utf8 tools/doctor.py
~~~

## 运行

启动桌面应用：

~~~powershell
.\.venv\Scripts\python.exe -X utf8 desktop.py
~~~

也可以双击 launch_azurjuus.vbs，或使用 launch_azurjuus.bat。如果只需要本地网页服务：

~~~powershell
.\.venv\Scripts\python.exe server.py
# http://127.0.0.1:4173
~~~

首次运行后，在左下角“设置”中填写模型接口地址、模型 ID、API Key，并选择允许任务使用的工作目录。也可以复制 .env.example 作为环境配置样例。Windows 上的 API Key 会使用 DPAPI 加密保存；接口只返回是否已配置，不返回明文。

前端源码修改后重新构建：

~~~powershell
npm ci
npm run build
~~~

## 项目结构

~~~text
azurjuus/
├─ backend/
│  ├─ api/                 HTTP 路由和输入契约
│  ├─ characters/          角色资料、关系和终端配置
│  ├─ chat/                会话、表达、附件和消息路由
│  ├─ mind/                认知、记忆、目标和在线生活
│  ├─ social/              群聊、朋友圈和分享范围
│  ├─ tasks/               任务调度、Harness、权限、复核和方法成长
│  ├─ platform/            本地设置、时间、事件和运行时支持
│  └─ app.py / services.py / models.py
├─ frontend/
│  ├─ features/chat/       会话列表、聊天、附件、表情和背景设置
│  ├─ features/social/     朋友圈列表、详情和发布
│  ├─ features/tasks/      工作抽屉和方法面板
│  ├─ features/characters/ 角色卡、关系图和任务参与
│  ├─ features/mind/       认知、经历和个人生活
│  ├─ features/settings/   港区和外观设置
│  └─ App.vue / theme.css / styles/
├─ kernel/                 Cordis 运行时和原生工作区插件
├─ resources/              角色、世界观和界面素材
├─ tools/                  安装、自检、导入和验收脚本
├─ tests/                  后端及内核回归测试
├─ deployment/optional/    可选的 PostgreSQL、Redis、Chroma 配置
├─ desktop.py              PySide6 桌面入口
├─ server.py               纯网页服务入口
├─ launch_azurjuus.*       Windows 启动脚本
└─ package.json / requirements*.txt / .env.example
~~~

角色素材的字段和导入方式见 resources/characters/README.md。

人物从设定、理解与行动选择到表达、反思和长期记忆的完整流程，以及旧数据的兼容策略，见 [人物心智与表达连续性](CHARACTER_MIND.md)。

## 验证

在项目目录执行：

~~~powershell
# Python 回归测试
.\.venv\Scripts\python.exe -m pytest tests -q

# 前端类型检查与构建
npm run check
npm run build

# Cordis 内核和原生工具契约
npm run test:kernel
~~~

tools/ 中还提供浏览器界面、PySide6/QWebEngine、OCR、社交和真实模型验收脚本。涉及真实模型或桌面窗口的脚本会读取当前配置并产生实际调用，应在隔离数据和授权目录中运行。

## 已知边界

- 模型输出、人物自然度和长期行为仍需要人工观察。
- 扫描 PDF、复杂 Office 版式、外部网站和桌面应用的自动化结果需要按实际文件和窗口复核。
- 角色素材来自社区资料整理，基础卡和关系研究可能需要继续校对；用户编辑内容和已经发生的任务事实优先保留。

## 许可与素材

本项目代码使用 [MIT License](LICENSE)。角色资料整理自[碧蓝航线 BWIKI](https://wiki.biligame.com/blhx/)，相关角色设定、台词和美术素材的权利归原权利方所有，不属于本项目代码的 MIT 授权范围。
