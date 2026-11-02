# Agent HTTP 接口

本机客户端通过 HTTP 管理任务、运行状态、会话和文本历史。`serve-agent` 运行完整 Agent；
`serve` 仅提供 MCP 工具，两者使用不同端口和令牌。

## 启动与认证

先按 [README](../README.md) 配置模型与真实上下文窗口，按 [数据库部署](database.md) 配置 PostgreSQL 并执行 `init-db`。
在 `backend/` 目录使用同进程工具时：

```powershell
$env:WORKSTATION_AGENT_HTTP_TOKEN = [guid]::NewGuid().ToString('N')
uv run workstation-agent doctor --http
uv run workstation-agent serve-agent --transport local
```

默认 MCP 模式需先启动 [工具服务](agent.md#本机-mcp-服务)，再运行 `serve-agent`；
`doctor --http --agent` 同时检查两类服务配置，不请求模型或探测端口。
服务前台运行，Ctrl+C 会等待活动任务取消收尾后关闭连接。

| 环境变量 | 默认值 | 用途 |
| --- | --- | --- |
| `WORKSTATION_AGENT_HTTP_PORT` | 8766 | 端口 1–65535；MCP 模式不能与工具端口相同 |
| `WORKSTATION_AGENT_HTTP_TOKEN` | 未配置 | 32–256 个字母、数字、下划线或连字符 |

默认地址 `http://127.0.0.1:8766`。除 `GET /health` 外，请求须携带 `Authorization: Bearer 令牌`。
JSON 请求使用 `Content-Type: application/json`；项目不会自动读取 `.env`。
服务只监听本机，校验 Host 和 Origin，不提供跨域或远程部署配置。
同一令牌代表当前本机用户的全部接口权限，请求不能覆盖身份、工作区、模型或工具策略。

## 运行接口

| 方法与路径 | 请求与结果 |
| --- | --- |
| `GET /health` | 返回服务状态和版本；就绪不代表外部模型已验证可用 |
| `POST /runs` | 必填 `task`；可选 `session_id`、`constraints`、`criteria`、`new_task`；返回 202 和运行记录 |
| `GET /runs` | 持久化运行分页列表；可用 `session_id` 筛选会话，按更新时间和 ID 倒序 |
| `GET /runs/{run_id}` | 运行详情，不存在返回 404 |
| `DELETE /runs/{run_id}` | 删除已结束运行及其步骤，保留会话消息；活动或待审批运行返回 409 |
| `POST /runs/{run_id}/cancel` | 活动运行返回 202；已结束返回 200 和 `cancel_requested:false` |
| `GET /sessions/{id}/approvals` | 按中断 ID 返回工具、参数和参数指纹 |
| `POST /sessions/{id}/resume` | JSON `{"decisions":{"中断ID":"approve"}}`；可用 `reject`，必须覆盖全部待审批项；返回 202 |

在已配置相同 HTTP 令牌的终端中提交任务并轮询：

```powershell
$base = 'http://127.0.0.1:8766'
$headers = @{ Authorization = "Bearer $env:WORKSTATION_AGENT_HTTP_TOKEN" }
$body = @{ task = '读取 backend/.python-version'; session_id = 'demo' } | ConvertTo-Json
$run = Invoke-RestMethod "$base/runs" -Method Post -Headers $headers `
    -ContentType 'application/json' -Body ([Text.Encoding]::UTF8.GetBytes($body))
Invoke-RestMethod "$base/runs/$($run.id)" -Headers $headers
```

返回真实的 `id` 和 `session_id`。`status` 表示运行状态，`answer` 是最终答复，
`node` 是最近完成的图节点，`task_status` 是 [任务状态栏](context.md#任务状态栏)。
活动运行在 `thinking`、`acting` 和 `observing` 之间切换，节点更新不代表逐 Token 输出；任务完成仍需检查答复和验收条件。

服务一次执行一个任务；忙碌时提交、恢复或查询审批返回 409，不排队，运行查询仍可用。
`waiting_approval` 时，核对工具和参数后提交全部审批决定。
恢复沿用原 Run ID、调用预算和截止时间；等待审批不能用取消接口终止，应提交拒绝决定。
恢复会保留此前步骤，再追加本次执行的步骤。超时与取消规则见 [Agent 使用](agent.md#取消与超时)。

运行列表接受 `limit=1..100`（默认 50）、`offset=0..100000`（默认 0），
返回 `{"items":[...],"next_offset":50}`，末页为 `null`。查询与删除均按当前可信用户、工作区和项目隔离。
用户不能通过 PATCH 任意修改执行状态；运行创建与更新由提交、执行进度和审批恢复等应用用例触发。

提交成功后客户端断开不会取消运行。重试提交可能创建新任务，当前没有请求去重协议。
`task` 最多 32000 字符；`constraints`、`criteria` 各最多 32 条，每条 1–2000 字符。

## 会话与消息接口

会话 ID 允许 1–128 个字母、数字、下划线或连字符。服务按可信用户、工作区和项目隔离数据。
会话返回 `id`、`title`、`revision`、`created_at` 和 `updated_at`。

| 方法与路径 | 请求与结果 |
| --- | --- |
| `POST /sessions` | JSON `{"title":"调试项目"}`；标题可省略，默认“新会话”；返回 201 |
| `GET /sessions` | 按更新时间倒序分页，时间相同按 ID 排序 |
| `GET /sessions/{id}` | 会话详情 |
| `PATCH /sessions/{id}` | JSON `{"title":"新标题","revision":0}`；返回更新后的会话 |
| `DELETE /sessions/{id}?revision=0` | 删除会话、全部文本和检查点；返回被删除的 ID |
| `POST /sessions/{id}/messages` | JSON `{"content":"背景说明","revision":0}`；只保存用户文本，返回 201 |
| `GET /sessions/{id}/messages` | 按消息位置升序分页 |
| `GET /sessions/{id}/messages/{message_id}` | 消息详情，检查所属会话 |
| `PATCH /sessions/{id}/messages/{message_id}` | JSON `{"content":"改写的问题","revision":2}`；只允许编辑用户消息 |
| `DELETE /sessions/{id}/messages/{message_id}?revision=2` | 删除该消息及后续消息；返回 ID 和 `following_messages_deleted:true` |

标题为 1–200 字符，文本为 1–32000 字符，均不能只含空白。写入不接受角色字段，助手答复只能由 Agent 保存。
消息包含 `id`、`session_id`、`position`、`role`、`content`、`run_id` 和创建/更新时间。
`position` 只保证排序，删除后不重编号；单独新增的文本没有运行 ID。
列表不包含工具原文、模型内部消息或上下文摘要。

两种列表均接受 `limit=1..100`（默认 50）、`offset=0..100000`（默认 0）。
结果为 `{"items":[...],"next_offset":50}`，末页为 `null`。
并发写入会改变分页内容，重新从第一页读取可获得当前排序。

### 修改与删除规则

- 新会话版本为 0；消息或标题变更会增加 `revision`。修改前读取会话并传回该版本，冲突返回 409 和 `revision_conflict`，应重新读取后再决定如何修改。
- 编辑用户消息会删除其后全部消息；删除消息会连同其后消息一起删除。助手消息不能编辑，返回 `message_read_only`。
- 历史变更会清除整个会话检查点、摘要和任务状态栏；下次运行从保留的用户文本与最终答复重建上下文，不重新执行旧工具调用。
- 运行中或待审批时，追加、编辑、删除历史及删除会话返回 `session_busy`，重启后仍有效。待审批时可更名；运行期间可读取会话和消息。
- 删除会话会在同一业务事务中删除当前身份下关联的运行及步骤；长期记忆、RAG 和外部文件保留。
  编辑或截断消息不删除运行轨迹。CLI `session clear` 清除文本和检查点，保留会话元数据与运行归档。

### 保存文本与执行任务

需要 Agent 回答时，直接调用 `POST /runs`。应用自动建立缺失的会话、保存用户消息，完成后保存最终答复；
审批恢复不重复插入用户消息。**不要先新增同一条消息再提交同一任务，否则会得到两条用户消息。**

消息新增只补充背景；历史编辑不自动运行模型。修改后可显式提交“根据上面修改后的问题继续回答”等新任务。

## 错误与保留范围

| HTTP 状态 | 含义 |
| --- | --- |
| 401 / 403 / 421 | 令牌、Origin 或 Host 不符 |
| 404 | 当前身份可见的会话、消息或运行不存在 |
| 409 | 忙碌、审批不符、版本冲突或尝试编辑助手消息 |
| 413 / 415 / 422 | 正文超过 65536 字节、非 JSON 或参数无效 |
| 503 | 数据库未配置、暂不可用或运行记录无法提交 |

错误不回显输入、连接串或底层异常，服务关闭访问日志，持久化与响应清除已知凭据。

会话文本、检查点及 Run/Step 均保存到 PostgreSQL。运行在模型执行前创建，图更新完成后保存已观察到的步骤和任务进度，
审批暂停、恢复、取消及结束时继续提交。进程内最多保留 128 个查询缓存，淘汰缓存不删除数据库历史。
不保存模型内部思考或完整工具正文，步骤时间是观察时间，不代表真实调用耗时。

服务启动会先执行业务迁移，再核对未结束运行。核对时取得会话锁，其他进程仍持有锁的运行保持不变。
检查点仍有有效审批时保留 `waiting_approval`；其余失去执行进程的运行标记为终态 `interrupted`，关闭属于该 Run 的旧执行入口，
不自动重放工具。已提交轨迹仍可查询，旧审批恢复沿用原 ID 和全部已保存步骤。

首次保存失败不会接受任务；执行中保存失败会停止后续执行。数据库不可用时可能无法保存失败状态，当前进程会显示安全失败说明，
下次启动再核对最后一次成功提交的记录。工具可能已经成功而结果尚未保存，重试前必须核对外部实际结果。

历史修改与截断在一个业务事务中完成；检查点由官方组件使用另一连接删除。
程序持有会话锁，先清除检查点再保存文本，写入失败时保留原文本供下次重建。
进程崩溃不保证答复与文本同时落库，也不会自动重放任务或撤销已发生的操作。
旧检查点不会整体导入消息表，只从首次新运行或审批恢复开始记录本次任务文本。
React 工作台已接入这些接口，启动方法见 [README](../README.md#打开工作台)。
运行状态约每 1.8 秒轮询，当前没有逐 Token 流或事件订阅；前端展示任务项，用户验收仍使用 CLI。
未提供模型切换、文件上传、多用户认证或远程部署入口。
