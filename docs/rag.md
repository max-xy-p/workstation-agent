# RAG：使用、实现与验收

> 2026-10-10。第一阶段已实现。总体设计见 [方案](rag-design.md)，外部模型效果尚需真实配置和文档验收。

## 离线使用

~~~powershell
uv run workstation-agent rag ingest docs/tools.md --approve-write
uv run workstation-agent rag search "ToolExecutor 权限检查"
uv run workstation-agent rag status
uv run workstation-agent doctor --rag
uv run workstation-agent rag delete docs/tools.md --approve-write
~~~

默认结构前缀 + BM25，不需要 API key。索引保存在工作区的 .workstation-agent/rag.db。
DATA_DIR 也必须将 RAG 数据路径指向工作区内。只读查询、工具发现和普通 doctor 不创建数据库。
摄取返回 indexed；内容、处理配置和 metadata 未变时返回 unchanged，复用已保存的派生结果。
更新文件后再次执行 ingest。删除只清除索引及派生数据，保留原文件。

--approve-write 仅授权本次 RAG 摄取或删除，不扩大其他工具权限。
默认 MCP 服务和 Agent 不批准索引写入，先用 CLI 摄取，再通过它们检索。

## 开启完整混合检索

服务需支持 OpenAI 兼容 Embeddings 和 Chat Completions，前缀与重排模型需支持工具形式的结构化输出。
以下模型名称必须替换为实际服务提供的名称：

~~~powershell
$env:WORKSTATION_AGENT_BASE_URL = 'https://你的模型服务/v1'
$env:WORKSTATION_AGENT_API_KEY = '你的密钥'
$env:WORKSTATION_AGENT_RAG_EMBEDDING_MODEL = '你的 embedding 模型'
$env:WORKSTATION_AGENT_RAG_CONTEXT_MODEL = '你的上下文与重排模型'
$env:WORKSTATION_AGENT_RAG_ALLOW_REMOTE = 'true'
uv run workstation-agent rag ingest docs/tools.md --approve-write
uv run workstation-agent doctor --rag
uv run workstation-agent rag search "哪个组件阻止未经批准的写入"
~~~

ALLOW_REMOTE 显式授权文档块、预算内章节背景和查询发送到配置端点，本机模型也经过同一授权。
API key 和端点复用现有配置，不自动选型或下载模型。MCP 使用服务进程环境，修改配置后需重启服务。
仅设置 embedding 模型时启用 BM25 + 向量 + RRF；设置 context 模型时启用生成前缀和重排。
没有 context 模型时使用结构前缀与融合排名，没有 embedding 模型时 mode 明确返回 bm25。

模型失败不静默替换管线：返回失败，摄取保留旧版本，查询不伪造证据。
模型、端点、输入预算或分块配置变化后返回 index_incompatible，需重新摄取匹配文档。

## 分块及来源

DocumentChunker 使用原生框架 Document 作为输出：markdown-it-py 按 CommonMark 解析 Markdown，并启用表格规则，
使用 token.map 的行位置识别顶层 ATX / Setext 标题、代码与表格，映射回原始字符区间。
段落、换行、句末标点及字符递归切分由官方 RecursiveCharacterTextSplitter 完成，
配置 add_start_index=true、strip_whitespace=false、keep_separator=end，验证切片后转换为全文偏移。
纯文本直接交给递归分块器，不解释 Markdown 标题。

小代码和表格独立完整保留；超长时使用无重叠的官方递归分块器，优先按行、再按字符切分。
表头和代码语言保存在 auxiliary_context，用于索引，不伪装成原文位置。
普通文本重叠由框架控制，不超过配置值；邻接关联不跨章节。

原文不做换行规范化，source_start / source_end 为 UTF-8 解码后 Python 字符的半开区间 [start, end)。
line_start / line_end 为一基行号，CRLF 保持不变。
文档 ID 根据相对来源生成，块 ID 根据来源、checksum、分块配置和区间生成，重复摄取可复现。

结构前缀包含文档和完整标题路径。配置 context 模型后，结合块与预算内章节背景生成至多 512 字符的说明。
原文保存在框架 Document.page_content，位置、标题路径、章节和邻接关系、前缀及生成版本保存在 metadata。
检索组合直接接收并返回框架 Document；索引使用增强文本，答案证据按块 ID 回到原始 page_content。
缓存粒度为整文档发布版本，内容、metadata 或处理指纹变化后重建。

## 框架组件与维护边界

| 能力 | 使用组件 | 项目扩展 |
| --- | --- | --- |
| Markdown 结构解析 | markdown-it-py 的 MarkdownIt / token.map | 原文位置映射、章节路径、代码与表格辅助说明 |
| 文本分块 | langchain-text-splitters 的 RecursiveCharacterTextSplitter | 稳定 ID、来源区间、同章节关联及块预算 |
| BM25 | rank-bm25 的 BM25Plus | 通过官方 BaseRetriever 接入中文与技术术语预处理、空证据和稳定同分规则 |
| 向量检索 | langchain-core 的 InMemoryVectorStore | 官方 Embeddings 接口装载 SQLite 中已发布的向量，模型请求经授权工具提供 |
| 排名融合 | langchain-classic 的 EnsembleRetriever | c=60、两路同权、id_key=chunk_id，保留不同来源的相同正文 |
| 重排组合 | ContextualCompressionRetriever / BaseDocumentCompressor | 调用已授权的模型 Protocol，校验调用预算、分数及零分过滤 |
| 模型接入 | ChatOpenAI / OpenAIEmbeddings / with_structured_output | 上下文前缀提示、模型预算与出站权限 |

不再维护手写 Markdown 扫描、递归切分、BM25 公式、余弦计算或 RRF 公式。
项目保留来源追溯、上下文增强、原子发布、过滤、证据预算和权限等业务规则。

处理链统一使用 LangChain Document，不再保留 TextChunk、RetrievalQuery、RetrievedChunk、
自定义 Retriever Protocol 或 FrameworkRetriever 转换入口；Context 接收官方 Runnable 并调用 invoke。
SourceDocument 只记录来源身份，IndexSnapshot 保存来源、框架文档和按块 ID 关联的向量。
IndexRepository 仅提供 state / get / publish / delete；不再保留 upsert_document、all_chunks
或独立关键词仓库，也不保存检索阶段未使用的 counts。旧类型和仓库接口没有兼容导出。

MarkdownHeaderTextSplitter 会处理空白及正文格式，其输出偏移不能直接作为原文引用；
因此选用成熟解析器的行映射，再将未经规范化的切片交给官方递归分块器。
框架缺少本项目要求的原子 SQLite 发布、授权边界和中文技术术语策略，通过已有接口补足这些能力。

新增依赖为 langchain-text-splitters、langchain-classic、markdown-it-py 和 rank-bm25，
版本由 uv.lock 固定。替代方案是继续维护旧算法，或引入外部检索数据库；前者增加算法维护，
后者增加服务部署成本。当前使用内存精确扫描，代价是 NumPy 等传递依赖的安装体积，
以及查询时按过滤后快照重建 BM25 / 内存向量索引的 CPU 与内存开销，不启动新服务。

langchain-community 已由官方停止维护，未作为新运行依赖；
BM25 直接使用其底层算法包，通过官方 BaseRetriever 扩展接口接入。
参见 [官方公告](https://github.com/langchain-ai/langchain-community/issues/674)、
[rank-bm25](https://github.com/dorianbrown/rank_bm25)、
[Markdown 位置映射](https://markdown-it-py.readthedocs.io/en/latest/using.html)。

### 现有索引升级

本次进行破坏性重构，当前持久化格式为 3，migration 003_rag_native_documents.sql
新增 format_version 标记。快照通过 Pydantic 直接序列化和校验框架 Document，不转换旧块对象。

旧格式只识别来源，不解析 payload，不作为候选参与检索。
status / doctor --rag 返回 needs_reindex，并在 obsolete_sources 中列出待处理来源；
查询可能涉及旧来源时返回 index_incompatible。指定 source_uri 可以单独查询已经重建的来源；
旧 metadata 不再解码，因此只使用其他过滤字段不能确定旧来源是否匹配，需先完成重建或删除。

按摄取命令从原文件重新构建每个来源，或经授权删除其索引记录。
格式迁移仅在已授权的写入时执行，不自动发送文档、删除其他来源或转译旧 payload；
成功发布后替换对应记录，构建或发布失败保留原记录。原文件不受索引格式变化影响。

## 单次检索与上下文

~~~text
查询及过滤
  → 增强文本 BM25 / 精确余弦向量召回
  → RRF 融合
  → 可配置模型重排
  → 选择命中块
  → 邻接或父章节扩展
  → 合并原文区间、逐块预算检查
  → 证据、行号引用及实际使用的块 ID
~~~

BM25Plus 使用 k1=1.5、b=0.75、delta=1.0，避免小语料中高频词的负 IDF。
中文采用单字与双字滑窗，英文保留技术标识符并增加分隔后的子项。
这是一种无需词典的初始预处理策略，通过检索接口接入，建索引与查询保持一致。
无查询词重叠的候选不会作为词法证据，空词表不构建 BM25。
原文框架文档与按块 ID 关联的向量原子保存在 SQLite，移除重复的词频持久化；
查询从过滤后的增强文本构建 BM25，通过官方 Embeddings 接口装载已有向量，
再由 InMemoryVectorStore 精确检索，
不会重新生成文档向量，查询向量只通过授权模型工具生成。
当前适合小型工作站知识库，默认最多 10000 个块；未引入 ANN 或独立向量数据库。

向量数量、维度、有限性和非零长度在摄取与查询时校验。
每路默认召回 20 个，RRF 常数 60，同分按稳定候选顺序；融合后最多重排 30 个，默认选择 6 个。
重排通过官方 ContextualCompressionRetriever 组合 BaseDocumentCompressor，
模型仍使用官方 ChatModel 的结构化分数输出，按输入预算分批，可通过 Protocol 替换专用模型。
纯 BM25 返回算法分数，混合模式无重排时返回官方融合排序位置的倒数，有重排时返回模型分数。
这些排序分数不作为答案置信概率，也不跨模式比较。

metadata 过滤采用精确匹配。source_uri、document_id、media_type 为保留过滤字段，不被同名 metadata 覆盖。
过滤先限定候选语料；扩展也仅使用这个范围内的同版本文档。
默认补取同章节前后各一个块；EXPAND_PARENT=true 时补取完整所属章节。
先分配全部命中块预算，再尝试扩展，扩展超限不挤掉核心证据。
连续与重叠原文合并，引用包含相对路径、行号和 checksum 前缀，块 ID 只记录实际注入部分。

ToolResult.content 为 JSON，包含 status、mode、evidence、context、used_chunk_ids 和 warnings。
status 为 ok、no_evidence 或 budget_exceeded；内容被预算丢弃时带 evidence_omitted_by_budget。
执行错误在外层返回 success=false 和安全 error_code。
全局 TOOL_OUTPUT_CHARS 仍限制输出，过小值可能截断 JSON；收到截断标记时不能继续当作完整证据。

## 现有单 Agent 与 MCP

默认目录增加 ingest_document、delete_document、search_documents 和 rag_status。
通过 ToolApplication / ToolExecutor / MCP 共享 schema、权限和结果；不建立第二个 Agent 循环。
模型操作属于内部 SENSITIVE 工具，未授权时即使索引写入已批准，也不会访问模型。

~~~powershell
$env:WORKSTATION_AGENT_MODEL = '你的工具调用模型'
uv run workstation-agent run "查文档，说明工具由谁检查权限以及写入有什么要求" --transport local
~~~

单 Agent 根据原文调用后续查询，官方中间件默认限制单任务 6 次检索。
相同参数的重复查询在执行前停止，状态为 LIMIT_REACHED；不同参数的无进展查询由次数上限兜底。
实际推理和缺失证据说明取决于工具调用模型能力；没有新增持久化会话或规划 Agent。

## 环境变量与预算

以下后缀统一加 WORKSTATION_AGENT_RAG_ 前缀，数值必须为合法整数，布尔值为 true / false。

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

预算采用 UTF-8 字节数保守估计，不声称是特定 tokenizer 的精确 token 计数。
前缀 + 辅助信息 + 原文超限时拒绝摄取，不隐式截断原文。
前缀生成与重排预留请求结构开销，章节背景按剩余预算截取。
模型请求超时复用 WORKSTATION_AGENT_MODEL_TIMEOUT，默认 30 秒，SDK 不自动重试。
模型调用数量在请求前保守估计，超限时减小输入范围或显式调整预算。

doctor 展示模式、授权、模型、块预算和路径。
doctor --rag 额外通过 Application 检查索引版本、数量及向量完整性，不调用模型。
missing / needs_reindex 或缺少必要模型授权与 API key 时退出非零。
该命令不代表模型服务的连通性或质量已验证。

## 存储和失败处理

migration 002_rag.sql 建立发布记录表，003_rag_native_documents.sql 引入原生文档格式标记，随包交付。
单条记录保存 SourceDocument、原文 Document、metadata、按块 ID 关联的向量和处理指纹，完整构建后事务替换。
内存仓库也只维护完整快照，读取返回隔离的框架对象，调用方修改 Document 不会污染已发布版本。
并发发布按 SQLite 写事务串行提交，总块预算在事务内强制检查。
查询单条 SELECT 获取快照，失败、删除和更新不暴露索引版本混用的中间状态。

非法路径、格式、UTF-8、参数、预算和索引配置有明确错误码。
模型与 SQLite 意外异常不回显凭据、底层消息或原始敏感内容。
失败更新可修复配置后重试，旧版本保留；源文件变更不自动触发摄取。

## 验收与剩余事项

~~~powershell
uv run python -m unittest discover -s tests -v
uv run workstation-agent doctor
uv build
~~~

测试覆盖 ATX / Setext 标题、重复文本与 CRLF 原文位置、代码表格保护、
中文排序与按块 ID 融合、重启后复用文档向量、向量验证、过滤及扩展、完整证据预算、
SQLite 重启与更新删除、旧格式不解码、授权迁移与逐来源重建、失败回滚和并发预算、
原生 Document 读取隔离、块 ID 与向量映射校验、路径与模型权限、错误脱敏、真实 CLI、
官方 SDK 的 HTTP 夹具，以及单 Agent 两次检索、重复停止和次数限制。

固定评估位于 tests/scenarios/test_rag_evaluation.py：4 份文档、7 个有答案问题，其中 1 个要求跨文档证据，
另有 1 个无答案问题。回归门槛为必要来源 Recall@6=1.0、MRR≥0.8 和原文引用区间准确。
这是小样例回归门槛，不能当作生产正确率；外部 embedding、生成前缀和重排质量仍需真实验收。

后续待定：实际模型选型、生产语料评估、召回与重排参数、延迟及费用门槛、分词器和规模优化。
PDF / Word / 网页、自动监听、专用 ANN 索引及历史对话改写不在本次交付范围。
