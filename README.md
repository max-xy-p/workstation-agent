# Workstation Agent

基于 **LangChain + LangGraph** 的单 Agent 项目，按模块提供真实入口、完整调用链和中文教学文档。
0.2.0 已移除旧手写 Agent 循环和兼容 API。

## 运行 Agent

配置支持工具调用的 OpenAI 兼容模型，在仓库根目录的 PowerShell 7 运行：

```powershell
uv sync
$env:WORKSTATION_AGENT_MODEL = '你的模型名称'
$env:WORKSTATION_AGENT_BASE_URL = 'https://你的模型服务地址/v1'
$env:WORKSTATION_AGENT_API_KEY = '你的模型服务密钥'
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

## 配置与边界

环境变量示例见 [.env.example](.env.example)，项目不会自动读取 `.env`。
模型名称必须显式配置；base_url 默认 `https://api.openai.com/v1`。
API key 从 `WORKSTATION_AGENT_API_KEY` 读取，也可使用 `OPENAI_API_KEY`。
默认模型调用上限 8、工具调用上限 32、请求超时 30 秒。

当前交付是官方 Agent 的单次任务和只读工具链；Memory/RAG 为内存基线，持久化会话、审批恢复和用户界面待逐模块交付。
HTTP 集成测试使用本机兼容接口夹具，外部模型实际效果需要使用自己的服务配置验收。

## 开发

```powershell
uv run python -m unittest discover -s tests -v
uv run workstation-agent doctor
uv build
```

[文档索引](docs/index.md) · [功能状态](docs/modules.md) · [架构](docs/architecture.md) · [开发约定](AGENTS.md)
