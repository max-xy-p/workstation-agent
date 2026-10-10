# 工具模块：入口与验收

当前提供两个本地只读文件工具及四个 RAG 工具，共享发现、校验、权限执行、输出和 CLI/MCP 调用链。
RAG 摄取/删除需要受控写入批准，查询与健康检查只读；模型出站另需授权。见 [RAG 文档](rag.md)。
运行流程使用 LangGraph，参数和结果类型使用 Pydantic。

## 实际调用链

```text
CLI tool 命令
  → ToolApplication
  → ToolWorkflow / LangGraph
      → validate_request：Pydantic 校验请求结构
      → execute_tool：调用 ToolExecutor
          → 查找工具
          → Pydantic 校验工具参数并补齐默认值
          → ApprovalPolicy 检查权限
          → 执行工具
          → 校验输出类型、限制长度、规范化错误
      → 返回 ToolResult
  → CLI 输出 JSON 和进程退出码
```

错误的请求结构直接走图的结束分支。工具参数无效或权限不足时不会调用工具操作。
`--trace` 使用 LangGraph 的节点更新流，只在 stderr 输出节点名称，不输出请求参数。

## 可以直接使用的入口

从项目根目录运行：

```powershell
uv sync
uv run workstation-agent doctor
uv run workstation-agent tools --json
uv run workstation-agent tool list_files
uv run workstation-agent tool read_text_file --path README.md --trace
```

`tools --json` 返回名称、用途、权限和参数 schema。两个工具的参数均为 `path`：列表默认 `.`，
读取必须提供非空字符串。额外字段、数字、空白路径和包含 NUL 的路径都会被拒绝。
程序调用可使用 `--arguments` 传入 JSON 对象；PowerShell 手工调用推荐使用 `--path`。

成功结果示例：

```json
{"tool_name":"read_text_file","content":"文件内容","success":true,"error":null,"error_code":null}
```

每次调用的状态独立；调用工具无需模型密钥，也不会自动初始化运行数据库。

独立 Agent 服务可通过 MCP 使用同一工具执行链，启动和跨进程验收见[本机服务](local-service.md)。

## 手工验收

以下命令对应真实文件操作和失败路径：

| 命令 | 预期 |
| --- | --- |
| `uv run workstation-agent tool list_files` | 退出码 0，返回当前目录的文件列表 |
| `uv run workstation-agent tool read_text_file --path README.md --trace` | 退出码 0，返回文件内容；stderr 显示两个图节点 |
| `uv run workstation-agent tool read_text_file` | 退出码 2，`invalid_arguments` |
| `uv run workstation-agent tool missing` | 退出码 2，`unknown_tool` |
| `uv run workstation-agent tool read_text_file --path ../outside.txt` | 退出码 1，`path_outside_workspace` |
| `uv run workstation-agent tool read_text_file --path no-such-file.txt` | 退出码 1，`not_found` |

进程退出码：0 成功，1 执行或文件错误，2 请求/配置错误，3 需要权限批准。
已知错误返回安全说明；意外异常只返回 `execution_failed`，不回显底层异常和敏感数据。
工具内容受 `WORKSTATION_AGENT_TOOL_OUTPUT_CHARS` 限制，默认 50000。
长度超限时返回截断标记；极小上限可能只能容纳部分标记。文件读取本身也限制字符数量，
不会先读入整个大文件再截断。目录列表目前仍枚举并排序整个目录。

路径在访问前解析为真实路径，拒绝工作区外的目标，包括链接。目录列表把符号链接和 junction
显示为 `link`，不读取其外部目标。这是路径检查，不是操作系统沙箱，也不提供对并发替换链接的隔离。

## 新增工具

继承 `ToolArguments` 声明参数，再继承 `PydanticTool` 实现 `run()`：

```python
from workstation_agent.safety import PermissionLevel
from workstation_agent.tools import PydanticTool, ToolArguments

class EchoArguments(ToolArguments):
    text: str

class EchoTool(PydanticTool[EchoArguments]):
    name = "echo"
    description = "Return supplied text."
    permission = PermissionLevel.SAFE_READ
    arguments_model = EchoArguments

    def run(self, arguments: EchoArguments) -> str:
        return arguments.text
```

在注册表中注册后，schema 自动从参数模型生成，CLI、MCP 和以后模型调用使用同一个执行器。
生产调用必须经过 Application 或 ToolExecutor；不要直接调用工具的内部 handler。

工具能力使用 `PydanticTool` 声明并由执行器授权；注册表只提供发现和查找，不再提供兼容调用入口。
Agent 侧通过官方 `StructuredTool` 连接同进程或 MCP 能力。详见[框架教学](framework-tutorial.md)。

## 测试与本阶段边界

```powershell
uv run python -m unittest discover -s tests -v
uv run workstation-agent doctor
uv build
```

集成测试以真实 CLI 子进程访问临时工作区，验证中文 UTF-8、schema、退出码、错误参数、
非法 UTF-8、输出上限及路径越界。符号链接测试在账户不允许创建链接时会明确跳过。
单元测试验证权限拒绝时没有副作用、图分支、请求间隔离和不泄露异常内容。

RAG 已提供限定的索引写入和模型出站授权；通用文件写入、Shell、可恢复审批、检查点和整次任务统一超时尚未交付。
当前图没有配置持久化，也没有自动重试副作用。Agent 循环已由官方 `create_agent` 接管；旧同步 Orchestrator 和兼容 API 已移除。
模型使用官方 OpenAI 兼容集成，外部模型效果需另外验收。

## 框架依赖

Pydantic 用于严格校验、结果类型和 schema 生成，替代手写字段检查；LangGraph 用于状态和节点流转，
替代自行管理流程分支。两者及其传递依赖由 `uv.lock` 固定，安装和升级需要下载这些包。
这条本地工具流程不要求外部平台账户或模型调用。
