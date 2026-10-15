# 记忆系统使用与验收

> 2026-10-15。文本长期记忆、可选持久化会话和默认关闭的自动提取已接入真实入口。
> 总体方案见 [记忆系统设计](memory-design.md)，交付状态见 [功能模块](modules.md)。

## 长期记忆

每条记忆是自然语言正文加作用域、来源、版本和有效期。SQLite 的 TEXT 列是正文唯一事实源；
偏好、项目决策和经验不分卡片格式。下面的命令不需要配置聊天模型。

~~~powershell
$env:WORKSTATION_AGENT_PROJECT_ID = 'workstation-agent'
uv run workstation-agent memory add "本项目统一使用 uv 管理 Python 依赖" --kind decision --approve-write
uv run workstation-agent memory list
uv run workstation-agent memory search "Python 依赖管理"
uv run workstation-agent memory get <memory_id>
uv run workstation-agent memory update <memory_id> "本项目改用 Poetry 管理依赖" --version 1 --approve-write
uv run workstation-agent memory delete <memory_id> --version 2 --approve-delete
uv run workstation-agent doctor --memory
~~~

管理命令输出统一 ToolResult JSON；其 content 是记忆对象或分页结果的 JSON 文本。
操作失败不回显原始异常；权限不足退出码为 3。修改和删除必须使用刚读取的 version，
冲突返回 version_conflict，不自动覆盖。

默认保存到当前项目；未配置项目时保存到当前工作区。add --scope user/workspace/project 可选范围，
project 必须已配置。所有按 ID 操作也检查身份与范围。用户范围笔记在当前工作区的数据库内可跨项目使用，
不提供跨工作区数据库聚合。当前是单人工作站模型，配置身份不是多用户认证机制。

新增可指定 --expires-at 带时区的 ISO 时间；更新省略该参数时保留原有效期，
传 never 清除有效期。过期笔记可查看、修改和删除，但不参与相关性召回。
完全相同的有效笔记合并来源并提升版本；不依据相似度自动合并不同事实。

## Agent 召回与显式写入

~~~powershell
uv run workstation-agent run "本项目使用什么依赖管理方式？" --transport local
uv run workstation-agent run "记住：本项目优先使用中文注释" --transport local --approve-memory-write
~~~

默认允许检索，新增和修改需要 --approve-memory-write；遗忘另需 --approve-memory-delete。
这些授权只作用于本次运行的记忆工具，不授权文件、RAG 或其他工具写入。
MCP 传输继续调用独立服务的文件/RAG 工具，记忆工具绑定客户端当前配置身份并在本机执行。
serve 可发现记忆工具，但默认策略仍不允许写入；CLI 管理和自然语言写入使用上述真实入口。

Context 先限定用户/工作区/当前项目，再调用共享的官方 Retriever 扩展和 BM25Plus，
支持英文标识符和中文双字词。索引按允许范围的有效正文即时构建，不保存第二份内容或陈旧缓存。
这是词项召回，不保证同义改写的语义召回；当前无需 embedding、向量服务或额外网络请求。
记忆条数和字符预算独立限制；无相关内容不注入，超预算按整条丢弃。
ContextSources 对应的实际 ID 记录到 Run 的 context_sources 步骤中，不记录正文。

动态记忆经官方模型调用中间件瞬时注入，不追加到检查点消息历史。工具检索结果统一标为数据。
记忆不覆盖当前要求、系统规则和权限；外部文档事实由独立 RAG 提供引用证据。

## 持久化会话

~~~powershell
uv run workstation-agent run "我们开始讨论项目结构" --session project-chat --transport local
uv run workstation-agent run "继续刚才的讨论" --session project-chat --transport local
uv run workstation-agent session clear project-chat --approve-delete
~~~

--session 是对本次会话持久化的显式授权；也可通过 SESSIONS_ENABLED 默认启用，
此时未提供 --session 会生成新 ID，返回的 session_id 可用于续聊。
会话 ID 只接受 1–128 位字母、数字、下划线或连字符。
框架 thread_id 由可信身份、当前范围和会话 ID 一起派生，其他项目不能读取或清除同名会话。

使用官方 SqliteSaver，业务 RunRepository 不承担恢复能力。运行与清除通过本机文件锁互斥，
同一会话跨进程忙碌时拒绝操作；锁文件只有占位字节，不含消息正文。
未授权额外模型出站时使用官方 trim_messages；授权后使用官方 SummarizationMiddleware，
摘要请求同样经过敏感操作策略，每次运行最多一次。会话恢复不等于工具审批恢复；
失败或等待审批后不承诺继续原执行点，应清除会话或开启新会话重试。

持久化时保留消息协议和调用 ID，原始工具正文/artifact 替换为占位符，已知 API Key、
服务令牌及常见凭据模式被脱敏。工具内容需要时重新读取。会话消息、模型答复和摘要仍可能含业务信息；
这里的秘密检测不是通用敏感信息识别或磁盘加密。

## 自动提取

~~~powershell
$env:WORKSTATION_AGENT_MEMORY_AUTO_EXTRACT = 'true'
$env:WORKSTATION_AGENT_MEMORY_ALLOW_REMOTE = 'true'
uv run workstation-agent doctor --memory
uv run workstation-agent run "我长期偏好使用中文交流" --transport local
~~~

还需配置 MODEL、BASE_URL 和 API_KEY。只有成功结束的任务进入有界运行后处理；
提取读取本次用户消息，不扫描完整历史、不从原始工具输出或 RAG 文档提取用户事实。
这是当前保守输入范围；自动总结执行经验需要后续接入专门的已验证结果来源。

复用 LangMem create_memory_manager 的无 Store API，每次输入先校验秘密和字符预算，
现有记忆只选当前写入范围内且预算允许的相关条目。只提交 certain 且 evidence 是本次用户文本原文片段的候选。
证据检查防止无依据写入，但不等于证明模型对语义的理解正确，外部模型效果需另行验收。

可以新增或更新已有自动记忆；显式记忆只允许经显式修改，不允许自动提取覆盖。
不确定候选只返回 pending 数量，不保存待审队列；需要用户使用显式管理入口处理。
框架返回完整快照时先过滤未变化事实，再计算新候选预算。
整个批次事务提交；限制候选数和模型请求数，框架修复调用也计入请求预算。
同一来源 Run 幂等，删除提升范围 generation，提交时再次校验，拒绝遗忘前的在途候选。
自动写入权限只授予内部提交工具，不扩大 Agent 可见写入权限。

Run 的 memory_extraction 步骤单独返回 saved/pending 或安全错误码。
失败不改变主任务 completed 状态，运行后处理可能增加响应延迟；
已经进入线程的模型调用不能保证立即响应协程取消。

## 遗忘与保留边界

删除清除长期正文，召回按数据库有效正文重建；旧批次 ID 保留但不保留正文，防止已处理来源重放。
它不清除原始会话或外部备份。需要清除对话时另外执行 session clear；清除会话也不会删除长期记忆。
模型先前答复可能已复述事实，需一并清除相应会话才能避免从历史重新获得。
新消息再次表达同一事实仍可形成新记忆，不承诺识别所有同义改写。
SQLite 空闲页、备份及已出站内容不属于应用层删除的物理擦除保证。

## 配置

下表名称均加 WORKSTATION_AGENT_ 前缀；布尔只接受 true/false/1/0，项目不会自动读取 .env。

| 名称 | 默认值 | 用途 |
| --- | --- | --- |
| MEMORY_ENABLED | true | 启用长期记忆读写入口；默认策略仅允许读取 |
| USER_ID | local | 当前配置身份 |
| PROJECT_ID | 空 | 当前项目；缺省时不读取任何项目范围 |
| SESSIONS_ENABLED | false | 默认持久化会话；--session 可单次启用 |
| MEMORY_AUTO_EXTRACT | false | 成功任务后的有界提取 |
| MEMORY_ALLOW_REMOTE | false | 单独授权提取和会话摘要的额外模型请求 |
| MEMORY_CONTEXT_CHARS | 4000 | 实际注入记忆的字符预算 |
| MEMORY_SEARCH_LIMIT | 5 | 默认 Context 召回条数，最大 20 |
| MEMORY_MAX_RECORDS | 2000 | 每个命名空间的记录上限，最大 10000，含过期记录 |
| MEMORY_EXTRACT_CHARS | 6000 | 当前用户消息与已有记忆正文的提取预算 |
| MEMORY_EXTRACT_LIMIT | 5 | 单批候选上限，最大 20 |
| MEMORY_EXTRACT_CALLS | 3 | 单批模型请求上限，最大 8；没有自动网络重试 |
| MEMORY_SUMMARY_TOKENS | 12000 | 历史裁剪/摘要触发预算，摘要保留最近 8 条消息 |
| DATA_DIR | 工作区下 .workstation-agent | memory.db 与 sessions.db 所在目录，必须位于工作区内 |

EXTRACT_CHARS 约束文本内容，不代表包含框架 schema 和提示词后的精确 token 预算。
doctor 打印开关、预算、路径及依赖版本；--memory 额外只读检查记录可访问性。
开启自动提取却缺少出站授权或模型配置时诊断失败，不发送探测请求。

## 存储与验收

`004_memory.sql` 是创建长期记忆相关数据库表的迁移脚本。
首次通过权限检查、向记忆数据库写入数据时，程序会自动执行该脚本；已有的表不会重复创建。
当前没有默认旧长期记忆数据库需要迁移；
旧内存对象和导出已移除，调用方须使用受控应用入口。未来格式变化继续使用版本化 migration。
sessions.db 的表由官方 Checkpointer 管理，升级需同时验收框架版本及应用的隐私序列化器。

SQLite 长期适配是本机持久化的能力缺口：官方 Store 集成未列出 SQLite，
引入额外数据库服务成本不合适，因此保留最小 Repository 适配，不实现通用 Store 克隆。
共享检索组件由现有 RAG 的纯 BM25 扩展提取，两类数据、数据库和访问规则继续独立。
新增运行依赖为官方 SQLite Checkpointer 与 LangMem；LangMem 带入其上游依赖，
只在配置自动提取/摘要时加载相应模型适配。

验收包括作用域/无关召回回归、重启 CRUD、版本冲突、事务回滚、水位、过期、秘密拒绝、
真实 CLI、官方检查点续聊、会话清除、瞬时注入、真实 LangMem 离线协议及提取失败保留主结果。
执行仓库 unittest discover、doctor 和 uv build；离线夹具不代表已验证外部模型提取准确率。
