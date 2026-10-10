# 开发指南

## 环境

- Python 3.13+
- `uv`
- 运行框架：LangGraph；参数/结果校验：Pydantic；本机服务：MCP SDK、Starlette、Uvicorn
- Windows 后台启动脚本需要 PowerShell 7；详见 [本机服务](local-service.md)
- 依赖及其传递版本由 `uv.lock` 固定，入口和验收见 [工具模块](tools.md)

初始化：

```powershell
uv sync
uv run workstation-agent init-db
uv run workstation-agent doctor
```

测试与构建：

```powershell
uv run python -m unittest discover -s tests -v
uv build
```

## 实现选型：优先复用框架

优先使用项目已采用框架的官方 API 和成熟组件，减少重复实现和长期维护成本。
选型与开发遵循 [开发约定](../AGENTS.md)，按以下顺序推进：

1. 查阅官方文档及现有 API，先通过配置和组件组合满足需求。
2. 需要调整行为时，优先使用框架提供的扩展接口，并通过项目现有 Protocol 接入。
3. 仅为具体业务规则或已验证的能力缺口编写最小必要的自定义逻辑；在改动说明或模块文档中记录缺口、配置或扩展为何不足、自定义范围及维护成本。

这一原则同样适用于 RAG 的文档解析、分块、索引和检索。先评估 LangChain 等已采用框架的组件，
再为来源追溯、上下文增强等项目要求补充必要的适配与扩展。
框架接入仍须遵守模块边界，文件读取、网络调用等副作用必须通过工具和权限策略。

简单辅助逻辑优先使用标准库；已有成熟框架能力时，不以减少依赖为理由重新实现。
新增依赖需说明用途、替代方案和运行成本，并更新依赖锁文件。

重构以目标架构为准，必要时直接调整对象、接口及调用方式，同步迁移调用方和测试。
未明确要求兼容时，不为维持旧结构增加适配层、兼容导出或重复运行链。
持久化格式变化通过版本化 migration 引入；派生索引可明确要求从原始文档重建，不解码旧对象。

## 添加模型 Provider

1. 在 `models/providers/` 使用官方 ChatModel 集成；
2. 供应商配置不进入 domain；核心图依赖官方 BaseChatModel；
3. 在 bootstrap 组装模型，不手写供应商响应解析或 Agent 循环；
4. 增加兼容 HTTP 边界及真实配置验收，明确模型能力限制；
5. 更新 `.env.example`、doctor、教学文档及模块状态。

## 添加工具

1. 优先继承 `PydanticTool`，用 `ToolArguments` 声明严格参数模型；
2. 提供稳定、唯一的名称和说明；JSON Schema 从参数模型生成；
3. 声明最小必要 `PermissionLevel`；
4. 限制路径、网络目标、执行时间和输出体积；
5. 注册到 `default_tools()` 或单独的工具集合；
6. 测试正常调用、非法参数、权限拒绝和安全边界。

工具不得自行绕过审批，也不得把密钥写入返回内容。

## 添加 Repository

1. 先在对应核心模块定义 Protocol；
2. 在 `storage/` 中实现；
3. SQL 变更使用新的递增 migration，禁止修改已发布 migration 的语义；
4. 使用领域对象作为输入输出，不向核心层返回 `sqlite3.Row`；
5. 增加至少一个保存/恢复往返测试；
6. 验证 migration 文件被包含进 Wheel。

## 扩展 Memory

Memory 用于可复用的用户和任务经验，不用于保存外部文档全文。新增能力时需要保持：

- `scope` 隔离；
- 来源可追溯；
- 支持删除和过期；
- 默认过滤秘密；
- 检索结果经 Context Builder 才能进入模型上下文。

## 扩展 RAG

建议按以下顺序实现：

```text
授权读取 → Parser / 来源位置 → Chunker → 上下文增强 → 原子索引发布 → 混合 Retriever → Context / Citation
```

当前解析使用 markdown-it-py，分块使用官方 RecursiveCharacterTextSplitter；
检索组合 BM25Plus、InMemoryVectorStore、EnsembleRetriever 和 ContextualCompressionRetriever。
新增格式或检索策略优先接入现成组件，项目扩展仅处理来源、上下文、预算及权限，
详见 [RAG 使用与验收](rag.md)。
每个 Chunk 必须保留文档、位置和版本信息，确保答案能够
回到原始证据。

## 框架重构约定

本项目 0.2.0 不保留旧 `Agent`、`ModelBackend` 和 contracts 兼容导出。
官方 Agent、工具与中间件使用方式见 [框架教学](framework-tutorial.md)。
代码注释和 docstring 使用简短中文，说明用途，不加入示例；保留必要的安全约束。
测试应验证行为和边界，避免重复实现框架。
