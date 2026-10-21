# 记忆与知识库的检索策略

记忆保存完整笔记，知识库保存带来源位置的文档片段。两者的数据单位、分词和结果处理不同，
因此分别使用检索策略；共用的只是底层算法组件和调用协议。

## 当前分工

| 对象 | 职责 |
| --- | --- |
| `RetrievalStrategy[T]` | 定义统一的 `retrieve(query, candidates, limit=...)` 方法；T 表示该策略接收的候选类型 |
| `MemoryRetrievalStrategy` | 接收完整 `MemoryRecord` 笔记，按记忆分词规则进行 BM25Plus 排序 |
| `KnowledgeRetrievalStrategy` | 接收已过滤的 `IndexSnapshot` 文档快照，组合关键词、向量召回、排名融合和可选重排 |
| `RetrievalHit` | 只返回候选 ID 和分数；服务根据 ID 读取本次候选中的原文 |
| `MemoryService` / `RagApplication` | 获取并过滤候选、调用注入的策略、检查结果，再交给 Context 组装 |
| `bootstrap.py` | 组装默认策略，或接收调用方提供的其他策略 |

`retrieval/strategy.py` 定义协议和结果检查，`memory/retrieval.py` 实现记忆策略，
`rag/retriever.py` 实现知识库策略。业务调用使用带类型的方法，不使用字符串查找策略方法。

## 两种默认策略

记忆以整条笔记为检索单位。英文按单词或标识符处理，中文按相邻双字切分；
没有共同词项的笔记不参与排序。默认策略不调用 embedding 或重排模型，
也尚未按更新时间、类别或重要性额外加权。后续若需要这些规则，可单独替换记忆策略。

知识库按文档块召回。中文保留单字和双字词，技术标识符保留整体和组成部分；
索引文本可以包含已有的上下文前缀。配置 embedding 后使用关键词与向量混合召回，
由官方 `EnsembleRetriever` 合并排名，并按块 ID 去重；配置重排模型后，
由官方 `ContextualCompressionRetriever` 和压缩器接口执行重排。
服务随后按原有设置扩展同章节片段，Context 根据预算合并原文并生成引用。

两种策略继续复用 `rank_bm25` 和 LangChain 组件，没有新增手写排序算法。
PostgreSQL 仅负责持久化，不决定采用哪种检索策略。

## 必须保持的边界

1. `MemoryService` 在调用策略前检查当前用户、工作区、项目和有效期。
   `RagApplication` 在调用策略前应用文档过滤条件和索引兼容性检查。
   RAG 标签过滤是查询条件，本身不构成多用户权限系统。
2. 策略只接收本次允许检索的候选，不持有数据库或文件系统。
   知识库策略需要模型能力时，生产入口注入经过 ToolExecutor 授权的后端。
3. 服务拒绝策略返回的未知 ID、重复 ID 和非有限分数，并再次限制结果数量。
   返回的记忆正文、文档原文和引用位置都取自本次候选，策略不能替换原文内容。
4. 策略实现是受信任的程序代码，不是可执行不可信插件的沙箱。
   新策略必须继续遵守工具、权限、模型调用预算和数据范围约定。
5. 切换检索策略不改变已保存的索引格式，不要求重建现有索引。
   改变分块、索引文本或 embedding 配置时，仍按现有规则重新摄取文档。

## 替换策略

自定义策略实现相同的方法即可，不需要继承项目基类。候选类型应明确：
记忆策略使用 `RetrievalStrategy[MemoryRecord]`，知识库策略使用
`RetrievalStrategy[IndexSnapshot]`。Pyright 会检查参数和返回值是否匹配。

```python
from collections.abc import Sequence

from workstation_agent.memory.models import MemoryRecord
from workstation_agent.retrieval.strategy import RetrievalHit


class RecentMemoryStrategy:
    """按更新时间选取笔记；示例只展示如何替换策略，不包含相关性检索。"""

    def retrieve(
        self, query: str, candidates: Sequence[MemoryRecord], *, limit: int
    ) -> tuple[RetrievalHit, ...]:
        ordered = sorted(candidates, key=lambda note: note.updated_at, reverse=True)
        return tuple(RetrievalHit(note.id, 1.0) for note in ordered[:limit])
```

将实例传给 `build_memory_application(settings, retrieval=RecentMemoryStrategy())`，
即可替换该记忆入口的检索方式。默认写入权限不会因此改变。
`build_tool_application` 分别接收 `memory_retrieval` 和 `knowledge_retrieval`；
`build_agent_application` 也接收这两个参数。本机工具传输使用注入的知识库策略，
MCP 传输的知识库策略由服务进程组装，客户端不能用本机对象替换远端实现。

当前没有新增环境变量或 CLI 策略名称注册表。只有默认实现时，通过带类型的参数注入即可；
有多个正式策略需要用户选择时，再添加相应配置与诊断。

## 为什么增加项目协议

[LangChain Retriever 官方接口](https://docs.langchain.com/oss/python/integrations/retrievers)
以查询字符串为输入、以 Document 列表为输出，适合组合底层检索组件。
本项目还需要把每次已过滤的候选显式交给策略，并保证服务从候选中取回原文。
单纯调整框架参数无法表达这两项业务约束，因此增加一个小型泛型协议和 ID 检查。
自定义范围限于输入输出约定、范围检查和组件组合；算法仍使用成熟组件。
维护时需保持协议类型、默认策略和候选检查测试同步，不维护另一套通用检索框架。

Pyright 是新增的开发依赖，用于检查上述接口和调用链；可替代工具是 mypy。
本项目统一选择 Pyright，避免维护两套检查配置。它仅在开发和检查时运行，
需要 Node.js 执行环境，Python 包可通过 nodeenv 准备环境，不增加 Agent 运行时数据库或模型请求。

## 验证

```powershell
uv run pyright
uv run python -m unittest discover -s tests -v
uv run workstation-agent doctor
```

策略测试覆盖独立注入、范围与过期过滤、无效 ID 和分数、结果数量上限、
原文引用、索引复用，以及经过真实工具入口的权限检查。
现有知识库回归继续验证混合召回、重排预算、片段扩展和固定语料的检索指标。
