# 知识库与检索

RAG 管理外部 Markdown 和纯文本文档，返回可核对的原文与引用。默认结构前缀加 BM25，无需模型密钥；
先按 [数据库部署](database.md) 配置 PostgreSQL；本地来源文件须位于工作区内，外部正文可通过下文的摄取接口提交。

## 摄取与查询

```powershell
uv run workstation-agent rag ingest docs/agent.md --approve-write
uv run workstation-agent rag search "ToolExecutor 权限检查"
uv run workstation-agent rag status
uv run workstation-agent doctor --rag
uv run workstation-agent rag delete docs/agent.md --approve-write
```

摄取返回 `indexed`；文件、处理配置和 metadata 未变时返回 `unchanged`。
修改文件后再次摄取；删除仅清除索引，保留原文件。
`--approve-write` 只授权本次 RAG 写入，默认 MCP/Agent 不自动批准摄取或删除。

本地文件摄取先读取原文，比较已有索引，再展示新增或更新计划。审批只保存路径、内容校验值、字节数和索引版本等摘要。
审批恢复时重新读取文件并核对计划。内容、处理配置或索引版本变化时，需要重新发起摄取。
从仅按路径审批的旧版本升级后，升级前等待审批的文件摄取需要重新发起。
同一份文件和配置已经发布时，直接返回 `unchanged`。分块和模型处理只在确认写入后运行。
索引发布事务会再次核对目标版本，避免准备完成后被其他任务更新或删除的索引被旧计划覆盖。

已有知识库可由 Agent 按需查询：

```powershell
uv run workstation-agent run "查文档，说明工具由谁检查权限" --transport local
```

每次任务默认最多 6 次检索，相同参数的重复查询在执行前停止；多次无进展查询由次数上限兜底。
答案质量和是否正确说明证据不足仍取决于实际模型。

## 工作台笔记摄取

启用工作台来源后，Agent 可调用两个工具完成连接：`inspect_workstation_note` 只读查看标题、版本和正文大小，
`ingest_workstation_note` 经审批后自行读取原文并写入知识库。原文不由模型填写，也不先截断成聊天工具输出再摄取。
笔记的查找、正文管理和访问权限仍由工作台业务与 MCP 工具负责，Agent 核心不实现这些规则。

在 `backend/` 安装可选 SDK 并配置来源：

```powershell
uv sync --extra workstation
$env:WORKSTATION_AGENT_MCP_SERVERS = '[{"name":"workstation","url":"http://127.0.0.1:8000/mcp","tools":{"get_note":"read"}}]'
$env:WORKSTATION_AGENT_WORKSTATION_NOTES = '{"server":"workstation","workspace_id":11}'
uv run workstation-agent doctor
```

MCP 连接和认证配置见 [外部 MCP 接入](agent.md#接入外部-mcp-服务)。SDK 的本地路径及部署方式见
[开发指南](development.md#工作台-sdk-与笔记适配)。默认不启用此来源，不要求基础运行环境安装工作台 SDK。
生产安装可使用 `uv sync --no-dev --extra workstation`；开发依赖包含同版 SDK，用于验证生成模型。

需要 SDK 5.1.0 的笔记详情结构，以及参数明确包含 `workspace_id`、`note_id` 的 `get_note` MCP 工具。
旧的单参数 MCP 工具或缺少版本的响应会被拒绝，不自动猜测工作区、填充版本或适配旧字段。
`doctor` 只检查绑定和 SDK 安装状态；启动 Agent 时才连接服务并核对工具 schema。

在会话中发送“将工作台笔记 42 加入知识库”，Agent 应先调用 `inspect_workstation_note(note_id=42)` 取得版本，
再调用 `ingest_workstation_note(note_id=42, expected_version=7)`，其中 `7` 替换为实际读取到的版本。
审批只接受笔记 ID 和该版本，不接受正文、MCP 服务名或工作区参数；模型不能更改可信来源绑定。
审批指纹还绑定当前来源配置和工具契约；重启后更换服务、工作区或契约时，旧审批不能作用于新的来源。
拒绝摄取不会发出本次摄取的读取请求，也不会写入索引；显式的只读查看操作不受影响。

批准后仍会重新读取笔记。身份不符、版本已变化、结果不符合 SDK、包含已知凭据或正文超过 `RAG_MAX_FILE_BYTES` 时停止，
不修改已有索引。审批期间发生修改时，重新查看来源并对新版本审批。可选 embedding 或上下文增强仍需独立的模型出站授权。

来源固定为 `workstation://服务名/workspaces/工作区ID/notes/笔记ID`，保存笔记版本号、更新时间及必要的项目/文件夹标签。
重新导入同一来源即刷新，内容和元数据未变化时返回 `unchanged`；标题变化不创建重复文档。
处理成功后才替换整篇索引，源文件和工作台笔记不会被修改。导入的是读取时的已批准版本，不保证源笔记在索引期间没有再次变化。
连接名称应稳定标识同一工作台实例，切换到不同实例时使用新名称。

当前只支持指定笔记的显式摄取与刷新，不自动同步远端删除、权限或版本变化；并发发布规则沿用下文的通用正文摄取说明。

## 外部正文摄取

外部来源不必先导出为本地文件。已启动并认证的 Agent HTTP 服务提供 `POST /knowledge/documents`，
接收完整正文并通过 Application、内部授权工具和现有 RAG 管线保存索引；不调用聊天模型重新生成正文。

```json
{
  "source_uri": "workstation://main/workspaces/1/notes/42",
  "title": "数据库设计",
  "text": "# 数据库\n项目使用 PostgreSQL。",
  "media_type": "text/markdown",
  "source_version": "7",
  "metadata": {"project": "demo"}
}
```

调用使用 [HTTP 文档](agent-service.md#启动与认证) 中的 Bearer 认证和 `Content-Type: application/json`。
该接口表示用户明确提交正文并授权写入索引，不证明调用方填写的来源真实；正文中的已知凭据会被拒绝。
它不会抓取 `source_uri`，也不读取本地路径。来源必须为不含凭据、查询串或片段的外部 URI，
同一文档应始终使用稳定标识，标题或文件夹变化不应改变来源身份。

`media_type` 只允许 `text/markdown` 与 `text/plain`，不再通过来源 URI 的文件后缀推断格式。
`source_version` 可选，保存到来源元数据，不能用标签覆盖；其他标签只用于检索过滤。
API 请求仍受 HTTP 服务的 64 KiB 正文上限约束，应用内部摄取还检查 `RAG_MAX_FILE_BYTES`。

返回 `indexed` 或 `unchanged`、稳定文档 ID 和块数。同一来源的正文、标题、格式、版本、标签或处理配置变化后会重新构建，
整篇处理成功后才替换原索引，失败保留旧版本。来源、chunks 和可选向量继续保存在现有 PostgreSQL 文档快照中，
不新增笔记表或另一套 chunk 存储。此内部正文工具不暴露给模型，避免模型自行编造正文再当作原文证据摄取。

工作台适配器会校验完整 MCP 结果、复用 SDK 模型并构造这个输入，RAG 不依赖工作台 SDK。
原始内容及其权限仍由来源系统管理；索引不会因远端删除或权限变化自动失效。
外部来源版本作为标记保存。摄取会检查构建期间目标索引是否被修改，但不会比较外部版本号的先后。
工作台自动同步接入时，仍需依据工作台的版本规则处理已过期的请求。

## 回答来源与原笔记链接

Agent 回答旁会展示本次实际检索到的来源。模型在结论旁保留检索结果中的引用链接后，点击即可查看
保存的原文片段、行号、版本和内容校验值。模型未使用链接时，仍可从回答下方的“本次检索来源”打开片段；
来源列表表示本次检索取得的资料，不证明回答中的每句话都得到资料支持。

需要跳转工作台时，为已有来源绑定补充工作台前端地址：

```powershell
$env:WORKSTATION_AGENT_WORKSTATION_NOTES = '{"server":"workstation","workspace_id":11,"web_url":"http://127.0.0.1:5174"}'
uv run workstation-agent doctor
```

把 `web_url` 换成实际工作台页面地址；它与 MCP 地址不同。仅允许不带凭据、查询参数和片段的 HTTP/HTTPS 地址。
链接使用工作台现有 `/w/{workspaceId}/notes/{noteId}/edit` 路由，不传递令牌；工作台自行处理登录和笔记权限。
不配置地址或来源不属于当前绑定工作区时，只展示本地片段，不猜测跳转地址。

原文片段随答复消息保存，刷新页面、刷新知识库或删除运行记录后仍可核对；工作台链接打开的是当前版本。
不自动检查远端是否已更新或删除。删除消息及后续消息时，会一并删除相应答复的来源片段。
来源只从完整成功的 `search_documents` 工具结果提取，不接受模型生成的来源列表；被截断或失败的结果不补造引用。
每次运行最多保留 32 个片段，每个片段的序列化结果最多 32 KiB，超出部分不展示；未保存的引用不能打开详情。
普通 Markdown 网址仍是模型输出的链接，不具有本地检索来源的验证含义。

## 可选混合检索

服务需支持 OpenAI 兼容 Embeddings 和 Chat Completions，前缀/重排模型还需支持工具形式的结构化输出。
把模型名替换为服务实际提供的名称：

```powershell
$env:WORKSTATION_AGENT_BASE_URL = 'https://你的模型服务/v1'
$env:WORKSTATION_AGENT_API_KEY = '你的密钥'
$env:WORKSTATION_AGENT_RAG_EMBEDDING_MODEL = '你的 embedding 模型'
$env:WORKSTATION_AGENT_RAG_CONTEXT_MODEL = '你的上下文与重排模型'
$env:WORKSTATION_AGENT_RAG_ALLOW_REMOTE = 'true'
uv run workstation-agent rag ingest docs/agent.md --approve-write
uv run workstation-agent rag search "哪个组件阻止未经批准的写入"
```

`ALLOW_REMOTE` 授权文档块、预算内章节背景及查询发送至配置端点，本机模型也需此授权。
MCP 使用服务进程环境，修改后需重启。仅设置 embedding 时启用关键词与向量融合；
设置 context 模型时生成前缀并重排；未设置 embedding 时模式明确返回 `bm25`。
模型失败会返回错误，不静默切换管线；摄取失败保留旧索引。

## 分块、检索与引用

Markdown 按标题、代码和表格结构处理，再用官方递归分块器切分；纯文本直接递归切分。
原文不规范化换行，引用使用 Python 字符半开区间和从 1 开始的行号。
小代码块和表格完整保留，超长内容按行/字符拆分；索引辅助文本不伪装成原文。
结构前缀提供文档和章节背景，模型前缀至多 512 字符，引用始终回到原始正文。

查询先过滤文档，再进行 BM25 / 向量召回、排名融合、可选重排、同章节扩展和证据预算选择。
默认每路召回 20 个、最多重排 30 个、选择 6 个；排名融合常数 60、两路同权。
中文使用单字与双字词，技术标识符保留整体及子项。排序分数不是答案置信概率，也不能跨模式比较。

metadata 精确匹配；`source_uri`、`document_id`、`media_type` 为保留过滤字段。
扩展仅限本次已过滤的同版本文档，默认同章节前后各一个块，可改成整个父章节。
先给命中块分配预算，再扩展；连续或重叠区间合并，引用包含相对路径、行号及 checksum。
标签过滤不构成多用户认证。

ToolResult 的 `content` 为 JSON，包含 `status`、`mode`、`evidence`、`context`、`used_chunk_ids` 和 `warnings`。
`status` 为 `ok`、`no_evidence` 或 `budget_exceeded`；预算省略证据时提示 `evidence_omitted_by_budget`。
外层 `success=false` 表示执行错误。工具全局输出上限可能截断 JSON，带截断标记的内容不能当成完整证据。

## 配置与预算

以下名称统一加 `WORKSTATION_AGENT_RAG_` 前缀：

| 后缀 | 默认值 | 用途 |
| --- | --- | --- |
| CHUNK_SIZE | 1000 | 原文块字符上限，至少 32 |
| CHUNK_OVERLAP | 100 | 普通长文本重叠，小于块大小 |
| MAX_FILE_BYTES | 1000000 | 单文档读取字节上限 |
| MAX_CHUNKS | 10000 | 总块数，发布事务内再次校验 |
| CONTEXT_BUDGET | 8000 | 证据预算，包含引用及标记 |
| RECALL_LIMIT | 20 | 每路召回数量 |
| RERANK_LIMIT | 30 | 重排候选数量 |
| SEARCH_LIMIT | 6 | CLI 默认命中数量，允许 1–20 |
| EXPANSION | 1 | 同章节前后各扩展 0–2 个块 |
| EXPAND_PARENT | false | 完整所属章节扩展，仍受预算限制 |
| MAX_SEARCH_CALLS | 6 | 单 Agent 任务最大检索次数 |
| INPUT_BUDGET | 8192 | 单条 embedding / 模型请求输入预算 |
| MAX_MODEL_CALLS | 64 | 单次摄取 / 查询的保守模型调用数量上限 |
| EMBEDDING_MODEL | 空 | 实际 embedding 模型名称 |
| CONTEXT_MODEL | 空 | 实际前缀及重排模型名称 |
| ALLOW_REMOTE | false | 授权文档与查询发送到配置端点 |

证据与模型输入预算以 UTF-8 字节数保守估算，不是服务端精确 Token 量。
前缀、辅助信息和原文超出输入预算时拒绝摄取，不隐式截断原文；
模型调用数在请求前保守检查。请求超时复用 `WORKSTATION_AGENT_MODEL_TIMEOUT`，SDK 不自动重试。

`doctor --rag` 只读检查索引版本、数量和向量完整性，不调用模型。
索引缺失、需要重建或模型授权/配置缺失时退出非零，不代表已验证模型连通性。

## 更新、重建与限制

文件、分块签名、索引文本、模型、端点或输入预算变化后，旧索引可能返回 `index_incompatible`。
用 `rag status` 找到需要重建的来源，再逐个执行摄取，或授权删除旧索引。
旧格式仅识别来源、不解码内容；可指定 `source_uri` 查询已重建来源，其他过滤字段不能证明旧来源不匹配。

每份文档的来源、原文块、前缀与按块 ID 关联的向量作为完整快照发布，更新失败保留原版本。
当前快照格式为 3；数据库表由 PostgreSQL 迁移创建，旧 SQLite 重建方式见 [数据库说明](database.md#迁移与旧数据)。
源文件变化不自动触发摄取。更换排序策略本身不要求重建，扩展接口见 [开发指南](development.md#分块策略)。

默认最多 10000 块，查询对过滤后内容构建 BM25 和内存精确向量索引，不重新生成文档向量。
当前适合小型工作站知识库，不提供 PDF/Word/网页、自动监听或专用近似向量索引。
框架组件及来源校验的必要扩展见 [架构](architecture.md#框架选择与必要扩展)。

固定检索样例只作回归，不能作为生产正确率；真实模型、语料、召回参数、延迟和费用仍需验证。
