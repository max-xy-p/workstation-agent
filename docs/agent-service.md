# 独立 Agent HTTP 服务

Agent HTTP 服务让桌面界面或其他本机客户端提交完整任务，查询运行进度、取消任务和恢复工具审批。
`serve-agent` 启动 Agent 进程；现有 `serve` 仍启动 MCP 工具服务。
两者分别监听 `127.0.0.1:8766` 和 `127.0.0.1:8765`，使用各自的访问令牌。

## 配置和启动

先按 [README](../README.md) 配置模型名称、API Key、接口地址和真实上下文窗口。
还需要按 [数据库说明](database.md) 配置 PostgreSQL 并执行 `init-db`。
模型需要支持 OpenAI 兼容的 Chat Completions 和工具调用。
在项目根目录的 PowerShell 7 中运行：

```powershell
uv sync
$env:WORKSTATION_AGENT_HTTP_TOKEN = [guid]::NewGuid().ToString('N')
$service = & .\scripts\start-local-service.ps1
uv run workstation-agent doctor --http --agent
uv run workstation-agent serve-agent
```

`doctor --http` 检查 Agent HTTP 令牌、工作区、模型和上下文配置，不请求外部模型；
加上 `--agent` 还会要求 MCP 令牌已配置。该命令不探测端口或已运行的服务。
服务启动后访问 `/health` 返回 `status: ok`，说明应用和工具连接已完成初始化；这不验证外部模型可用性。
`serve-agent` 前台运行，按 Ctrl+C 停止；活动任务完成协作取消后才关闭工具连接。

同进程工具模式可独立启动，无需 MCP 服务：

```powershell
uv run workstation-agent doctor --http
uv run workstation-agent serve-agent --transport local
```

启动 `serve-agent` 表示允许在当前工作区内保存会话检查点，服务会启用现有会话持久化。
审批等待和会话续聊需要这些检查点；工具正文不会保存到会话中，已知凭据继续脱敏。
数据目录、身份隔离和删除会话的方式见 [记忆与会话说明](memory.md)。

| 环境变量 | 默认值 | 用途 |
| --- | --- | --- |
| `WORKSTATION_AGENT_HTTP_PORT` | `8766` | Agent HTTP 服务端口，必须为 1–65535 的整数；MCP 模式下不能与工具服务端口相同 |
| `WORKSTATION_AGENT_HTTP_TOKEN` | 未配置 | 启动前必须注入 32–256 个字母、数字、下划线或连字符；持有者可以提交任务及审批 |

项目不会自动读取 `.env`。HTTP 令牌仅由接口用于认证，不作为工具授权参数传给模型。
请求不能修改工作区、模型配置、工具权限或服务身份。

## 提交任务和查看结果

保持上述服务运行，在另一个已配置相同 HTTP 令牌的 PowerShell 终端中运行以下命令。
`$headers` 中应使用启动 Agent 服务时设置的令牌，不能重新生成一个不同值。

```powershell
$base = 'http://127.0.0.1:8766'
$headers = @{ Authorization = "Bearer $env:WORKSTATION_AGENT_HTTP_TOKEN" }
$body = @{ task = '读取 .python-version，回答需要的 Python 版本'; session_id = 'demo' } |
    ConvertTo-Json
$run = Invoke-RestMethod "$base/runs" -Method Post -Headers $headers `
    -ContentType 'application/json' -Body ([Text.Encoding]::UTF8.GetBytes($body))
Invoke-RestMethod "$base/runs/$($run.id)" -Headers $headers
```

提交成功返回 HTTP 202，以及应用实际创建的 `id` 和 `session_id`。
客户端随后轮询任务地址；任务完成时返回 `status: completed`、`answer` 和执行步骤摘要。
`node` 表示最近完成的框架节点，`task_status` 是已有的任务状态栏。
活动任务的 `status` 为 `thinking`；详细进度通过 `node` 和 `task_status` 查看。
节点名称不是实时工具状态，也不是模型逐 Token 输出。

程序运行结束不等于任务要求已经满足。客户端需要结合答复、步骤和任务状态栏判断结果。
请求提交成功后，即使客户端断开，任务也会继续执行；需要停止时调用取消接口。
网络中断后重试提交可能创建新任务，当前没有请求去重协议，应先查询运行记录再决定是否重试。

| 方法和路径 | 用途与结果 |
| --- | --- |
| `GET /health` | 无需令牌，返回不含配置和工作区路径的服务状态 |
| `POST /runs` | 提交 `task`，可选 `session_id`、`constraints`、`criteria`、`new_task`；返回 202 |
| `GET /runs` | 列出当前进程保留的运行记录，按最近更新时间排序 |
| `GET /runs/{run_id}` | 查看指定运行，不存在时返回 404 |
| `POST /runs/{run_id}/cancel` | 请求取消活动运行，返回 202；已结束时返回 200 和 `cancel_requested: false` |
| `GET /sessions/{session_id}/approvals` | 从检查点读取待审批项，按中断 ID 返回工具、参数和参数指纹 |
| `POST /sessions/{session_id}/resume` | 提交覆盖全部待审批项的 `decisions`，接受后返回 202，继续沿用原 Run ID |

服务一次执行一个任务，执行期间提交或恢复其他任务返回 409，不会排队。
运行期间查询审批同样返回 409，避免读取尚未稳定的检查点；运行查询始终可用。
同一个会话有待审批操作时，不能提交新任务覆盖它。
自动生成的会话 ID 会在提交响应中返回；手工指定的会话 ID 允许 1–128 个字母、数字、点、下划线和连字符。
任务正文最多 32000 个字符，单个请求正文最多 65536 字节；超出字节上限返回 413。

## 审批和恢复

任务返回 `waiting_approval` 后，用响应中的会话 ID 查询审批。
用户核对工具和参数后，对每个中断 ID 提交 `approve` 或 `reject`：

```powershell
$pending = Invoke-RestMethod "$base/sessions/demo/approvals" -Headers $headers
$decisions = @{}
# 本例拒绝全部待审批项；确认实际工具和参数后，可将对应项改成 approve。
$pending.PSObject.Properties | ForEach-Object { $decisions[$_.Name] = 'reject' }
$body = @{ decisions = $decisions } | ConvertTo-Json
Invoke-RestMethod "$base/sessions/demo/resume" -Method Post -Headers $headers `
    -ContentType 'application/json' -Body ([Text.Encoding]::UTF8.GetBytes($body))
```

决定遗漏、重复提交旧审批或当前没有待审批项时返回 409，客户端应重新查询。
恢复不会重新计时，默认整次任务限时 300 秒，包含审批等待；过期后返回 `limit_reached`。
待审批任务没有活动执行协程，取消接口对此返回 409；拒绝审批后由原 Agent 流程完成收尾。
取消仍采用已有的协作机制，已完成的工具副作用不能撤销，详见 [运行控制](run-control.md)。

## 保留范围和当前限制

运行记录只在当前进程内保留，最多 128 条；容量不足时删除最早结束的记录。
待审批记录不会自动淘汰，全部容量被待审批项占用时，新任务返回 503，恢复已有审批仍然可用。
这份列表不是任务历史数据库，进程重启后旧 Run ID 查询返回 404。
等待审批的操作仍保存在官方会话检查点中，可通过会话审批接口查询和恢复；恢复后原 Run ID 会重新出现在运行列表中。
恢复返回的是本次继续执行的步骤，不拼接完整历史。
进程异常退出不会自动重放运行中的任务，也不保证外部副作用严格执行一次。
服务当前使用单进程运行，未提供用户界面、事件订阅、多用户身份或远程部署入口。

未认证请求返回 401；非法浏览器来源返回 403；不匹配的 Host 返回 421。
非 JSON 请求返回 415，错误 JSON、额外字段或非法参数返回 422；错误说明不回显输入值。
服务关闭访问日志，返回结果及会话持久化均清除已知凭据；通用敏感内容识别仍受现有规则限制。
同一个服务令牌代表当前本机用户的全部权限，不支持按客户端划分授权。

## 实现选择与维护

服务复用已依赖的 Starlette、Uvicorn、Pydantic 和官方 Agent，不新增依赖。
[Starlette 生命周期接口](https://starlette.dev/lifespan/) 管理应用及 MCP 连接的打开和关闭，
官方请求正文大小限制处理超限请求，Pydantic 处理输入校验。
HTTP 路由仅调用 `application/agent_service.py` 的任务用例，工具和副作用仍经过原 `ToolExecutor`。

官方 Agent 已提供任务执行、审批恢复与取消，尚不负责本项目 HTTP 请求对应的活动任务句柄和限量查询记录。
因此自定义范围仅为一个 `asyncio.Task` 的保留、进度副本和 HTTP 数据转换；没有自行实现模型循环、检查点或持久任务队列。
维护成本集中在请求取消、服务关闭、审批恢复回调和内存记录淘汰；升级框架时需要重跑对应回归。

新增回归位于 `tests/integration/test_agent_http.py`，覆盖请求校验、来源与令牌、审批拒绝、
PostgreSQL 重启恢复、忙碌拒绝、超时及取消收尾。
`tests/integration/test_framework_agent.py` 额外启动真实 Agent HTTP 进程，经官方模型 SDK 的本机接口夹具和独立 MCP 服务完成读取。
这些回归验证本次服务实现，不调用外部模型，也不代表外部模型质量验收。
