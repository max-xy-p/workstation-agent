# 知识库与检索

RAG 管理外部 Markdown 和纯文本文档，返回可核对的原文与引用。默认结构前缀加 BM25，无需模型密钥；
先按 [数据库部署](database.md) 配置 PostgreSQL，来源文件须位于工作区内。

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

已有知识库可由 Agent 按需查询：

```powershell
uv run workstation-agent run "查文档，说明工具由谁检查权限" --transport local
```

每次任务默认最多 6 次检索，相同参数的重复查询在执行前停止；多次无进展查询由次数上限兜底。
答案质量和是否正确说明证据不足仍取决于实际模型。

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
