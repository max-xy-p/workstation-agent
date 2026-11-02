# 架构

项目面向个人工作站的单 Agent。标准模型与工具循环使用 LangChain `create_agent`，
执行状态、检查点和中断由 LangGraph 管理；当前范围见 [功能状态](modules.md)。

## 模块与依赖

前端是 `frontend/` 中的独立工程；下列 Python 模块均位于 `backend/src/workstation_agent/`。

```text
CLI / HTTP / 后续通信适配器
  → api 请求契约 → application 用例
                      → agent / context → domain
                      → Protocol ← storage / models/providers 等外围实现
```

| 模块 | 职责与边界 |
| --- | --- |
| `frontend/` | 独立 React 工程，通过同源 HTTP 访问用例；不导入后端源码或访问数据库 |
| `api/` | 与 HTTP 无关的请求和结果契约，不决定业务行为 |
| `interfaces/`、CLI | 校验通信请求、认证和转换响应，只调用 Application |
| `application/` | 协调任务、会话、CRUD、审批和状态；通过明确协议调用外围能力 |
| `domain/` | 稳定的 Run、Step、会话和文本对象，不依赖框架、数据库或 UI |
| `agent/` | 组合官方图与中间件，不直接访问文件、数据库或网络 |
| `context/` | Memory/RAG 进入 Prompt 的唯一组装入口；管理预算、选择和任务状态 |
| `memory/`、`rag/` | 分别保存用户经验与外部文档证据，不混用 |
| `tools/`、`safety/` | 所有副作用的参数、权限、工作区、停止信号及输出检查 |
| `storage/`、`models/providers/` | 通过协议实现 PostgreSQL 和模型 SDK 适配，由 bootstrap 组装 |

运行调用链为 Application → 官方图 → ChatModel / StructuredTool →
同进程或 MCP → ToolApplication → ToolExecutor。图更新流只用于消费进度，不构成另一套 Agent 循环。
普通会话 CRUD 通过应用用例和内部授权操作执行，不注册为模型可发现工具，也不自动调用模型。

## 状态与数据

| 数据 | 用途与保存位置 |
| --- | --- |
| Run / Step | PostgreSQL 保存单次任务状态、已观察步骤、进度和结果；框架检查点负责恢复，两者分别管理 |
| 框架检查点 | 会话消息、待执行节点、审批、截止时间及任务状态；官方 PostgresSaver 管理 |
| 会话与文本历史 | 用户可管理的标题、版本、用户输入和最终答复；独立业务表，支持分页和历史修改 |
| Memory | 可跨会话复用的偏好、决策与经验；自然语言 TEXT 正文是内容来源 |
| RAG | 原文 Document、来源位置、派生前缀与向量；完整文档快照发布 |

业务历史不代替检查点恢复，检查点裁剪也不删除业务文本。
修改历史先清除检查点，再在业务事务内修改文本；下次从保留文本重建上下文。
两者不共享同一事务，故障边界见 [HTTP 文档](agent-service.md#错误与保留范围)。
默认 CLI 仅显式会话持久化；HTTP 始终持久化。SQL、连接池和迁移集中在外围存储，不使用 ORM。
CLI 和 HTTP 的运行轨迹均由应用自动保存，因此执行 Agent 任务需要 PostgreSQL；纯文件工具不受此要求影响。
运行写入通过内部授权操作，图更新流提供进度，存储协议负责保存与查询；不为轨迹实现第二套 Agent 循环。
任务状态栏的数据类型位于 `domain/task_status.py`，Context 继续负责组装和校验，领域运行对象不反向依赖 Context。
官方检查点不提供本项目按身份管理的运行历史分页、删除与消息关联，因此这些规则保留在 Application 和 RunRepository 中；
执行进度继续复用现有官方图更新流，维护范围仅限业务记录与故障核对，不新增框架状态表或自定义执行器。

动态记忆按次注入，不追加到会话历史；RAG 由 Agent 按需检索。
检索结果标记为数据，不拥有系统指令或工具授权的优先级。
检查点清除工具正文、artifact 和已知秘密，但这些措施不等于通用敏感信息识别或磁盘加密。

## 框架选择与必要扩展

| 能力 | 复用组件 | 项目保留的规则 |
| --- | --- | --- |
| Agent 与模型 | create_agent、官方调用限制、ChatOpenAI | 配置、结果转换与运行截止时间 |
| 审批与停止 | interrupt、Command、aupdate_state | 执行器按实际参数检查权限，恢复验证指纹；线程结束前保留会话锁 |
| 请求管理 | ClearToolUsesEdit、SummarizationMiddleware、trim_messages | 完整请求预算、来源版本去重、有界刷新和用户验收 |
| 记忆提取 | LangMem create_memory_manager | 原文证据、范围、版本、整批提交及遗忘保护 |
| 文档处理 | MarkdownIt、RecursiveCharacterTextSplitter | 原文位置、稳定 ID、章节关联和分块校验 |
| 检索 | BM25Plus、InMemoryVectorStore、EnsembleRetriever、ContextualCompressionRetriever | 可信候选范围、ID 检查、引用、证据预算和模型出站授权 |
| 服务与存储 | MCP SDK、Starlette、Uvicorn、Psycopg、PostgresSaver | 本机认证、限量运行句柄、业务表及版本化迁移 |

官方 HumanInTheLoopMiddleware 的工具名配置不能表达服务端执行器基于实际参数的全部授权规则，
因此在官方工具扩展点转换权限要求，不另建审批状态机。
通用 TodoListMiddleware 允许自由标记完成，不能满足本项目的用户目标不可改和用户验收要求，
因此只扩展状态字段校验与工具。框架 Store 的键值操作不能直接代替整批版本检查、遗忘保护和容量限制。
检索协议显式接收已过滤候选并只返回 ID；分块协议补足原文、来源与签名要求，算法仍由成熟组件执行。

维护范围是这些业务约束与框架扩展点的衔接，不维护另一套循环、驱动、协议或检索算法。
后端直接依赖由 `backend/pyproject.toml` 声明，实际版本由 `backend/uv.lock` 固定。
使用内存精确检索会增加每次构建候选索引的 CPU 和内存开销，适合当前小型知识库；
可选摘要、提取、embedding 和重排会增加模型调用、费用及延迟，按各自配置授权。

## 升级约束

保持 [开发约定](../AGENTS.md) 中的类型、权限和依赖方向，持久化变化通过版本化迁移交付。
不保留旧 Agent 循环、兼容导出或重复入口。

- 运行控制：验证参数绑定审批、恢复不重放已成功节点、超时及重复取消时线程先收尾。
- 上下文：捕获最终请求，验证必要内容、工具配对、输出上限和摘要失败降级。
- 存储：验证迁移、范围隔离、历史重建、事务回滚及脱敏序列化。
- RAG：验证 CRLF 与重复文本位置、代码表格保护、块 ID/向量关联及引用。

当前上下文适配按 LangChain 1.4.3、LangGraph 1.2.12 验收；模型 `profile` 仍为 Beta，只读取带默认保护的容量字段。
结构化输出适配使用官方 `schema_specs` 和 `OutputToolBinding`，收尾依赖官方工厂的 `tools` 节点与 `END`。
升级时须验证这些接口，不能只调整版本号。

参考：[官方 Agent](https://docs.langchain.com/oss/python/langchain/agents)、
[中间件](https://docs.langchain.com/oss/python/langchain/middleware/built-in)、
[持久化](https://docs.langchain.com/oss/python/langgraph/persistence)、
[MCP Python SDK](https://py.sdk.modelcontextprotocol.io/)。
