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
Connector → Parser → Normalizer → Chunker → Repository → Retriever → Citation
```

先实现可评估的关键词基线，再接 Embedding。每个 Chunk 必须保留文档、位置和版本信息，确保答案能够
回到原始证据。

## 框架重构约定

本项目 0.2.0 不保留旧 `Agent`、`ModelBackend` 和 contracts 兼容导出。
官方 Agent、工具与中间件使用方式见 [框架教学](framework-tutorial.md)。
代码注释和 docstring 使用简短中文，说明用途，不加入示例；保留必要的安全约束。
测试应验证行为和边界，避免重复实现框架。
