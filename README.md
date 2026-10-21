# Workstation Agent

基于 **LangChain + LangGraph** 的单 Agent 项目，按模块提供真实入口、完整调用链和中文教学文档。
0.2.0 已移除旧手写 Agent 循环和兼容 API。

持久化统一使用 PostgreSQL。运行 Agent 或记忆、知识库命令前，先按
[数据库部署说明](docs/database.md) 配置 `WORKSTATION_AGENT_DATABASE_URL`，执行 `init-db` 和
`doctor --database`。旧 SQLite 文件不会自动导入或删除，重建方式见该文档。

## 运行 Agent

配置支持工具调用的 OpenAI 兼容模型，在仓库根目录的 PowerShell 7 运行：

```powershell
uv sync
$env:WORKSTATION_AGENT_MODEL = '你的模型名称'
$env:WORKSTATION_AGENT_BASE_URL = 'https://你的模型服务地址/v1'
$env:WORKSTATION_AGENT_API_KEY = '你的模型服务密钥'
# 替换为实际模型窗口，32768 仅为示例。
$env:WORKSTATION_AGENT_CONTEXT_WINDOW = '32768'
$service = & .\scripts\start-local-service.ps1
uv run workstation-agent doctor --agent
uv run workstation-agent run "读取 .python-version 并回答项目需要的 Python 版本" --trace
```

Agent 默认通过 MCP 调用独立本机工作站服务，结果输出 JSON。
`--transport local` 可在同进程调用相同工具，用于教学调试。
停止本终端启动的服务：`$service.Kill()`。

教学顺序、官方组件和验收见[框架教学](docs/framework-tutorial.md)。

## 工具与服务入口

```powershell
uv run workstation-agent tools --json
uv run workstation-agent tool read_text_file --path README.md --trace
uv run workstation-agent serve
```

内置目录列举和 UTF-8 文件读取，工具路径受工作区限制。`serve` 前台启动需要服务令牌。
默认地址 `http://127.0.0.1:8765/mcp`。详细说明见[工具模块](docs/tools.md)与[本机服务](docs/local-service.md)。

需要通过 HTTP 提交和管理完整 Agent 任务时，配置独立令牌并启动常驻服务：

```powershell
$env:WORKSTATION_AGENT_HTTP_TOKEN = [guid]::NewGuid().ToString('N')
uv run workstation-agent doctor --http --agent
uv run workstation-agent serve-agent
```

默认地址 `http://127.0.0.1:8766`。该服务启用会话保存；提交、查询、取消和审批恢复见
[Agent HTTP 服务](docs/agent-service.md)。
会话更名、删除和历史消息管理见 [会话与消息接口](docs/conversations.md)。

## 文档检索

~~~powershell
uv run workstation-agent rag ingest docs/tools.md --approve-write
uv run workstation-agent rag search "工具权限检查"
uv run workstation-agent rag status
~~~

默认离线 BM25；配置模型后支持上下文增强、向量混合召回和重排。
使用和配置见 [RAG 文档](docs/rag.md)，总体方案见 [设计与待定事项](docs/rag-design.md)。

## 配置与边界

环境变量示例见 [.env.example](.env.example)，项目不会自动读取 `.env`。
模型名称必须显式配置；base_url 默认 `https://api.openai.com/v1`。
API key 从 `WORKSTATION_AGENT_API_KEY` 读取，也可使用 `OPENAI_API_KEY`。
默认模型调用上限 8、工具调用上限 32、请求超时 30 秒。

当前提供官方 Agent、文件只读工具、RAG 和文本长期记忆。
记忆管理、可选会话续聊及自动提取见 [记忆使用与验收](docs/memory.md)。审批恢复、运行超时与取消见 [运行控制](docs/run-control.md)；用户界面待交付。
HTTP 集成测试使用本机兼容接口夹具，外部模型实际效果需要使用自己的服务配置验收。

## 开发

```powershell
uv run python -m unittest discover -s tests -v
uv run workstation-agent doctor
uv build
```

[文档索引](docs/index.md) · [功能状态](docs/modules.md) · [架构](docs/architecture.md) · [开发约定](AGENTS.md)

上下文预算、分级压缩与任务状态栏见 [上下文管理](docs/context.md)。自定义模型运行前需配置真实上下文窗口。
