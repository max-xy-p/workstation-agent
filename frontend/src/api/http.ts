import { z } from 'zod';
import {
  approvalsSchema, conversationSchema, messageSchema, pageSchema, runSchema,
  type AgentGateway, type Decisions,
} from './contracts';

export class ApiError extends Error {
  constructor(readonly status: number, readonly code: string, message: string) {
    super(message);
  }
}

const errorSchema = z.object({ error: z.string(), message: z.string() });
const successSchema = z.unknown();
const part = encodeURIComponent;

/** 令牌仅保存在本实例内，不进入 URL、浏览器持久存储或构建环境变量。 */
export class HttpGateway implements AgentGateway {
  readonly demo = false;
  constructor(private readonly token: string) {}

  private async request<T>(path: string, schema: z.ZodType<T>, method = 'GET', body?: unknown, signal?: AbortSignal): Promise<T> {
    const encoded = body === undefined ? undefined : JSON.stringify(body);
    if (encoded && new TextEncoder().encode(encoded).length > 65_536) {
      throw new ApiError(413, 'body_too_large', '内容过长，请缩短后再提交。');
    }
    let response: Response;
    try {
      response = await fetch(`/api${path}`, {
        method, signal, cache: 'no-store',
        headers: { Authorization: `Bearer ${this.token}`, ...(encoded ? { 'Content-Type': 'application/json' } : {}) },
        body: encoded,
      });
    } catch (error) {
      if (error instanceof DOMException && error.name === 'AbortError') throw error;
      throw new ApiError(0, 'network_error', '无法连接 Agent，请检查本机服务是否已启动。');
    }
    let value: unknown;
    try { value = await response.json(); } catch {
      throw new ApiError(response.status, 'invalid_response', '服务未返回有效结果，请检查服务连接。');
    }
    if (!response.ok) {
      const parsed = errorSchema.safeParse(value);
      throw new ApiError(response.status, parsed.success ? parsed.data.error : 'request_failed',
        response.status === 401 ? '访问令牌无效，请重新连接服务。' :
          parsed.success ? parsed.data.message : '请求未完成，请稍后重试。');
    }
    const parsed = schema.safeParse(value);
    if (!parsed.success) throw new ApiError(0, 'invalid_response', '服务返回的数据格式不匹配，请检查前后端版本。');
    return parsed.data;
  }

  sessions(offset = 0, signal?: AbortSignal) { return this.request(`/sessions?limit=100&offset=${offset}`, pageSchema(conversationSchema), 'GET', undefined, signal); }
  session(id: string, signal?: AbortSignal) { return this.request(`/sessions/${part(id)}`, conversationSchema, 'GET', undefined, signal); }
  create(title: string) { return this.request('/sessions', conversationSchema, 'POST', { title }); }
  rename(id: string, title: string, revision: number) { return this.request(`/sessions/${part(id)}`, conversationSchema, 'PATCH', { title, revision }); }
  async deleteSession(id: string, revision: number) { await this.request(`/sessions/${part(id)}?revision=${revision}`, successSchema, 'DELETE'); }
  messages(id: string, offset = 0, signal?: AbortSignal) { return this.request(`/sessions/${part(id)}/messages?limit=100&offset=${offset}`, pageSchema(messageSchema), 'GET', undefined, signal); }
  addMessage(id: string, content: string, revision: number) { return this.request(`/sessions/${part(id)}/messages`, messageSchema, 'POST', { content, revision }); }
  editMessage(id: string, messageId: string, content: string, revision: number) { return this.request(`/sessions/${part(id)}/messages/${part(messageId)}`, messageSchema, 'PATCH', { content, revision }); }
  async deleteMessage(id: string, messageId: string, revision: number) { await this.request(`/sessions/${part(id)}/messages/${part(messageId)}?revision=${revision}`, successSchema, 'DELETE'); }
  runs(offset = 0, signal?: AbortSignal, sessionId?: string) {
    return this.request(`/runs?limit=50&offset=${offset}${sessionId ? `&session_id=${part(sessionId)}` : ''}`, pageSchema(runSchema), 'GET', undefined, signal);
  }
  run(id: string, signal?: AbortSignal) { return this.request(`/runs/${part(id)}`, runSchema, 'GET', undefined, signal); }
  async deleteRun(id: string) { await this.request(`/runs/${part(id)}`, successSchema, 'DELETE'); }
  submit(id: string, task: string) { return this.request('/runs', runSchema, 'POST', { session_id: id, task }); }
  async cancel(id: string) { await this.request(`/runs/${part(id)}/cancel`, successSchema, 'POST'); }
  approvals(id: string, signal?: AbortSignal) { return this.request(`/sessions/${part(id)}/approvals`, approvalsSchema, 'GET', undefined, signal); }
  resume(id: string, decisions: Decisions) { return this.request(`/sessions/${part(id)}/resume`, runSchema, 'POST', { decisions }); }
}
