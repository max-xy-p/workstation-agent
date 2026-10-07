# 项目文档

这里是 Workstation Agent 的文档入口。

| 文档 | 面向对象 | 内容 |
| --- | --- | --- |
| [项目 README](../README.md) | 使用者 | 项目定位、启动命令、当前能力 |
| [框架教学](framework-tutorial.md) | 学习者与开发人员 | 官方 Agent、模型、工具、运行和验收 |
| [架构设计](architecture.md) | 架构与开发人员 | 分层原则、目标架构、记忆和 RAG 设计 |
| [工具模块](tools.md) | 使用者与开发人员 | LangGraph 工具调用入口、参数、权限及验收 |
| [本机服务](local-service.md) | 使用者与 Agent 开发人员 | 本机部署、MCP 通信、认证和独立进程验收 |
| [功能模块](modules.md) | 产品与开发人员 | 模块职责、完成状态、依赖关系和下一步 |
| [Agent 运行机制](agent.md) | Agent 开发人员 | 状态机、执行序列、错误和扩展点 |
| [开发指南](development.md) | 贡献者 | 环境、命令、添加 Provider/Tool/Repository 的流程 |
| [开发约定](../AGENTS.md) | AI Agent 与贡献者 | 仓库边界、编码规则、测试和完成定义 |

## 文档维护规则

- `README.md` 是简洁的用户入口，不承载完整设计细节。
- `architecture.md` 记录长期稳定的架构原则和目标，不作为任务清单。
- `modules.md` 是当前实现状态的事实来源；代码变化后需要同步更新。
- `agent.md` 描述运行时行为，任何状态或事件语义变化都要同步修改。
- 接口和配置的细节应靠近代码，并通过 docstring 和测试保持准确。
