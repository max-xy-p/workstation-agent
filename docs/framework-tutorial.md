# 教学重构：用官方组件搭建工作站 Agent

本教程对应真实项目代码。主线是理解官方组件如何连接，并通过可运行入口验收。
自定义代码承担工作区、安全和业务配置，标准 Agent 循环交给 LangChain / LangGraph。

## 第一课：官方 Agent 工厂

阅读 `src/workstation_agent/agent/factory.py`。核心结构是：

```python
from langchain.agents import create_agent

# model 是官方 ChatModel，tools 是官方 BaseTool 列表。
graph = create_agent(model=model, tools=tools)
```

这一步返回 LangGraph 图，已经包含模型调用、工具调用、消息更新和结束路由。
项目没有再写一份 `while` 或 `for` Agent 决策循环。
应用中的 `async for graph.astream(...)` 只是消费框架执行更新。

[官方 Agent 文档](https://docs.langchain.com/oss/python/langchain/agents)
和 [框架关系](https://docs.langchain.com/oss/python/langgraph/overview)。
旧 `langgraph.prebuilt.create_react_agent` 已弃用，本项目使用当前 `create_agent`。

## 第二课：模型接入

阅读 `models/providers/openai.py`。它创建官方 `langchain_openai.ChatOpenAI`：

```python
model = ChatOpenAI(
    model=settings.model,
    api_key=settings.api_key,
    base_url=settings.model_base_url,
    use_responses_api=False,
)
```

本项目选用 Chat Completions 兼容接口，供应商需要支持工具调用。
`base_url` 是服务根地址，通常以 `/v1` 结尾，SDK 会组合 `/chat/completions`。
不同兼容服务的模型能力、错误格式和附加参数可能不同，仍需要真实服务验收。
不手写 HTTP 请求、Assistant/Tool 消息转换或供应商回复解析。

依赖新增 `langchain`、`langchain-openai`；底层 OpenAI SDK 随集成包安装。
相比自写循环和供应商协议，这增加安装依赖；真实请求按所选模型服务计费。
没有强制 LangSmith 账户、云部署或追踪。

[官方模型集成文档](https://docs.langchain.com/oss/python/integrations/chat/openai)
及 [Chat Completions 接口](https://developers.openai.com/api/reference/python/resources/chat/subresources/completions/methods/create)。

## 第三课：原生工具与工作站服务

阅读 `tools/langchain.py` 和 `tools/mcp_client.py`。
能力目录转为官方 `StructuredTool`，其参数 schema 来自工作站声明。
MCP 工具的 coroutine 使用官方 MCP SDK 发起请求，服务器仍执行参数和权限检查。

```text
StructuredTool → MCP Client → ToolApplication → LangGraph 工具流程 → ToolExecutor
```

为什么还保留执行器：框架的工具调用路由不能替代本项目的工作区限制和权限规则。
业务失败转换为官方 `ToolException`，工具配置 `handle_tool_error=True` 后，框架生成错误 ToolMessage。
权限拒绝需要停止任务，不能让模型反复请求自动批准。
成功结果同时保存到 ToolMessage 的 artifact，供程序读取结构化结果。

本机服务使用 MCP SDK 2.x。桥接直接使用当前官方 SDK，再转换为 StructuredTool；
该桥接只处理连接和结果契约，不实现 MCP 协议，也不实现 Agent 循环。

## 第四课：观察框架执行

完成模型配置，在仓库根目录的 PowerShell 7 运行：

```powershell
uv sync
$env:WORKSTATION_AGENT_MODEL = '你的支持工具调用的模型名称'
$env:WORKSTATION_AGENT_BASE_URL = 'https://你的服务地址/v1'
$env:WORKSTATION_AGENT_API_KEY = '你的模型服务密钥'
$service = & .\scripts\start-local-service.ps1
uv run workstation-agent doctor --agent
uv run workstation-agent run "读取 .python-version，并根据实际内容回答 Python 版本" --trace
```

`--trace` 中应看到 `model`、`tools`，随后再次出现 `model`，还有调用次数限制中间件的节点。
最终 stdout 是 Run 的 JSON，包含 status、answer 和步骤，不输出整个框架消息历史。
如果模型无需工具就直接回答，可能只出现模型节点；文件事实任务应检查是否确实出现工具步骤。

验收错误路径：

```powershell
uv run workstation-agent run "读取 ../outside.txt，并明确说明工具结果" --trace
uv run workstation-agent run "读取不存在的文件 no-such-file.txt，并说明失败原因" --trace
```

预期工具步骤失败，模型可以解释失败，任务本身仍可 completed；不能把这种结果理解为文件读取成功。
停止服务后再调用，预期明确初始化失败，不回显令牌。

```powershell
$service.Kill()
$service.WaitForExit()
```

教学调试也可以省去独立服务：`run ... --transport local`。
两种传输都通过 Application 与 ToolExecutor，参数校验和权限要求一致。

## 第五课：调用上限与测试

阅读 factory 中官方 `ModelCallLimitMiddleware` 和 `ToolCallLimitMiddleware`。
它们限制单次任务的模型和工具调用，不是自定义的循环计数器。

```powershell
uv run python -m unittest discover -s tests -v
uv run workstation-agent doctor
uv build
```

Agent 单元验收使用官方 FakeMessagesListChatModel 的测试扩展，仅重放回复。
HTTP 集成夹具验证真实 SDK 请求和 MCP 跨进程工具执行，不假装是真实模型推理。
外部模型未配置时仍能离线执行这些测试。

## 后续课程与边界

1. 使用官方 Checkpointer 建立会话并验收恢复。
2. 使用官方中断/审批机制实现安全工具恢复。
3. 用框架流式事件连接实际用户界面。
4. 将 Memory 与 RAG 分别持久化、建立真实入口，再从 Context 注入。
5. 如需独立 Agent API 服务，再使用 `langgraph-sdk` 管理线程和运行。

这些是后续交付，不在本课中用占位函数标记为完成。
