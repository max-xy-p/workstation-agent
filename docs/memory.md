# 长期记忆

记忆保存可跨会话复用的用户偏好、项目决策和经验，以自然语言正文加来源、范围、版本和有效期管理。
外部文档属于 [RAG](rag.md)，会话续聊属于 [Agent 使用](agent.md#会话续聊)。

## 管理记忆

先配置 [PostgreSQL](database.md)。以下命令无需聊天模型，`memory_id` 替换为返回的记录 ID：

```powershell
$env:WORKSTATION_AGENT_PROJECT_ID = 'workstation-agent'
uv run workstation-agent memory add "本项目使用 uv 管理依赖" --kind decision --approve-write
uv run workstation-agent memory list
uv run workstation-agent memory search "依赖管理"
uv run workstation-agent memory get memory_id
uv run workstation-agent memory update memory_id "本项目改用 Poetry" --version 1 --approve-write
uv run workstation-agent memory delete memory_id --version 2 --approve-delete
uv run workstation-agent doctor --memory
```

返回 ToolResult JSON，`content` 是记录或列表的 JSON 文本。修改、删除使用刚读取的 `version`，
冲突返回 `version_conflict`，不会自动覆盖；权限不足退出码为 3。
版本示例须替换为实际值。完全相同的有效笔记合并来源并增加版本，不按相似度自动合并不同事实。

默认保存到当前项目，未配置项目时保存到工作区。`add --scope user/workspace/project` 可选择范围，
项目范围必须有项目配置；按 ID 访问也检查归属。用户范围可在当前工作区跨项目使用，不自动跨工作区共享。
配置身份用于本机隔离，不是多用户认证。

新增可用 `--expires-at` 指定带时区的 ISO 时间；更新省略时保留原值，传 `never` 清除有效期。
过期记录可管理，但不参与召回。

## Agent 召回与写入

```powershell
uv run workstation-agent run "本项目使用什么依赖管理方式？" --transport local
uv run workstation-agent run "记住：本项目优先使用中文注释" --transport local --approve-memory-write
```

读取默认允许；新增/修改需要 `--approve-memory-write`，遗忘另需 `--approve-memory-delete`。
这些授权不扩大文件或 RAG 权限。MCP 模式的记忆工具仍绑定客户端配置身份，在本机执行。

需要一起保存多条明确约定时，Agent 可以调用 `save_memory_batch` 提交最多五条候选。
新增候选提供正文和类别。修改候选还需指定记录 ID 和读取时的版本。
工具会先核对范围、重复内容和版本，按写入策略展示待确认内容，再将整批写入数据库。
发现目标冲突时，先让用户明确要保留的记录。语义相似本身不会触发覆盖。
同一运行内重复提交相同批次时，返回 `already_processed`。等待期间目标变化或发生遗忘时，旧计划不能提交。

服务先检查范围和有效期，再用 BM25Plus 排序整条笔记；中文按双字、英文按单词/标识符检索，
不调用 embedding，不保证同义改写召回。无相关结果不注入，超预算按整条舍弃。
Context 每次调用模型时临时注入记忆，不把召回内容追加到保存的消息历史。
记忆不覆盖当前要求、系统规则或工具权限；来源 ID 和版本记入步骤，不记录正文。
替换检索方式见 [开发指南](development.md#检索策略)。

## 自动提取

```powershell
$env:WORKSTATION_AGENT_MEMORY_AUTO_EXTRACT = 'true'
$env:WORKSTATION_AGENT_MEMORY_ALLOW_REMOTE = 'true'
uv run workstation-agent doctor --memory
uv run workstation-agent run "我长期偏好使用中文交流" --transport local
```

还需配置模型名称、地址和密钥。默认关闭；成功任务结束后使用 LangMem，在整次运行预算内提取。
仅读取本次用户文本，不扫描完整历史、工具输出或 RAG；候选须有本次文本的原文证据且标记为确定。
可新增或更新自动记忆，不能覆盖显式记忆。不确定候选仅报告 pending 数量，不保存待审队列。

批次提交时校验范围、版本和预算，同一 Run 不重复处理。删除记忆会增加所属范围版本，
在删除前启动的提取批次因此不能把内容写回。自动写入只授予内部提交工具，不扩大 Agent 权限。
原文证据校验不等于模型理解正确，实际提取效果仍需验收。

`memory_extraction` 步骤记录 saved/pending 或安全错误码。
普通提取失败保留主任务答复；整次超时仍返回 `limit_reached`。
提取会增加调用费用和延迟，已开始的阻塞模型请求不保证立即取消。

## 遗忘与保留边界

删除清除记忆正文，不清除原始会话、备份或已出站内容；模型曾复述的事实仍可能存在于旧对话中。
需要时另行清除对应会话，清除会话也不会删除长期记忆。
旧批次 ID 保留以防重放，但不保留正文；新消息再次表达相同事实仍可能形成新记忆。
应用层删除不保证 PostgreSQL 空闲页或外部备份的物理擦除。

## 配置

以下名称统一加 `WORKSTATION_AGENT_` 前缀，布尔值接受 true/false/1/0，项目不自动读取 `.env`。

| 名称 | 默认值 | 用途 |
| --- | --- | --- |
| MEMORY_ENABLED | true | 启用长期记忆读写入口；默认策略仅允许读取 |
| USER_ID | local | 当前配置身份 |
| PROJECT_ID | 空 | 当前项目；缺省时不读取任何项目范围 |
| MEMORY_AUTO_EXTRACT | false | 成功任务后的有界提取 |
| MEMORY_ALLOW_REMOTE | false | 单独授权提取和上下文摘要的额外模型请求 |
| MEMORY_CONTEXT_CHARS | 4000 | 实际注入记忆的字符预算 |
| MEMORY_SEARCH_LIMIT | 5 | 默认 Context 召回条数，最大 20 |
| MEMORY_MAX_RECORDS | 2000 | 每个命名空间的记录上限，最大 10000，含过期记录 |
| MEMORY_EXTRACT_CHARS | 6000 | 当前用户消息与已有记忆正文的提取预算 |
| MEMORY_EXTRACT_LIMIT | 5 | 单批候选上限，最大 20 |
| MEMORY_EXTRACT_CALLS | 3 | 单批模型请求上限，最大 8；没有自动网络重试 |

`MEMORY_EXTRACT_CHARS` 只限制输入文本，不代表包含 schema 与提示词后的精确 Token 用量。
`doctor --memory` 只读检查记录可访问性；开启提取却缺少出站授权或模型配置时失败，不发送探测请求。
存储使用 Psycopg 与业务 Repository，整批版本、遗忘和容量约束的实现依据见 [架构](architecture.md#框架选择与必要扩展)。
