# Workstation Agent 开发约定

本文件供参与本仓库开发的 AI Agent 和开发者阅读。开始修改前，请先查看
[`docs/index.md`](docs/index.md) 和与任务相关的模块文档。

## 项目目标

本项目是一个面向个人工作站的单 Agent 基础设施。当前重点是建立可靠的单 Agent 执行链路，
而不是提前引入多 Agent、复杂工作流引擎或某个模型厂商的专属抽象。

## 架构边界

- `domain/` 只包含稳定领域对象，不依赖 SQLite、模型 SDK 或 UI。
- `agent/` 负责编排和状态流转，不直接访问数据库、文件系统或外部网络。
- `context/` 是记忆和 RAG 进入 Prompt 的唯一组装入口。
- `memory/` 保存用户或任务经验；`rag/` 管理外部知识，两者不得混用。
- `models/` 定义统一模型协议，厂商 SDK 只能出现在 `models/providers/`。
- 所有系统副作用必须通过 `tools/`，并经过 `safety/` 权限策略。
- 基础设施实现放在 `storage/`、`observability/` 等外围模块，通过 Protocol 被核心层依赖。
- CLI、HTTP 和桌面接口只调用 `application/` 用例，不复制 Agent 业务逻辑。

依赖方向应大致保持为：

```text
interfaces → application → agent/context → domain
                         ↘ protocols ← infrastructure
```

禁止让 `domain/` 反向依赖其他业务或基础设施包。

## 开发规则

1. 优先扩展现有 Protocol，不在核心代码中判断具体供应商类型。
2. 新增公开类或协议时，添加简短 docstring，说明职责、边界或重要副作用。
3. 代码注释和 docstring 使用中文；注释用于解释“为什么”和安全约束，不逐行复述代码。
4. 新工具必须声明 `PermissionLevel`、参数 schema，并通过 `ToolExecutor` 执行。
5. 任何文件路径必须规范化并限制在配置的工作区内。
6. 新的持久化结构必须通过版本化 migration 引入。
7. 不记录 API Key、访问令牌、原始秘密或未脱敏的敏感工具输出。
8. 项目已明确采用 LangChain 与 LangGraph 重构，不保留旧 Agent 循环和兼容导出；Agent 标准循环优先使用官方实现。
9. 通用能力优先复用已采用框架的官方 API 和成熟组件，包括文档解析、分块、索引与检索；不要为避免依赖或满足假设需求而重复手写框架已有能力。
10. 实现顺序为框架配置与组件组合、官方扩展接口、最小必要的自定义逻辑；仅在具体业务规则或已验证的能力缺口需要时扩展，继续遵守 Protocol、模块和权限边界。
11. 自定义实现前先查阅框架文档与现有 API，并在改动说明或模块文档中说明能力缺口、配置或扩展为何不足、自定义范围及维护成本。简单辅助逻辑优先使用标准库，但不得以此替代成熟框架能力；引入依赖前说明用途、替代方案和运行成本。
12. 重构以目标架构和长期维护为准，必要时直接进行破坏性调整，并同步修改调用方、测试和文档；未明确要求兼容时，不保留旧对象、旧入口、兼容导出或重复运行链。持久化格式变化仍通过版本化 migration 引入，并明确旧数据重建方式。
13. 一个提交或改动应聚焦一个功能模块，并同步更新对应文档状态。

## 测试要求

- 领域规则、排序、权限和纯逻辑放在 `tests/unit/`。
- SQLite、模型适配器、文件系统等边界放在 `tests/integration/`。
- 完整用户任务以后放在 `tests/scenarios/`。
- 修复缺陷时先增加能复现问题的测试。
- 所有改动至少运行：

```powershell
uv run python -m unittest discover -s tests -v
uv run workstation-agent doctor
```

涉及打包资源、migration 或入口点时额外运行：

```powershell
uv build
```

## 完成定义

一项功能完成时应满足：

- 模块边界清楚，没有绕过 Application、ToolExecutor 或 Repository；
- 正常路径和关键失败路径有测试；
- 错误不会泄露秘密或让 Run 留在不明确状态；
- 新配置有默认值、环境变量说明和 `doctor` 检查；
- `docs/modules.md` 中的状态与真实代码一致；
- README 只保留用户需要的入口信息，细节链接到 `docs/`。
