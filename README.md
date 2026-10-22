# Workstation Agent

面向个人工作站的单 Agent 项目，使用 LangChain / LangGraph 执行任务，提供 CLI、本机 MCP 工具服务、Agent HTTP 接口和 React 工作台。持久化使用 PostgreSQL。

## 快速开始

需要 Python 3.13+、uv 和 PostgreSQL。先按 [数据库部署](docs/database.md) 配置连接并运行 `init-db`，再配置支持工具调用的 OpenAI 兼容模型：

```powershell
cd backend
uv sync
$env:WORKSTATION_AGENT_WORKSPACE = (Resolve-Path ..).Path
$env:WORKSTATION_AGENT_MODEL = '你的模型名称'
$env:WORKSTATION_AGENT_BASE_URL = 'https://你的模型服务/v1'
$env:WORKSTATION_AGENT_API_KEY = '你的密钥'
# 替换为模型的真实上下文窗口。
$env:WORKSTATION_AGENT_CONTEXT_WINDOW = '32768'
uv run workstation-agent run "读取 backend/.python-version，说明项目需要的 Python 版本" --transport local
```

以上命令从仓库根目录开始执行。成功后输出包含 `status`、`answer` 和执行步骤的 JSON。
项目不会自动读取 `.env`；配置示例见 [后端配置](backend/.env.example)。
示例将整个仓库设为 Agent 工作区；未配置 `WORKSTATION_AGENT_WORKSPACE` 时使用启动进程的当前目录。

## 打开工作台

需要 Node.js 22.12+。先按 [HTTP 文档](docs/agent-service.md#启动与认证) 启动 Agent 服务，再在另一个终端从仓库根目录运行：

```powershell
cd frontend
npm ci
npm run dev
```

打开 `http://127.0.0.1:5173`，点击“连接本机 Agent”并输入 HTTP 令牌。
也可选择页面演示，无需模型和数据库。前端启动、检查和目录说明见 [开发指南](docs/development.md#前端工作台)。

## 使用入口

前端代码和 Node 配置在 `frontend/`；后端源码、测试、脚本和 Python 配置在 `backend/`。
`docs/` 保存公共文档，根目录的 `compose.yaml` 提供 PostgreSQL 部署配置；详细目录见 [开发指南](docs/development.md#目录与命令位置)。

| 需求 | 文档 |
| --- | --- |
| CLI 任务、会话续聊、工具、MCP 服务、审批与取消 | [Agent 使用](docs/agent.md) |
| HTTP 任务、运行状态、会话和消息 CRUD | [HTTP 接口](docs/agent-service.md) |
| 记住偏好与项目决策 | [长期记忆](docs/memory.md) |
| 摄取文档、检索与引用 | [知识库](docs/rag.md) |
| 模型请求预算与任务状态栏 | [上下文管理](docs/context.md) |

工具只在配置工作区内访问文件，默认只读；RAG、记忆写入及额外模型出站分别授权。外部模型的实际效果需要用自己的服务和任务验证。

[文档目录](docs/index.md) · [功能状态](docs/modules.md) · [架构](docs/architecture.md) · [开发指南](docs/development.md) · [开发约定](AGENTS.md)
