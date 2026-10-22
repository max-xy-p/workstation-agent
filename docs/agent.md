# Agent 使用

Agent 执行自然语言任务，返回答复和执行结果。模型必须支持 OpenAI 兼容的 Chat Completions 与工具调用。
先按 [README](../README.md) 配置模型、真实上下文窗口和 [PostgreSQL](database.md)。
下列命令在 `backend/` 执行，文件工具的路径相对于配置的工作区。

## 执行任务

```powershell
uv run workstation-agent run "读取 backend/.python-version，说明项目需要的 Python 版本" --transport local --trace
```

`local` 在同进程使用工具；默认 `mcp` 模式需要先启动下面的独立工具服务。
两种模式使用相同的参数和权限检查。`--trace` 在 stderr 显示已完成的图节点，不输出 Prompt 或工具参数，也不是逐 Token 输出。

stdout 为 Run JSON，包含 `id`、`session_id`、`status`、`answer` 和 `steps`；任务状态栏见 [上下文管理](context.md)。
主要结果如下：

| 状态 | 含义 |
| --- | --- |
| `completed` | 模型已给出最终答复；仍需核对工具结果和任务验收条件 |
| `waiting_approval` | 工具尚未获准执行，等待用户决定 |
| `limit_reached` | 达到时间、调用次数或上下文限制 |
| `failed` | 模型、连接或其他执行失败，返回安全错误说明 |
| `cancelled` | 用户或应用取消了活动任务 |

文件不存在、路径越界等工具错误会交回模型解释或修正，因此 Run 完成不等于所有工具都成功。

以下配置均带 `WORKSTATION_AGENT_` 前缀：

| 配置 | 默认值 | 用途 |
| --- | --- | --- |
| `MAX_STEPS` | 8 | 每次任务的模型调用上限 |
| `MAX_TOOL_CALLS` | 32 | 每次任务的工具调用上限 |
| `MODEL_TIMEOUT` | 30 | 单次模型请求与 MCP 读取超时秒数 |
| `RUN_TIMEOUT` | 300 | 整次任务限时秒数，包含审批等待、摘要与记忆提取 |
| `SESSIONS_ENABLED` | false | 默认持久化会话；也可用 `--session` 单次启用 |

模型客户端不自动重试，图不自动重试工具副作用。配置总表见 [后端配置示例](../backend/.env.example)。

## 会话续聊

```powershell
uv run workstation-agent run "讨论项目结构" --session project-chat --transport local
uv run workstation-agent run "继续刚才的讨论" --session project-chat --transport local
uv run workstation-agent session clear project-chat --approve-delete
```

会话 ID 允许 1–128 个字母、数字、下划线或连字符，按配置的用户、工作区和项目隔离。
启用持久化后，官方检查点保存执行状态，业务表保存用户文本和最终答复；HTTP 服务始终启用持久化。
普通 CLI 任务默认独立执行，不保存对话。启用默认持久化却未指定 ID 时会生成新 ID，使用返回的 `session_id` 续聊。

`session clear` 清除文本历史、摘要和检查点，保留会话元数据；长期记忆需要 [单独删除](memory.md#遗忘与保留边界)。
检查点不保留原始工具正文和 artifact，已知凭据会脱敏；需要工具内容时重新读取。

## 查看和恢复审批

跨 CLI 命令恢复任务时，首次运行就必须保存会话：

```powershell
uv run workstation-agent run "把 README.md 加入知识库" --transport local --session review
uv run workstation-agent approvals review --transport local
# approval-id 替换为查询返回的中断 ID；有多项时重复 --decision。
uv run workstation-agent resume review --transport local --decision "approval-id=approve"
```

查看工具和完整参数后，对全部待审批项提交 `approve` 或 `reject`；遗漏、旧 ID 或不匹配的参数都会被拒绝。
批准只覆盖指定调用，后续调用仍检查权限；拒绝作为工具错误交回模型。待审批会话不能另开任务覆盖它。

恢复沿用原 Run ID、调用次数和截止时间，不重新计时，也不重放已成功的并行工具节点。
普通失败或已停止的任务不能通过旧审批重新启动。未持久化的审批仅能在同一个应用实例中恢复，CLI 退出后丢失。

## 取消与超时

CLI 按 Ctrl+C；应用调用方通过 `on_started` 回调取得 Run ID，再调用 `AgentApplication.cancel(run_id)`。
HTTP 调用见 [运行接口](agent-service.md#运行接口)。超时返回 `limit_reached`，取消标记为 `cancelled` 并继续传播取消异常。

取消采用协作方式，工具线程结束前仍持有会话锁。第三方阻塞调用可能需要等自身超时；
已完成的外部操作不会撤销。进程崩溃后应先核对外部结果，不能假设恢复保证工具严格执行一次。

## 文件工具

```powershell
uv run workstation-agent tools --json
uv run workstation-agent tool list_files
uv run workstation-agent tool read_text_file --path README.md --trace
```

文件工具无需模型或数据库。`tools --json` 返回当前可用工具的名称、权限和参数 schema。
结果为 ToolResult：`tool_name`、`content`、`success`、`error`、`error_code`。
退出码为 0 成功、1 执行错误、2 请求或配置错误、3 需要授权。

路径解析为真实路径后必须仍在工作区内；目录中的链接只显示为链接，不读取外部目标。
这是路径检查，不是操作系统沙箱。默认工具输出上限为 50000 字符，超限带截断标记。
`read_text_file` 支持 `offset`、`limit` 和 `expected_version` 分段读取；文件变化返回 `source_changed`。
RAG 和记忆工具的授权分别见其使用文档，通用文件写入与 Shell 尚未提供。

## 本机 MCP 服务

`serve` 只提供工作站工具，默认地址为 `http://127.0.0.1:8765/mcp`；
`serve-agent` 提供完整任务与会话 HTTP 接口，见 [HTTP 文档](agent-service.md)。

在 Windows PowerShell 7 中，从项目根目录启动工具服务：

```powershell
$service = & .\scripts\start-local-service.ps1
uv run python examples/local_agent_client.py read_text_file --path backend/.python-version
uv run workstation-agent run "读取 backend/.python-version 并回答" --trace
# 不再需要时停止本终端启动的服务。
$service.Kill()
$service.WaitForExit()
```

脚本等待健康检查通过并返回进程句柄；缺少令牌时在当前终端生成令牌，不打印或写入文件。
客户端需从同一终端启动以继承它；不同终端必须注入相同的服务令牌。
前台启动可设置令牌后运行 `uv run workstation-agent serve`，按 Ctrl+C 停止。

| 环境变量 | 默认值 | 用途 |
| --- | --- | --- |
| `WORKSTATION_AGENT_WORKSPACE` | 当前目录；脚本默认仓库根目录 | 工具允许访问的已存在目录 |
| `WORKSTATION_AGENT_SERVICE_PORT` | 8765 | MCP 端口，范围 1–65535 |
| `WORKSTATION_AGENT_SERVICE_TOKEN` | 未配置 | 32–256 个字母、数字、下划线或连字符 |
| `WORKSTATION_AGENT_TOOL_OUTPUT_CHARS` | 50000 | 工具输出字符上限 |

`doctor --service` 只检查配置，不探测运行进程或端口。`/health` 无需令牌；
MCP 请求通过 Bearer 令牌认证，并校验 Host 和 Origin。服务只监听 `127.0.0.1`，不信任代理头，请求正文上限 64 KiB。

外部 Agent 可参考 [独立客户端示例](../backend/examples/local_agent_client.py)，使用官方 MCP SDK 发现和调用工具。
`structured_content` 是完整 ToolResult，`content[0].text` 是它的 JSON 文本，业务失败带 `is_error=true`。
MCP 不保存协议会话或运行检查点；后台脚本不提供自动重启或登录自启。
