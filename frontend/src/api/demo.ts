import type { AgentGateway, Approvals, Conversation, Decisions, Message, Page, Run } from './contracts';
import { ApiError } from './http';

const now = () => new Date().toISOString();
const id = (prefix: string) => `${prefix}_${crypto.randomUUID().replaceAll('-', '')}`;

/** 仅在用户选择演示时启用，所有数据仅在当前页面内存中，不发送请求。 */
export class DemoGateway implements AgentGateway {
  readonly demo = true;
  private chats: Conversation[] = [];
  private history: Message[] = [];
  private records: Run[] = [];
  private pending: Record<string, Approvals> = {};
  private position = 0;
  private finishing: { run: Run; task: string; at: number } | null = null;

  constructor() {
    const first = this.newChat('前端工作台设计');
    this.push(first.id, 'user', '我们已经补齐后端接口，接下来怎么设计前端页面？', 'demo_design');
    this.push(first.id, 'assistant', `可以围绕一条完整的工作路径展开：**发起任务 → 查看进度 → 处理审批 → 获得结果**。

### 把会话放在中心

左侧管理会话，中间专注阅读与输入。运行信息放在右侧，需要时展开，保持对话的连续性。

| 区域 | 主要内容 |
| --- | --- |
| 会话列表 | 新建、搜索、更名与删除 |
| 对话区 | 历史消息、Markdown、编辑与续聊 |
| 运行侧栏 | 任务项、执行步骤与待审批操作 |

### 保持清楚的模块边界

前端单独放在 \`frontend/\`，通过现有 HTTP 接口访问应用用例。页面不直接依赖数据库或 Agent 内部状态。

\`会话\` 保存持续的对话；\`运行\` 对应一次任务执行。一个会话可以包含多次运行。

下一步可以先打通新建会话、提交问题与查看结果，再接入历史修改和审批恢复。`, 'demo_design');
    this.records.push({
      id: 'demo_design', session_id: first.id, status: 'completed', node: 'model',
      answer: '演示答复', error: null, updated_at: now(),
      task_status: {
        goal: '设计以会话为中心的前端工作台', current_request: '梳理布局、交互和模块边界',
        constraints: ['沿用现有后端接口'], criteria: ['会话、运行状态与审批入口清楚'],
        items: [
          { id: 'layout', text: '明确页面布局与阅读层级', status: 'pending_confirmation' },
          { id: 'api', text: '对齐会话与运行接口', status: 'pending_confirmation' },
          { id: 'ui', text: '实现前端交互页面', status: 'pending' },
        ], next_action: '实现会话与消息的交互页面', blockers: [],
      },
      steps: [
        { id: 's1', name: 'model', kind: 'model', status: 'completed', error: null, started_at: now(), finished_at: now() },
        { id: 's2', name: 'context_usage', kind: 'retrieval', status: 'completed', error: null, started_at: now(), finished_at: now() },
      ],
    });
    const approval = this.newChat('把项目文档加入知识库');
    this.push(approval.id, 'user', '把 README.md 加入知识库。', 'demo_review');
    this.records.push({ id: 'demo_review', session_id: approval.id, status: 'waiting_approval', node: 'tools',
      answer: null, error: '工具需要审批', task_status: null, steps: [], updated_at: now() });
    this.pending[approval.id] = { demo_interrupt: { name: 'ingest_document', arguments: { path: 'README.md' }, fingerprint: 'demo-only' } };
    this.newChat('梳理记忆和会话的区别');
    this.chats = [first, approval, ...this.chats.filter((chat) => chat.id !== first.id && chat.id !== approval.id)];
  }

  private newChat(title: string): Conversation {
    const chat = { id: id('session'), title, revision: 0, created_at: now(), updated_at: now() };
    this.chats.unshift(chat);
    return chat;
  }
  private find(sessionId: string): Conversation {
    const chat = this.chats.find((entry) => entry.id === sessionId);
    if (!chat) throw new ApiError(404, 'not_found', '会话不存在。');
    return chat;
  }
  private check(sessionId: string, revision: number): Conversation {
    const chat = this.find(sessionId);
    if (chat.revision !== revision) throw new ApiError(409, 'revision_conflict', '会话已变化，请重新读取后修改。');
    if (this.records.some((run) => run.session_id === sessionId && ['thinking', 'waiting_approval'].includes(run.status))) {
      throw new ApiError(409, 'session_busy', '请先结束运行或处理审批。');
    }
    return chat;
  }
  private touch(chat: Conversation): void { chat.revision++; chat.updated_at = now(); }
  private push(sessionId: string, role: Message['role'], content: string, runId: string | null): Message {
    const message: Message = { id: id('message'), session_id: sessionId, position: ++this.position,
      role, content, run_id: runId, created_at: now(), updated_at: now() };
    this.history.push(message);
    this.touch(this.find(sessionId));
    return message;
  }
  private page<T>(entries: T[], offset: number): Page<T> {
    return structuredClone({ items: entries.slice(offset, offset + 100), next_offset: entries.length > offset + 100 ? offset + 100 : null });
  }
  async sessions(offset = 0) { return this.page(this.chats, offset); }
  async session(sessionId: string) { return structuredClone(this.find(sessionId)); }
  async create(title: string) { return structuredClone(this.newChat(title)); }
  async rename(sessionId: string, title: string, revision: number) {
    const chat = this.find(sessionId);
    if (chat.revision !== revision) throw new ApiError(409, 'revision_conflict', '会话已变化，请重新读取后修改。');
    chat.title = title; this.touch(chat); return structuredClone(chat);
  }
  async deleteSession(sessionId: string, revision: number) {
    this.check(sessionId, revision);
    this.chats = this.chats.filter((chat) => chat.id !== sessionId);
    this.records = this.records.filter((run) => run.session_id !== sessionId);
    this.history = this.history.filter((message) => message.session_id !== sessionId);
  }
  async messages(sessionId: string, offset = 0) { this.find(sessionId); return this.page(this.history.filter((message) => message.session_id === sessionId), offset); }
  async addMessage(sessionId: string, content: string, revision: number) { this.check(sessionId, revision); return structuredClone(this.push(sessionId, 'user', content, null)); }
  async editMessage(sessionId: string, messageId: string, content: string, revision: number) {
    this.check(sessionId, revision);
    const message = this.history.find((entry) => entry.session_id === sessionId && entry.id === messageId);
    if (!message) throw new ApiError(404, 'not_found', '消息不存在。');
    if (message.role !== 'user') throw new ApiError(409, 'message_read_only', '只能编辑用户消息。');
    this.history = this.history.filter((entry) => entry.session_id !== sessionId || entry.position <= message.position);
    message.content = content; message.run_id = null; message.updated_at = now(); this.touch(this.find(sessionId));
    return structuredClone(message);
  }
  async deleteMessage(sessionId: string, messageId: string, revision: number) {
    this.check(sessionId, revision);
    const message = this.history.find((entry) => entry.session_id === sessionId && entry.id === messageId);
    if (!message) throw new ApiError(404, 'not_found', '消息不存在。');
    this.history = this.history.filter((entry) => entry.session_id !== sessionId || entry.position < message.position);
    this.touch(this.find(sessionId));
  }
  async runs(offset = 0, _signal?: AbortSignal, sessionId?: string) {
    if (this.finishing && Date.now() >= this.finishing.at) {
      const { run } = this.finishing;
      const answer = '这是交互演示，任务没有发送到模型。\n\n点击左下角的**连接服务**，即可使用真实后端执行任务。';
      run.status = 'completed'; run.answer = answer; run.updated_at = now();
      this.push(run.session_id, 'assistant', answer, run.id); this.finishing = null;
    }
    return this.page(this.records.filter((run) => !sessionId || run.session_id === sessionId)
      .sort((a, b) => b.updated_at.localeCompare(a.updated_at)), offset);
  }
  async run(runId: string) {
    const run = this.records.find((entry) => entry.id === runId);
    if (!run) throw new ApiError(404, 'not_found', '运行记录不存在。');
    return structuredClone(run);
  }
  async deleteRun(runId: string) {
    const run = await this.run(runId);
    if (!['completed', 'failed', 'cancelled', 'limit_reached', 'interrupted'].includes(run.status)) {
      throw new ApiError(409, 'session_busy', '请先结束运行或处理审批。');
    }
    this.records = this.records.filter((entry) => entry.id !== runId);
  }
  async submit(sessionId: string, task: string) {
    if (this.finishing) throw new ApiError(409, 'agent_busy', 'Agent 正在执行另一项任务。');
    this.check(sessionId, this.find(sessionId).revision);
    const run: Run = { id: id('run'), session_id: sessionId, status: 'thinking', node: 'model',
      task_status: null, answer: null, error: null, steps: [], updated_at: now() };
    this.push(sessionId, 'user', task, run.id); this.records.unshift(run);
    this.finishing = { run, task, at: Date.now() + 2500 }; return structuredClone(run);
  }
  async cancel(runId: string) {
    if (this.finishing?.run.id === runId) { this.finishing.run.status = 'cancelled'; this.finishing = null; }
  }
  async approvals(sessionId: string) { return structuredClone(this.pending[sessionId] ?? {}); }
  async resume(sessionId: string, decisions: Decisions) {
    const pending = this.pending[sessionId] ?? {};
    if (Object.keys(pending).length === 0 || Object.keys(pending).some((key) => !decisions[key])) throw new ApiError(409, 'invalid_state', '请为全部操作选择决定。');
    const run = this.records.find((entry) => entry.session_id === sessionId && entry.status === 'waiting_approval');
    if (!run) throw new ApiError(409, 'invalid_state', '当前没有待审批任务。');
    this.pending[sessionId] = {}; run.status = 'completed'; run.error = null; run.updated_at = now();
    run.answer = '演示审批已处理。真实连接下，Agent 会按你选择的决定继续执行。';
    this.push(sessionId, 'assistant', run.answer, run.id); return structuredClone(run);
  }
}
