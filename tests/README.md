# 本地测试目录

`conftest.py` 集中隔离数据库、人物心智与群聊场景的共享夹具。其余测试按被验证的领域放在 `platform/`、`characters/`、`chat/`、`mind/`、`social/`、`tasks/`。跨领域回归放在最接近故障入口的目录，例如任务结果进入闲聊的回归位于 `tasks/`。

历史领域测试默认关闭交互阶段门禁，便于分别验证原有行为；`tasks/test_task_reviews.py` 显式启用门禁，覆盖秘书与用户审核、恢复、过期确认、依赖和风险操作。产品默认启用，不以测试夹具配置作为运行默认。`tasks/test_method_autonomy.py` 使用受控模型及真实隔离文件验证检索、组合、演化和三场景基线比较；`tasks/test_pdf_ocr.py` 实际执行本地中英文扫描 PDF 识别。新增按钮由 `tools/functional_gaps_acceptance.py` 在独立临时数据上操作真实浏览器验证。

从项目根目录运行 `./.venv/Scripts/python.exe -m pytest tests -q`；专项测试可指定领域目录。测试源码纳入版本控制，临时数据库与 `validation/` 验收产物不纳入版本控制。离线测试不使用真实运行数据；云端、桌面验收脚本会分别调用当前配置的真实模型、操作隔离的本机窗口。
