# Workstation Agent

面向个人工作站的单 Agent 项目，使用 LangChain / LangGraph 执行任务，提供 CLI、本机 MCP 工具服务和 Agent HTTP 接口。持久化使用 PostgreSQL，前端尚未实现。

## 快速开始

需要 Python 3.13+、uv 和 PostgreSQL。先按 [数据库部署](docs/database.md) 配置连接并运行 `init-db`，再配置支持工具调用的 OpenAI 兼容模型：

```powershell
uv sync
$env:WORKSTATION_AGENT_MODEL = '你的模型名称'
$env:WORKSTATION_AGENT_BASE_URL = 'https://你的模型服务/v1'
$env:WORKSTATION_AGENT_API_KEY = '你的密钥'
# 替换为模型的真实上下文窗口。
$env:WORKSTATION_AGENT_CONTEXT_WINDOW = '32768'
uv run workstation-agent run "读取 .python-version，说明项目需要的 Python 版本" --transport local
```

成功后输出包含 `status`、`answer` 和执行步骤的 JSON。项目不会自动读取 `.env`；配置示例见 [.env.example](.env.example)。

## 使用入口

| 需求 | 文档 |
| --- | --- |
| CLI 任务、会话续聊、工具、MCP 服务、审批与取消 | [Agent 使用](docs/agent.md) |
| HTTP 任务、运行状态、会话和消息 CRUD | [HTTP 接口](docs/agent-service.md) |
| 记住偏好与项目决策 | [长期记忆](docs/memory.md) |
| 摄取文档、检索与引用 | [知识库](docs/rag.md) |
| 模型请求预算与任务状态栏 | [上下文管理](docs/context.md) |

工具只在配置工作区内访问文件，默认只读；RAG、记忆写入及额外模型出站分别授权。外部模型的实际效果需要用自己的服务和任务验证。

[文档目录](docs/index.md) · [功能状态](docs/modules.md) · [架构](docs/architecture.md) · [开发指南](docs/development.md) · [开发约定](AGENTS.md)
