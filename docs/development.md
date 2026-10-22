# 开发指南

需要 Python 3.13+、uv 和专用 PostgreSQL 测试数据库。先阅读 [架构](architecture.md) 与 [开发约定](../AGENTS.md)；
编码、类型和文档规范以开发约定为准，本篇只补操作与扩展方法。

## 目录与命令位置

```text
workstation-agent/
├── frontend/                 React 页面、前端测试和 Node 配置
├── backend/
│   ├── src/workstation_agent/ Agent、应用用例、通信接口和基础设施
│   ├── tests/                Python 单元、集成和场景测试
│   ├── scripts/              后端启动脚本
│   ├── examples/             独立客户端示例
│   ├── pyproject.toml        Python 依赖、打包和检查配置
│   └── uv.lock               Python 依赖锁定文件
├── docs/                     共享使用与架构文档
└── compose.yaml              PostgreSQL 本机部署配置
```

Python 命令在 `backend/` 执行，npm 命令在 `frontend/` 执行，Docker Compose 命令在仓库根目录执行。
两端分别维护依赖和构建产物；后端的 Agent 位于 `backend/src/workstation_agent/agent/`，由 Application 调用。
已有根目录 `.venv/` 不再用于后端，整理目录后需在 `backend/` 重新运行 `uv sync`。

## 环境与检查

从仓库根目录开始：

```powershell
cd backend
uv sync
$env:WORKSTATION_AGENT_WORKSPACE = (Resolve-Path ..).Path
# 按数据库文档设置 WORKSTATION_AGENT_DATABASE_URL，测试不要指向重要数据库。
uv run workstation-agent init-db
uv run python -m unittest discover -s tests -v
uv run pyright
uv run workstation-agent doctor
uv run workstation-agent doctor --database
uv build
```

测试使用临时工作区隔离数据，但会在数据库中留下记录；部署见 [数据库说明](database.md)。
离线模型夹具验证协议和编排，外部模型效果需独立验收。
Pyright 的检查范围在 `backend/pyproject.toml`；新增或重构 Python 模块时将其及直接调用方加入清单，不扩大忽略范围。
Ruff 不替代类型检查。涉及 migration 或打包资源时，确认资源已进入 Wheel。

## 前端工作台

`frontend/` 是独立的 React + TypeScript + Vite 工程，后端位于 `backend/src/workstation_agent/`。
需要 Node.js 22.12+；在前端目录运行：

```powershell
npm ci
npm run dev
# 类型检查和生产构建；浏览器测试首次使用时先安装 Chromium。
npm run build
npx playwright install chromium
npm test
```

开发地址为 `http://127.0.0.1:5173`，`npm run preview` 在 4173 预览构建结果。
Vite 将同源 `/api` 请求转发到本机 Agent 的 8766 端口；使用其他端口时，启动前端前设置
`WORKSTATION_AGENT_HTTP_PORT`。代理重写后端 Host/Origin，后端认证和跨域限制保持原规则。
这些服务仅监听本机；正式静态托管还需提供相同的 `/api` 转发配置。

页面点击“连接本机 Agent”后输入 HTTP 令牌，令牌仅保存在内存，刷新后需重新输入。
不在 `VITE_*`、URL、localStorage 或 sessionStorage 中存令牌。没有后端时可显式选择页面演示，
或访问 `/?demo=1`；演示数据不发出 API 请求，刷新后重置，页面始终标明演示模式。

| 目录/文件 | 职责 |
| --- | --- |
| `src/api/` | 与后端契约对应的 Zod 校验、明确的 HTTP 方法及隔离的演示数据 |
| `src/useWorkspace.ts` | 可取消的读取与轮询、分页及消息版本同步，不自动重试写入 |
| `src/components/` | 会话导航、消息、Markdown、运行侧栏与弹窗 |
| `src/App.tsx` | 用户动作与组件组装，修改历史时传入读取到的版本 |
| `tests/` | Chromium 浏览器回归；真实后端联调由专用夹具和环境变量显式启用 |

真实后端联调：在 `backend/` 目录配置专用 `WORKSTATION_AGENT_DATABASE_URL`、至少 32 位的
`WORKSTATION_AGENT_E2E_TOKEN` 和 `WORKSTATION_AGENT_HTTP_PORT=18766`，运行
`uv run python -m tests.serve_frontend_fixture`。前端终端注入相同的测试令牌与端口后运行 `npm test`。
夹具使用临时工作区和离线模型，不需要真实模型密钥；未提供测试令牌时，该项浏览器测试跳过。

React 管理交互，Vite 提供开发代理和构建；Zod 校验网络边界，react-markdown/remark-gfm
展示 Markdown，Lucide 提供图标。未启用原始 HTML，也不自动加载模型返回的远程图片。
无需额外 Node 后端、SSR 或全局状态框架；`package-lock.json` 固定依赖，运行成本是本机开发进程与浏览器资源。
依据：[React 客户端应用](https://react.dev/learn/creating-a-react-app)、[Vite](https://vite.dev/guide/)、
[react-markdown](https://github.com/remarkjs/react-markdown)。

## 添加能力

先尝试现有框架的配置与组件组合，再用官方扩展接口，最后才补最小业务逻辑。
新增依赖或自定义逻辑时，说明能力缺口、替代方案及安装、运行和维护成本。

| 能力 | 主要步骤 |
| --- | --- |
| 模型 | 在 `models/providers/` 使用官方 ChatModel，bootstrap 注入；补兼容 HTTP 边界测试和实际配置说明 |
| 工具 | 继承 `ToolArguments`、`PydanticTool`，声明 schema 和最小 PermissionLevel，注册后只经 Application / ToolExecutor 调用 |
| 存储 | 核心先定义 Protocol，`storage/` 实现；使用参数化 SQL，新增递增 migration，测试往返、隔离、冲突与回滚 |
| API | 在 `api/` 声明通用契约，`application/` 实现用例，接口层只做协议校验和转换 |
| Memory / RAG | 保持两类内容独立，来源可追溯；检索结果经 Context 组装，写入与额外模型请求分别授权 |

工具还需测试非法参数、越界、权限拒绝、输出限制及取消；路径规范化后限制在工作区内。
长循环调用 `tools.execution.check_execution`，第三方网络操作设置自身超时。
新增配置同步补默认值、环境变量、`doctor` 与所属功能文档。

## 分块策略

`rag/chunking/contracts.py` 的 `ChunkingStrategy` 声明 `split(document, text)` 与 `signature`。
默认 `StructuredChunkingStrategy` 组合 MarkdownIt 的位置映射和官方递归分块器，
只接收来源与原文，不读取文件、访问数据库或调用模型。

输出为 `tuple[langchain_core.documents.Document, ...]`。`page_content` 必须等于原文切片，
每块 ID 非空、唯一且可重复生成；`metadata` 约定如下：

| 字段组 | 必须保留的字段 |
| --- | --- |
| 来源 | document_id、source_uri、title、checksum、media_type |
| 原文位置 | source_start、source_end、line_start、line_end |
| 顺序与章节 | ordinal、section_id、heading_path |
| 邻接 | previous_chunk_id、next_chunk_id；章节边界为 None |
| 索引辅助信息 | context_prefix、auxiliary_context、context_provenance |

字符区间为 Python 字符的半开区间 `[start,end)`，行号从 1 开始，不规范化 CRLF。
覆盖全部非空白正文，片段可在同章节内重叠；每块与总块数遵守配置预算。
`validate_chunks` 在模型调用和索引发布前统一检查；无效来源/位置返回 `invalid_chunking_output`，
超预算返回 `resource_limit`，两者都不能覆盖旧索引。

`signature` 是稳定的字符串、数值或布尔值元组，包含策略名称、版本和影响分块的全部参数。
不得使用时间或随机值；默认标记为 `native-document-v3`。
修改签名会改变处理指纹，查询不兼容来源返回 `index_incompatible`，需从原文重新摄取。

```python
from workstation_agent.bootstrap import build_tool_application
from workstation_agent.config import Settings
from workstation_agent.rag.chunking.structured import StructuredChunkingStrategy

settings = Settings.from_env()
tools = build_tool_application(
    settings, chunking=StructuredChunkingStrategy(settings.rag),
)
```

自定义策略实现协议即可，也可注入 `RagApplication` 或 `build_agent_application`。
MCP 模式必须在服务端组装策略，客户端对象不能替换远端实现。没有另设 CLI 策略注册表。
解析器与框架分块器不提供本项目的来源、邻接及失效规则，因此项目只补协议与校验，不重写通用分块算法。

## 检索策略

`retrieval/strategy.py` 的 `RetrievalStrategy[T]` 声明
`retrieve(query, candidates, *, limit) -> tuple[RetrievalHit, ...]`。
记忆候选为 `MemoryRecord`，知识库候选为 `IndexSnapshot`；两者分别使用默认策略，算法组件见 [架构](architecture.md#框架选择与必要扩展)。

服务先过滤范围、有效期和索引兼容性，再把候选交给策略。
策略只返回 ID 和分数；服务拒绝未知/重复 ID、非有限分数和超量结果，并从原候选中读取正文。
策略不能自行扩大数据范围、替换原文或绕过模型出站授权。

注入方式：

- `build_memory_application(settings, retrieval=strategy)` 替换记忆检索。
- `build_tool_application` 与 `build_agent_application` 分别接收 `memory_retrieval`、`knowledge_retrieval`。
- MCP 知识库策略仍由服务进程组装。本机传输才使用当前注入的知识库对象。

替换排序策略本身不改变持久化格式；修改分块、索引文本或 embedding 配置时需重新摄取。
官方 Retriever 接口只接收查询并返回 Document，不能直接表达本项目的“显式传入可信候选、按 ID 取回原文”，
因此保留这一个小型泛型协议。策略属于可信代码，不是第三方插件沙箱。

## 运行与上下文维护

审批使用官方中断和检查点，测试拒绝不执行、批准只覆盖指定参数、恢复不重放成功节点。
会话包装器保留官方 get_tuple、put 等明确方法和返回类型，不改成字符串转发。
`SessionSerializer` 的开放类型只用于框架序列化边界；应用内立即校验成审批、RunControlState 或 TaskBoard。

`context/` 管理预算和状态规则，`agent/context.py` 负责框架适配。
修改后直接捕获最终模型请求，检查当前要求、工具配对、来源与输出参数，不只检查中间结果。
状态栏版本由 `restore_board` 迁移：当前格式 2，格式 1 升级时把旧完成项改为待确认，未知格式拒绝加载。
运行控制记录格式 1 保存 Run ID、绝对截止时间和结束标记；缺少它的旧审批不能恢复。

会话历史修改先删除官方检查点，再提交业务文本，故障时保留可重建的原历史。
须验证编辑后无旧上下文、写入失败回滚、CLI 清除不恢复旧文本，以及旧审批没有业务历史时仍可恢复。

## 处理静态分析建议

以当前源码和具体报告为准，按职责拆分复杂函数，保留授权、事务、预算和失败路径。
不要为降低指标添加无用包装、字符串分发或关闭规则；异常断言仅包含待验证操作。

两项需要人工复核的约束：重复取消时必须等待工具线程收尾后释放锁；服务 HTTP 仅用于
`127.0.0.1` 且校验令牌、Host 和 Origin，不能直接改成远程监听。
历史扫描的逐条数量和分析编号不再作为当前质量状态维护。

需要覆盖率时，可临时加载 Coverage.py；以下报告不包含另行启动的 CLI/服务子进程：

```powershell
uv run --with coverage python -m coverage run --data-file "$env:TEMP/wa-coverage.data" --source src/workstation_agent -m unittest discover -s tests -v
uv run --with coverage python -m coverage xml --data-file "$env:TEMP/wa-coverage.data" -o "$env:TEMP/wa-report.xml"
```

将报告路径交给 Sonar 的 `sonar.python.coverage.reportPaths`。
上传源码或修改远程分析设置需另行获得授权；本地检查不等于已执行远程扫描。
