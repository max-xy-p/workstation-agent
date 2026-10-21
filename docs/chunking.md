# 文档分块策略

分块策略决定一份文档在什么位置切开。应用负责读取已授权的文档、检查策略结果、
生成索引并保存；策略只接收来源信息和原文文本，不自行读文件、访问数据库或调用模型。

当前保留一种正式实现：结构化分块策略。它延续原有 Markdown 与纯文本分块规则，
没有增加新的算法或运行依赖。以后需要其他分块方式时，实现协议并在组装入口注入即可。

## 目录与职责

```text
rag/
├── identifiers.py          # 文档、章节和片段共用的稳定 ID 生成函数
└── chunking/
    ├── __init__.py         # 包说明
    ├── contracts.py        # 分块协议、策略签名类型和已知错误
    ├── structured.py       # 当前结构化分块实现，复用解析器与官方递归分块器
    └── validation.py       # 所有分块策略共用的原文、来源和预算检查
```

| 对象 | 职责 |
| --- | --- |
| `ChunkingStrategy` | 定义 `split(document, text)` 方法和只读 `signature` 属性 |
| `StructuredChunkingStrategy` | 保留章节、代码和表格结构，再调用官方递归分块器处理长内容 |
| `validate_chunks` | 在模型调用和索引发布前检查策略返回的全部片段 |
| `RagApplication` | 依赖分块协议，协调校验、上下文增强、向量生成和索引发布 |
| `bootstrap.py` | 创建默认策略，或者接收调用方传入的策略 |

原来的 `DocumentChunker` 已替换为 `StructuredChunkingStrategy`，没有兼容别名或旧分块入口。
解析器目前仍是默认策略内部的组件；增加新格式时再根据实际输入扩展解析能力。

## 调用流程

```text
ToolExecutor 检查摄取权限
→ 工具读取工作区内的文档
→ RagApplication 调用注入的分块策略
→ validate_chunks 检查结果
→ 可选的上下文增强与向量生成
→ Repository 原子发布完整索引
```

校验失败时不会发起后续模型请求，也不会覆盖已发布的索引。
分块数量或长度超出预算时返回 `resource_limit`；来源、原文位置或关联关系无效时返回
`invalid_chunking_output`。错误消息不回显策略返回的原文或底层异常。

## 输入与输出约定

输入是 `SourceDocument` 来源对象和未经换行规范化的文本。
输出是 `tuple[Document, ...]`，其中 Document 使用 LangChain 的官方类型。
每条 `page_content` 必须等于原文对应区间，不能把生成的摘要或说明放进原文正文。

metadata 需要保留以下字段，允许额外的辅助字段：

| 字段组 | 字段 | 用途 |
| --- | --- | --- |
| 来源 | document_id、source_uri、title、checksum、media_type | 必须与输入来源一致 |
| 原文位置 | source_start、source_end、line_start、line_end | 字符区间和引用行号 |
| 顺序与章节 | ordinal、section_id、heading_path | 按原文顺序排列，属于同一章节的片段连续出现 |
| 邻接关系 | previous_chunk_id、next_chunk_id | 指向同章节紧邻片段，章节边界使用 None |
| 索引辅助信息 | context_prefix、auxiliary_context、context_provenance | 保存结构背景和生成版本，不作为原文引用 |

每条 Document 必须有非空、唯一且可重复生成的 ID。
字符位置采用 Python 字符的半开区间 `[start, end)`，行号从 1 开始，沿用现有换行计数规则。
片段可以重叠，但不能跨章节重叠；所有非空白正文都必须被覆盖。
每条正文不超过配置的 `chunk_size`，总数不超过 `max_chunks`，全库数量仍由应用和仓库另行检查。

这些是分块结果必须遵守的约束，不属于可以随策略替换的可选行为。
策略实现是受信任的程序代码，接口不提供运行不可信插件的隔离环境。

## 策略签名与索引重建

`signature` 是由字符串、整数、浮点数或布尔值组成的稳定元组。
它需要包含能够区分策略的名称、实现版本，以及所有会改变分块结果的配置。
名称和版本可以合并为一个稳定标记，默认策略沿用原有的 `native-document-v3` 标记。
不要使用随机值、进程地址或当前时间；新增策略或修改规则时必须调整对应标记或参数。

`RagApplication` 将该元组与索引文本和模型配置一起计算处理指纹。
签名改变后，即使原文件没有变化，索引也会被标记为需要重新摄取：

```powershell
uv run workstation-agent rag status
uv run workstation-agent rag ingest docs/tools.md --approve-write
```

第二条命令中的路径应替换为需要重建的来源文件；每个不兼容来源需要重新摄取。
重建前查询不兼容来源返回 `index_incompatible`，不会混用不同规则的索引。
重建失败保留原来的完整索引，但只有匹配原策略配置的入口能继续查询它。

本次默认策略的规则、参数顺序、块 ID 和处理指纹保持不变，已有默认索引无需重建。
数据库表和持久化格式未变化，因此不需要新的数据库迁移脚本。

## 注入方式

默认入口已组装结构化策略。以下代码展示显式指定相同实现的方法：

```python
from workstation_agent.application.tools import ToolApplication
from workstation_agent.bootstrap import build_tool_application
from workstation_agent.config import Settings
from workstation_agent.rag.chunking.structured import StructuredChunkingStrategy


def build_tools(settings: Settings) -> ToolApplication:
    """使用同一份分块配置组装策略与工具入口。"""
    return build_tool_application(
        settings,
        chunking=StructuredChunkingStrategy(settings.rag),
    )
```

自定义实现遵守 `ChunkingStrategy` 即可，不要求继承默认策略。
也可以通过 `RagApplication(..., chunking=strategy)` 或
`build_agent_application(..., chunking=strategy)` 注入。
Agent 使用 local 工具传输时采用本机注入的策略；使用 MCP 时，摄取和查询由服务进程执行，
策略应在服务进程的 `build_tool_application` 中组装，客户端对象不能替换远端实现。

目前只有一个正式实现，没有新增 CLI 策略选择参数、空实现或动态注册表。
后续增加正式策略时，在本目录实现并补充签名、原文位置和预算检查的测试。

## 框架复用与自定义范围

默认策略继续使用 [MarkdownIt 的位置映射](https://markdown-it-py.readthedocs.io/en/latest/using.html)
和 [RecursiveCharacterTextSplitter](https://docs.langchain.com/oss/python/integrations/splitters/recursive_text_splitter)。
分块算法由成熟组件执行，项目只维护来源映射、稳定 ID、章节关联和业务约束。
框架分块器自身不定义本项目的来源字段、邻接关系和索引失效规则，
因此通过小型 Protocol 与统一校验补足这些边界，不另行实现通用分块框架。
维护成本集中在约定字段和验证测试；框架升级时需要继续验证原文偏移、代码表格和 CRLF 行号。

## 验证

```powershell
uv run pyright
uv run python -m unittest discover -s tests -v
uv run workstation-agent doctor
uv build
```

测试覆盖默认行为、替换策略、版本变化后的重新摄取、默认索引复用、来源与位置校验、
代码与表格保护、无效结果不调用模型、不覆盖旧索引，以及通过真实工具入口和 PostgreSQL 重启后的使用。
