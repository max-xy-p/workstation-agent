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

## 类型和调用关系必须清楚

这是适用于整个项目的强制要求，完整规则见
[开发约定：类型与调用关系规范](../AGENTS.md#类型与调用关系规范强制)。

- 参数、返回值和跨模块依赖必须有明确类型，优先使用框架公开类型或现有 Protocol。
- 普通业务代码直接调用明确的方法；工具注册等需要动态查找的部分集中在接入边界，并校验输入输出。
- 授权、日志和重试包装器用泛型保留回调的返回类型，避免包装后丢失类型信息。
- 不用 Any、无类型参数转发或大范围忽略错误来掩盖接口问题。
- 审核时既要检查运行结果，也要确认维护者能找到接口声明和具体实现。

仓库使用 Pyright，配置位于 `pyproject.toml`，依赖通过 `uv sync` 安装：

```powershell
uv run pyright
```

独立 Agent HTTP 服务的路由、任务用例、访问检查和直接调用方已加入类型检查清单。
服务结构与框架选择见 [Agent HTTP 服务](agent-service.md)，本模块回归可运行：

```powershell
uv run python -m unittest tests.integration.test_agent_http -v
```

当前检查检索与分块协议、默认策略、分块结果校验、记忆服务、RAG 用例、证据组装、工具协议、
应用组装入口、直接工具调用方及新增策略测试。具体文件以配置中的 include 清单为准。
旧模块尚未全部纳入静态检查；后续新增或重构模块时，必须将该模块和直接调用方加入清单，
不得通过扩大忽略范围来消除错误。Ruff 用于代码风格和部分错误检查，不能代替静态类型检查。
检索与分块的扩展方式分别见 [检索策略](retrieval.md) 和 [分块策略](chunking.md)。

## 注释与文档必须通俗易懂

这是一项强制开发要求，具体规则和改写示例见
[开发约定：注释与 Markdown 写作规范](../AGENTS.md#注释与-markdown-写作规范强制)。

- 首次提到文件、类或配置时，说明它是什么、用来做什么。
- 说明具体的执行条件、操作和结果，不让读者从术语或源码中猜测。
- 注释解释代码的职责、原因和限制；文档按读者的操作或理解顺序组织。
- 必要时提供简短示例，并说明命令的前提、占位符和成功结果。
- 提交前通读检查：未参与实现的开发者能否在不看聊天记录的情况下理解说明。

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
代码注释、docstring 和 Markdown 遵守上述强制写作规范。简短不能以省略必要说明为代价，
复杂流程可用少量示例解释，并保留安全约束。
测试应验证行为和边界，避免重复实现框架。


## 修改上下文管理

`context/` 保存请求数据、预算、选择和状态栏规则；`agent/context.py` 是官方中间件适配入口。
修改保留规则时，直接捕获最终模型请求，检查当前要求、工具配对和最大输出参数。
`uv run pyright` 使用 pyproject.toml 中的统一配置，新增模块及直接调用方应加入检查范围。
上下文设置和复现命令见 [上下文管理](context.md)，摘要及临时选择不得变成长期记忆写入。

## 修改运行控制和会话存储

审批恢复使用官方检查点协议，说明见 [运行控制](run-control.md)。新增或修改工具时，
测试拒绝不执行、批准只覆盖指定参数、恢复不重放已成功节点，以及取消后不能开始新副作用。
长循环调用 `tools.execution.check_execution`，第三方网络调用需要自身的超时。
会话包装器保留官方 get_tuple、put 等明确方法及返回类型，不改成字符串分发。
`SessionSerializer` 的 Any 仅用于框架通用序列化边界；进入应用后将状态校验为 TaskBoard、RunControlState 或审批模型。

## 处理静态分析建议

先读取具体报告和规则，确认提示指向当前代码，再修改实现。函数复杂时按实际职责拆分，
保持授权、事务、预算和失败路径；不要为降低指标引入无用包装器或字符串分发。
异常测试的准备步骤放在 assertRaises 外，避免由构造失败误判业务调用符合预期。
保留框架要求的协议参数；遇到误报或部署约束，记录原因和回归证据，不直接关闭规则。
本次处理记录见 [Sonar 修复说明](sonar-review.md)。

需要向 Sonar 提供覆盖率时，可用 Coverage.py 统计测试实际执行了哪些代码。
它只在检查时通过 uv 临时加载，不加入 Agent 的运行依赖；采集会增加测试耗时。
以下命令把报告保存到系统临时目录，避免把本机报告提交到仓库：

```powershell
uv run --with coverage python -m coverage run --data-file "$env:TEMP/wa-sonar-coverage.data" --source src/workstation_agent -m unittest discover -s tests -v
uv run --with coverage python -m coverage xml --data-file "$env:TEMP/wa-sonar-coverage.data" -o "$env:TEMP/wa-sonar-report.xml"
```

测试应显示 `OK`，第二条命令应提示 XML 报告已写入。
复扫时将该 XML 文件的绝对路径传给 `sonar.python.coverage.reportPaths`。
这组命令统计当前测试进程；测试另行启动的 CLI 或服务进程不会自动计入，所以结果不包含这些子进程的执行记录。
