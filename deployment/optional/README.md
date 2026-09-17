# 可选服务器组件

普通 Windows 桌面不需要本目录，也不需要启动 Docker。桌面入口仍为 `launch_azurjuus.bat`。

这里保留 PostgreSQL、Redis、Chroma 的历史服务器部署配置，供有明确多进程或外部存储需求时使用。本轮没有运行这些服务器，不能将保留配置理解为已完成服务器部署验收。

从项目根目录执行：

```powershell
.\.venv\Scripts\python.exe -m pip install -r deployment/optional/requirements.txt
docker compose -p azurjuus -f deployment/optional/compose.yml up -d
```

`-p azurjuus` 保持原根目录 Compose 默认项目名，避免移动配置后误用另一套数据卷。若以前自定义过项目名，应沿用原值。已有容器、数据卷及真实 `.env` 均未删除或修改。

只有确实启用服务器时，才在 `.env` 中配置对应的数据库、Redis、Chroma 地址；否则保留桌面示例配置即可。文件中的默认密码与端口是本地开发示例，不能直接用作公网部署配置。
