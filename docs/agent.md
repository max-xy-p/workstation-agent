# Agent 运行机制：官方 LangChain / LangGraph 实现

本次为 0.2.0 破坏性重构，不保留旧 `Agent.run()`、`ModelBackend` 或兼容导出。
标准模型与工具循环由 `langchain.agents.create_agent` 生成的 LangGraph 图执行。
教学顺序和逐步验收见 [框架教学](framework-tutorial.md)。

## 真实入口

```powershell
uv run workstation-agent run "读取 .python-version 并解释项目需要的 Python 版本" --trace
```

默认通过 MCP 调用独立本机工具服务；`--transport local` 可在同进程使用相同权限执行器。
配置和启动命令见 [README](../README.md) 与 [本机服务](local-service.md)。
模型服务必须支持 OpenAI 兼容的 `/chat/completions` 和工具调用。

## 组装关系

```text
CLI / 后续界面 → build_agent_application → AgentApplication.run_task
    → ContextBuilder 组装消息
    → 官方 create_agent 图
        → 官方 ChatOpenAI 模型集成
        → 官方 StructuredTool
            → 官方 MCP SDK → Workstation ToolApplication → ToolExecutor → 文件工具
    → 返回领域 Run
```

`agent/factory.py` 组装官方 Agent、调用次数限制和重复 RAG 查询防护中间件。
OpenAI 兼容集成限制在 models/providers/，包括聊天与 RAG 模型适配器。
`tools/mcp_client.py` 管理 MCP 连接；`tools/langchain.py` 将能力目录和结果转换为官方工具。
`application/service.py` 消费图更新、组装上下文并返回业务结果，不自行调用模型或决定循环分支。

## 状态、消息与错误

Agent 内部直接使用框架消息和图状态；Assistant 消息保留结构化 `tool_calls`，
ToolMessage 保留 `tool_call_id`，框架据此把结果交回模型。
领域 Run 只汇总任务状态和完成步骤，不承担框架状态机的职责。

- 正常最终消息：`completed`。
- 参数、文件和路径等工具错误：错误 ToolMessage，允许模型解释或修正；任务仍可能正常完成。
- 工作站返回需要审批：停止当前任务，`waiting_approval`；本阶段未交付审批恢复。
- 官方模型、工具或 RAG 检索次数限制，以及相同参数的重复检索：`limit_reached`。
- 网络、模型或其他运行异常：安全的 `failed` 说明，不回显原始异常。
- 协程取消：标记 `cancelled` 后继续传递取消信号；已经开始的工具线程未必立即停止。
- 空任务：调用前拒绝，不创建 Run。

`--trace` 输出官方图节点名称，包括模型、工具和中间件节点，不输出参数、Prompt 或工具内容。
这是节点完成进度，不是模型 token 流。
Step 不保存工具原始参数或输出，避免默认审计泄漏内容。最终答复仍返回调用方。

## 限制与持久化

- `WORKSTATION_AGENT_MAX_STEPS`：单次模型调用上限，默认 8。
- `WORKSTATION_AGENT_MAX_TOOL_CALLS`：单次工具调用上限，默认 32。
- `WORKSTATION_AGENT_MODEL_TIMEOUT`：模型请求与 MCP 读取超时，默认 30 秒。
- 底层图递归限制为中间件留出执行余量；业务调用次数仍由官方中间件限制。
- 模型客户端重试次数为 0，图不自动重试工具副作用。

默认每次任务独立；--session 或 SESSIONS_ENABLED 可启用官方 SQLite Checkpointer 跨任务续聊，不能恢复工具审批。
可通过 `AgentApplication(repository=...)` 显式保存业务 Run；默认入口不保存任务、消息或答复。
SQLite RunRepository 的 round-trip 和显式保存测试保留，但它不是 LangGraph Checkpointer。
Memory 已通过受控查询门面装配到 ContextBuilder，动态内容按次注入模型请求，不持久化到会话历史。
RAG 已通过默认 search_documents 工具接入，证据使用 Context 模块统一格式。
先通过 CLI 摄取文档，再由单 Agent 按证据决定后续查询，详见 [RAG 文档](rag.md)。
显式记忆写入/删除、会话清除和默认关闭的自动提取见 [记忆使用与验收](memory.md)。用户界面与审批恢复仍待交付。

## 验收依据

- 原有 Agent 测试已改为官方 ChatModel 消息重放，验证工具调用、关联标识和调用次数限制。
- `tests/integration/test_framework_agent.py` 启动本机兼容 HTTP 接口夹具和独立 MCP 服务，
  用实际 ChatOpenAI / OpenAI SDK 请求验证模型 → 工具 → 模型完整协议链。
- 参数和越界错误返回模型；供应商 HTTP 错误转为安全的任务失败。
- 这些离线测试验证接口和编排，不代表已验证某个外部模型的推理质量。
