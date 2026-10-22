# Workstation Agent 后端

Python 工程提供 Agent 执行、CLI、MCP 工具服务和 HTTP 接口，业务数据保存在 PostgreSQL。
源码位于 `src/workstation_agent/`，测试、示例和启动脚本分别位于 `tests/`、`examples/` 和 `scripts/`。

在本目录运行 `uv sync` 安装依赖，运行 `uv run workstation-agent doctor` 检查配置。
运行完整服务前，请按仓库的 [快速开始](../README.md) 配置数据库、模型和工作区；
测试、类型检查与打包命令见 [开发指南](../docs/development.md)。

前端是同级的独立工程 `frontend/`，通过 HTTP 调用后端，不参与 Python 打包。
