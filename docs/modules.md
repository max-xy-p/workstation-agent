# 功能模块清单

> 更新日期：2026-10-07。项目已进行 0.2.0 破坏性框架重构，不保留旧 Agent API。

| 模块 | 当前状态 | 已实现范围 | 后续范围 |
| --- | --- | --- | --- |
| Agent Core | 官方框架运行链可用 | create_agent / LangGraph，官方模型和工具调用限制；移除手写循环 | 持久化会话、审批恢复、整次任务超时 |
| Model | OpenAI 兼容适配器可用 | 官方 ChatOpenAI，model/base_url/API key/超时配置，HTTP 接口验收 | 外部模型实际能力验收、用量统计 |
| Tools | 只读能力可用 | 官方 StructuredTool 适配、MCP/同进程调用、校验、权限、输出限制 | 写入、资源预算、结构化内容分页 |
| Application | 单次任务入口可用 | 异步 run_task、Context 组装、节点进度、结果状态、异常脱敏 | 会话/取消 API、审批接口 |
| Interfaces | CLI 与本机 MCP 可用 | 自然语言 run、工具发现、serve、Windows 启动脚本 | 用户界面、独立 Agent HTTP 服务 |
| Safety | 基础 | 四级权限，默认只读，拒绝时无工具副作用 | 可恢复审批、敏感内容过滤 |
| Domain | 业务对象可用 | Run/Step 结果摘要，与框架内部消息和状态分离 | 显式状态转换规则 |
| Context | 基础 | 唯一 Prompt 组装入口；可注入 Memory/RAG | token 预算、来源裁剪 |
| Memory | 未完成模块 | 内存仓库与检索规则；默认入口不装配 | 持久化、提取、用户入口和验收 |
| RAG | 未完成模块 | 内存文档/切块与关键词基线；默认入口不装配 | 摄取、索引、引用和用户验收 |
| Run Storage | 业务存储可用 | SQLite Run/Step round-trip，显式 Repository 注入 | 官方 Checkpointer，不能将现有 Run 存储当作恢复能力 |
| Observability | 基础 | 框架节点进度，现有事件接收器基线 | 完整审计与界面事件协议 |

## 本次重构的实际边界

默认链路：CLI → Application → 官方 LangChain Agent / LangGraph → StructuredTool
→ 官方 MCP SDK → 独立 Workstation 服务 → ToolExecutor → 文件工具。

旧 `Agent`、`AgentState`、`RunLimits`、`ModelBackend`、`ModelReply`、兼容导出和注册表兼容 invoke 已移除。
工具执行器是工作站安全规则，不承担 Agent 决策；直接工具命令的两节点 LangGraph 流程继续作为独立工具入口。
原有 Agent 和存储测试已迁移到官方图，没有为兼容旧 API 保留第二条运行链。

接口验收使用本机 OpenAI 兼容 HTTP 夹具，不能据此声称已验证外部模型效果。
本阶段没有用户界面、持久化对话、审批恢复或完成的 Memory/RAG。

阅读顺序：[框架教学](framework-tutorial.md) → [运行机制](agent.md) →
[工具模块](tools.md) → [本机服务](local-service.md)。

## 代码注释

项目注释统一采用 tools 模块的简短中文风格：说明类、方法和字段的用途，不加入示例，保留必要安全约束。
本次只调整说明文字，模块能力和执行逻辑不变。
