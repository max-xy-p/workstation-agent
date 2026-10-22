# PostgreSQL 部署与数据管理

项目使用 PostgreSQL 保存业务数据和官方 Agent 检查点，不使用 ORM，也不会回退到 SQLite。
需要一个专用数据库及可创建 schema、表和索引的账号，应用不代为创建数据库。

## 本机启动

已有 PostgreSQL 可直接配置连接；使用 Docker Desktop 时，在项目根目录的 PowerShell 7 中运行：

```powershell
$env:POSTGRES_PASSWORD = '替换为你的数据库密码'
docker compose up -d --wait postgres
$dbPassword = [uri]::EscapeDataString($env:POSTGRES_PASSWORD)
$env:WORKSTATION_AGENT_DATABASE_URL = "postgresql://workstation:${dbPassword}@127.0.0.1:5432/workstation_agent"
cd backend
uv sync
$env:WORKSTATION_AGENT_WORKSPACE = (Resolve-Path ..).Path
uv run workstation-agent init-db
uv run workstation-agent doctor --database
```

`compose.yaml` 使用 PostgreSQL 18，端口仅绑定本机，数据保存在命名卷。
`init-db` 通过授权入口初始化业务表和官方检查点，重复运行不清空数据。
`doctor --database` 仅检查连接，成功输出 `PostgreSQL connection: ok`；普通 `doctor` 不连接数据库。

`WORKSTATION_AGENT_DATABASE_URL` 默认未配置，格式为 `postgresql://用户:密码@主机:端口/数据库`，
特殊字符需 URL 编码。其他终端和服务进程需注入同一连接；项目不会自动读取 `.env`。

回到仓库根目录，停止并保留数据用 `docker compose stop postgres`。已有数据卷继续使用首次初始化的账号密码，
修改环境变量不会修改数据库内的密码。

## 数据与隔离

| 数据 | 保存位置 | 管理方式 |
| --- | --- | --- |
| Run / Step | `workstation.runs` | JSONB 结果，仍需显式注入 Repository；HTTP 最近运行列表在内存 |
| 会话与文本 | `workstation.conversations`、`conversation_messages` | 标题、版本、用户输入和最终答复，支持 [HTTP CRUD](agent-service.md#会话与消息接口) |
| 长期记忆 | `workstation.memory_records`、`memory_scopes`、`memory_batches` | TEXT 正文、版本、来源、批次与遗忘保护 |
| RAG | `workstation.rag_documents` | 完整文档快照，包含原文块与派生数据 |
| 框架检查点 | `workstation_checkpoints` schema | 官方 PostgresSaver 管理表和迁移 |

数据按可信工作区隔离，记忆、会话及检查点另绑定用户与项目；这些访问约束不替代数据库账号权限。
会话使用非阻塞数据库锁，跨进程同时操作返回 `session_busy`；
记忆提交与 RAG 发布分别用事务锁保护版本和容量，失败一起回滚。

业务数据和官方检查点使用独立 Psycopg 连接池，每类最多 4 个连接，首次使用时创建、退出时关闭。
整次会话锁单独占用连接，结束时关闭释放；池连接归还前清除会话设置。
连接/池等待上限 5 秒、SQL 30 秒、锁等待 5 秒。取消已开始的 SQL 可能仍需等待其超时。

纯文件工具、工具发现和普通配置检查不需要数据库；Agent 默认读取记忆，HTTP 保存会话，因此需要数据库配置。
业务写入先检查迁移；只读查询不会建表。

## 迁移与旧数据

`workstation.migrations` 记录已执行版本，初始化只执行尚未完成的脚本：

- `005_postgresql.sql` 创建 PostgreSQL 业务表。
- `006_conversations.sql` 新增会话与文本消息表。
- 历史 001–004 是 SQLite 结构记录，不用于 PostgreSQL 初始化。
- 检查点迁移由官方组件在 `workstation_checkpoints.checkpoint_migrations` 中独立管理。

不自动导入或删除旧 SQLite 文件。保留旧文件作备份后，按需要重建：

| 数据 | 重建方式 |
| --- | --- |
| RAG | 从原文件重新 `rag ingest 路径 --approve-write` |
| 长期记忆 | 核对旧正文后用 `memory add 内容 --approve-write` 重新录入，产生新 ID |
| 会话/审批 | 新建会话；SQLite 检查点不能直接复制，旧操作先核对外部结果以免重复执行 |
| Run / Step | 旧文件留作归档，当前查询不会读取 |

没有保留旧 ID 的自动导入工具，不能把复制数据库文件视为迁移。
新增持久化结构必须用新版本脚本，禁止修改已发布脚本语义。
实现依据和扩展方法分别见 [架构](architecture.md) 与 [开发指南](development.md)。
