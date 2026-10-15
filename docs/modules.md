# 功能模块清单

> 更新日期：2026-10-15。项目已进行 0.2.0 破坏性框架重构，不保留旧 Agent API。

| 模块 | 当前状态 | 已实现范围 | 后续范围 |
| --- | --- | --- | --- |
| Agent Core | 官方框架运行链可用 | create_agent / LangGraph，官方模型和工具调用限制、可选官方 SQLite 会话、摘要/裁剪；移除手写循环 | 审批恢复、整次任务超时 |
| Model | OpenAI 兼容适配器可用 | 官方 ChatOpenAI，model/base_url/API key/超时配置，HTTP 接口验收 | 外部模型实际能力验收、用量统计 |
| Tools | 只读及 RAG 受控写入可用 | StructuredTool、MCP/同进程、校验权限、RAG 摄取/删除与模型出站授权 | 通用文件写入、结构化内容分页 |
| Application | 单次任务入口可用 | 异步 run_task、Context 组装、节点进度、结果状态、异常脱敏 | 取消 API、审批接口 |
| Interfaces | CLI 与本机 MCP 可用 | 自然语言 run、工具发现、serve、Windows 启动脚本 | 用户界面、独立 Agent HTTP 服务 |
| Safety | 基础 | 四级权限，默认只读，拒绝时无工具副作用 | 可恢复审批、敏感内容过滤 |
| Domain | 业务对象可用 | Run/Step 结果摘要，与框架内部消息和状态分离 | 显式状态转换规则 |
| Context | 证据组装可用 | 唯一组装入口、原文引用、完整证据预算与实际来源记录 | 精确 tokenizer 预算、更多来源格式 |
| Memory | 文本记忆闭环可用 | SQLite、受控 CRUD、范围隔离、版本/过期/遗忘、中文 BM25、Context 瞬时注入、CLI/Agent 与默认关闭的 LangMem 提取 | 外部模型质量、同义召回、已验证执行经验来源；见 [使用与验收](memory.md) |
| RAG | 第一阶段可用，已采用框架组件 | 原生 Document 处理链、成熟 Markdown 解析器与官方分块/检索组合；格式 3 快照、按 ID 关联的向量、上下文与引用、授权入口；移除旧对象和仓库兼容接口 | 真实模型质量与参数校准、更多格式、规模优化 |
| Run Storage | 业务存储可用 | SQLite Run/Step round-trip，显式 Repository 注入 | 框架会话另用官方 Checkpointer，业务 Run 存储不承担恢复 |
| Observability | 基础 | 框架节点进度，现有事件接收器基线 | 完整审计与界面事件协议 |

## 本次重构的实际边界

默认链路：CLI → Application → 官方 LangChain Agent / LangGraph → StructuredTool
→ 官方 MCP SDK → 独立 Workstation 服务 → ToolExecutor → 文件工具。

旧 `Agent`、`AgentState`、`RunLimits`、`ModelBackend`、`ModelReply`、兼容导出和注册表兼容 invoke 已移除。
工具执行器是工作站安全规则，不承担 Agent 决策；直接工具命令的两节点 LangGraph 流程继续作为独立工具入口。
原有 Agent 和存储测试已迁移到官方图，没有为兼容旧 API 保留第二条运行链。

接口验收使用本机 OpenAI 兼容 HTTP 夹具，不能据此声称已验证外部模型效果。
已支持显式会话续聊和文本 Memory；没有用户界面或审批恢复。默认仍不持久化会话，自动提取默认关闭。
RAG 第一阶段已交付，外部模型质量尚需真实文档验收，详见 [RAG 使用与验收](rag.md)。

阅读顺序：[框架教学](framework-tutorial.md) → [运行机制](agent.md) →
[工具模块](tools.md) → [本机服务](local-service.md)。

## 代码注释

代码注释、docstring 和 Markdown 必须通俗易懂，遵守
[开发约定中的强制写作规范](../AGENTS.md#注释与-markdown-写作规范强制)。
先说明对象和用途，再解释行为、原因和限制；需要时用简短示例说明，不以简短为由省略关键信息。
