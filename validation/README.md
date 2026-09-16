# 界面截图

本目录只保留用于展示的界面实拍截图，全部来自真实运行的界面（`tools/ui_acceptance.py` 驱动，1280×720 / 1600×900 / 1920×1080 等尺寸），任务与聊天内容均为**隔离的合成样例**，不含真实用户数据。

| 截图 | 界面 |
| --- | --- |
| [initial.png](ui/initial.png) | 私聊：角色列表、会话与立绘背景 |
| [group-1600x900.png](ui/group-1600x900.png) | 群聊：港区协作频道 |
| [moments-1600.png](ui/moments-1600.png) | 动态总览 |
| [moment-detail-1600.png](ui/moment-detail-1600.png) | 动态详情与评论 |
| [work-drawer-1600.png](ui/work-drawer-1600.png) | 工作抽屉：分工、调用、审批与交付 |
| [settings-1600.png](ui/settings-1600.png) | 设置对话框 |

> 说明：原先存放在本目录的交互录屏（`.webm`）、多分辨率重复截图、JUnit 报告与验收 JSON 已在开源前清理，以缩减仓库体积并避免验收报告里残留本机绝对路径。完整的实现状态、测试结论与未覆盖范围见 [实施与验收记录](../docs/IMPLEMENTATION.md)；界面规范见 [UI 规范](../docs/UI.md)。

## 复现截图

```powershell
.\.venv\Scripts\python.exe tools/ui_test_server.py
# 另一个终端：
.\.venv\Scripts\python.exe -X utf8 tools/ui_acceptance.py
```
