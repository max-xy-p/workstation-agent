# PostgreSQL 存储与部署

项目使用 PostgreSQL 保存运行记录、文本记忆、RAG 文档快照和 Agent 检查点。
数据库服务需要先启动，应用不会创建 PostgreSQL 数据库，也不会回退到 SQLite。
前端、HTTP 路由和 Agent 核心通过应用用例及存储协议访问数据，不直接执行 SQL。

## 本机启动

已安装 PostgreSQL 时，准备一个专用数据库和可创建 schema、表及索引的账号即可。
使用 Docker Desktop 时，可在项目根目录的 PowerShell 7 中运行：

```powershell
# 把示例替换为自己保存的密码；已有数据卷继续使用首次初始化时的密码。
$env:POSTGRES_PASSWORD = '替换为你的数据库密码'
docker compose up -d --wait postgres
$dbPassword = [uri]::EscapeDataString($env:POSTGRES_PASSWORD)
$env:WORKSTATION_AGENT_DATABASE_URL = "postgresql://workstation:${dbPassword}@127.0.0.1:5432/workstation_agent"
uv sync
uv run workstation-agent init-db
uv run workstation-agent doctor --database
```

`compose.yaml` 使用官方 PostgreSQL 18 镜像，仅将端口绑定到本机，并将数据保存在命名数据卷中。
`init-db` 是显式写入用例，通过工具权限边界初始化业务表和官方检查点表；重复运行不会清空数据。
`doctor --database` 只检查连接，不执行建表或迁移；成功时输出 `PostgreSQL connection: ok`。
普通 `doctor` 只显示连接是否配置，不打印地址、用户名或密码，也不发起数据库连接。
其他终端或服务进程必须配置同一个连接环境变量；Agent 不会自动读取 `.env`。

停止容器并保留数据使用 `docker compose stop postgres`。
已有数据卷的账号和密码不会因为修改环境变量而自动改变；密码修改应通过 PostgreSQL 管理操作完成。

## 配置与连接行为

| 环境变量 | 默认值 | 用途 |
| --- | --- | --- |
| `WORKSTATION_AGENT_DATABASE_URL` | 未配置 | `postgresql://用户:密码@主机:端口/数据库`；密码等特殊字符必须 URL 编码 |

未配置连接时，数据库用例返回固定的配置错误。纯文件工具、工具发现和普通配置检查不要求数据库。
运行 Agent 默认会读取记忆，HTTP 服务还会保存会话，因此这些入口需要配置数据库。
旧 `WORKSTATION_AGENT_DATA_DIR` 配置已移除。

业务数据使用 Psycopg 官方连接池；检查点使用独立的字典行连接池，保持官方驱动类型。
连接池在首次使用时创建，进程退出时关闭；每类池最多 4 个连接。
归还连接前清除 schema 与只读设置，防止一次查询影响后续写入。
整次会话锁使用独立连接，关闭时由数据库释放，避免占用所有池连接导致检查点无法写入。
连接与池等待上限为 5 秒，单条 SQL 上限为 30 秒，等待数据库锁上限为 5 秒。
取消沿用协作机制，已经开始的 SQL 可能需要等待数据库超时后才能收尾。

## 数据分类与隔离

| 数据 | 保存位置 | 管理方式 |
| --- | --- | --- |
| Run 与步骤摘要 | `workstation.runs` | 运行与步骤作为一个 JSONB 结果保存；当前仍需显式注入 Repository |
| 会话与文本历史 | `workstation.conversations`、`conversation_messages` | 标题、版本号及用户/助手文本，按可信用户、工作区和项目隔离；见 [会话管理](conversations.md) |
| 长期记忆正文 | `workstation.memory_records.content` | 正文保存在 TEXT 列，来源等元数据使用 JSONB |
| 记忆版本与提取批次 | `workstation.memory_scopes`、`memory_batches` | 版本冲突、整批回滚和遗忘保护 |
| RAG 文档、分块和向量快照 | `workstation.rag_documents` | 按来源发布 JSONB 快照，继续使用现有官方检索组件 |
| 框架检查点 | `workstation_checkpoints` schema | 官方 `PostgresSaver` 管理表和迁移；业务 CRUD 不直接修改这些表 |

业务表查询都带有由配置工作区真实路径派生的 `workspace_id`，保留原来不同 SQLite 文件提供的隔离。
记忆还保留用户、项目等范围检查；用户范围记忆不会因为共用 PostgreSQL 而自动跨工作区共享。
框架 thread_id 继续绑定可信用户、工作区、项目和会话 ID。
这些规则是应用的数据访问约束，不替代 PostgreSQL 账号权限。

记忆写入与遗忘使用同一工作区事务锁，同时保护跨范围记录 ID、条数预算和提取版本。
RAG 发布与删除使用工作区预算锁，保证并发发布不能突破总块数上限。
Agent 执行和会话删除使用非阻塞数据库会话锁，其他进程操作同一会话时返回 `session_busy`。
HTTP 服务的最近运行列表仍在内存中保存；换数据库本身不等于已交付持久历史查询接口。

## 迁移和旧数据

`005_postgresql.sql` 是建立 PostgreSQL 业务表的版本化迁移脚本，由 `workstation.migrations` 记录版本 5。
`006_conversations.sql` 新增会话及文本消息表，记录版本 6；初始化按版本顺序检查，仅执行尚未执行的迁移。
首次获准写入业务数据时也会自动检查迁移；只读查询不创建 schema 或表。
历史 `001`–`004` SQL 文件保留用于说明旧 SQLite 结构，PostgreSQL 初始化不会执行它们。
官方检查点迁移独立记录在 `workstation_checkpoints.checkpoint_migrations` 中，由官方组件执行。

此次更换存储不自动导入或删除旧 `.db` 文件。重建方式如下：

- RAG：保留原始文件，通过 `rag ingest 路径 --approve-write` 重新摄取，重建索引及派生结果。
- 长期记忆：先备份旧文件，对需要保留的内容核对后通过 `memory add 内容 --approve-write` 重新录入。
  新记录获得新 ID、版本和来源，当前没有保持旧 ID 的自动导入工具。
- 对话和检查点：使用新会话重新开始；SQLite 检查点不能直接复制到官方 PostgreSQL 表。
  旧待审批任务需要先核对已经完成的外部操作，再决定新任务内容，避免重复副作用。
- 旧 Run/Step：原文件可留作归档，当前 PostgreSQL 查询入口不会读取旧文件。

不会为了兼容旧格式保留第二套存储实现。需完整保留旧业务历史时，应另行设计显式导入用例，不能把复制数据库文件当成迁移。

## 框架依据和维护成本

数据库驱动和连接池分别采用 `psycopg[binary]`、官方 `psycopg_pool`，均声明为直接运行依赖。
检查点采用 `langgraph-checkpoint-postgres` 中的官方 `PostgresSaver`。
现有授权包装器已有同步协议和线程适配，因此复用同步 Saver；HTTP 中的异步图调用仍通过包装器的异步方法在线程中执行存储操作。
这避免额外维护同步、异步两套授权实现，也适配当前 Windows 工作站环境。

参考：[Psycopg 连接池](https://www.psycopg.org/psycopg3/docs/advanced/pool.html)、
[LangGraph PostgreSQL 检查点](https://reference.langchain.com/python/langgraph.checkpoint.postgres)、
[PostgreSQL 显式锁](https://www.postgresql.org/docs/current/explicit-locking.html)。

业务存储复用原 Repository 协议，以参数化 SQL 实现项目特有的版本、预算和范围规则。
官方 Store 的通用键值操作不能直接替代整批记忆版本校验、遗忘版本与来源快照发布，
因此保留这些最小业务适配；不另写驱动、连接池、检查点格式或检索算法。
替代方案是引入 ORM 与独立迁移框架，但目前没有需要它们解决的对象映射能力缺口，因此此次不增加这两类依赖。
运行成本新增 PostgreSQL 服务、数据卷、连接和备份管理；Psycopg binary 安装不要求本机编译 libpq。

静态类型检查直接使用官方行、连接和检查点类型。
数据库行值属于第三方通用数据边界，结果通过 Pydantic 转为领域对象或原生文档快照。
仅打包迁移资源使用一次 `LiteralString` 类型断言：SQL 来自固定包资源，不接受外部输入。
官方序列化边界保留开放类型，并沿用工具正文清除和秘密脱敏规则。

## 开发与回归

测试需要一个专用的 PostgreSQL 数据库，账号须有建表和 schema 权限。
先设置 `WORKSTATION_AGENT_DATABASE_URL`，再执行：

```powershell
uv run python -m unittest discover -s tests -v
uv run pyright
uv run workstation-agent doctor --database
uv build
```

测试使用临时工作区隔离业务记录，但会在所配置数据库中留下测试数据；不要指向生产或个人重要数据库。
集成测试验证真实 PostgreSQL、独立 CLI、记忆批次回滚、并发容量、工作区隔离、会话锁和审批恢复。
它们不调用外部模型，也不代表模型质量验收。
