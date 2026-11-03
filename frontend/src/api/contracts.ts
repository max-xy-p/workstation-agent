import { z } from 'zod';

const date = z.iso.datetime({ offset: true });
export const conversationSchema = z.object({
  id: z.string(), title: z.string(), revision: z.number().int().nonnegative(),
  created_at: date, updated_at: date,
});
export const sourceSchema = z.object({
  id: z.string().regex(/^src_[a-f0-9]{24}$/), document_id: z.string(), checksum: z.string(),
  source_uri: z.string(), title: z.string(), content: z.string(), citation: z.string(),
  line_start: z.number().int().positive(), line_end: z.number().int().positive(),
  source_version: z.string().nullable(),
  original_url: z.url().refine((value) => {
    const url = new URL(value);
    return ['https:', 'http:'].includes(url.protocol) && !url.username && !url.password && !url.search && !url.hash;
  }).nullable(),
});
export const messageSchema = z.object({
  id: z.string(), session_id: z.string(), position: z.number().int(),
  role: z.enum(['user', 'assistant']), content: z.string(), run_id: z.string().nullable(),
  created_at: date, updated_at: date,
  sources: z.array(sourceSchema).max(32),
});
export const stepSchema = z.object({
  id: z.string(), name: z.string(), kind: z.enum(['model', 'tool', 'memory', 'retrieval']),
  status: z.enum(['started', 'completed', 'failed', 'denied']),
  error: z.string().nullable(), started_at: date, finished_at: date.nullable(),
});
export const boardSchema = z.object({
  goal: z.string(), current_request: z.string(), constraints: z.array(z.string()),
  criteria: z.array(z.string()), next_action: z.string(), blockers: z.array(z.string()),
  items: z.array(z.object({
    id: z.string(), text: z.string(),
    status: z.enum(['pending', 'in_progress', 'pending_confirmation', 'completed', 'blocked']),
  })),
});
export const runSchema = z.object({
  id: z.string(), session_id: z.string(),
  status: z.enum(['created', 'preparing', 'thinking', 'acting', 'observing', 'waiting_approval',
    'waiting_user', 'completed', 'failed', 'cancelled', 'limit_reached', 'interrupted']),
  node: z.string().nullable(), task_status: boardSchema.nullable(), answer: z.string().nullable(),
  error: z.string().nullable(), steps: z.array(stepSchema), updated_at: date,
});
export const approvalsSchema = z.record(z.string(), z.object({
  name: z.string(), arguments: z.record(z.string(), z.json()), fingerprint: z.string(),
}));
export function pageSchema<T extends z.ZodType>(item: T) {
  return z.object({ items: z.array(item), next_offset: z.number().int().nonnegative().nullable() });
}

export type Conversation = z.infer<typeof conversationSchema>;
export type Message = z.infer<typeof messageSchema>;
export type AnswerSource = z.infer<typeof sourceSchema>;
export type Run = z.infer<typeof runSchema>;
export type Step = z.infer<typeof stepSchema>;
export type Approvals = z.infer<typeof approvalsSchema>;
export type Decisions = Record<string, 'approve' | 'reject'>;
export type Page<T> = { items: T[]; next_offset: number | null };

/** 前端只依赖这些明确操作，真实 HTTP 与显式演示使用同一契约。 */
export interface AgentGateway {
  readonly demo: boolean;
  sessions(offset?: number, signal?: AbortSignal): Promise<Page<Conversation>>;
  session(id: string, signal?: AbortSignal): Promise<Conversation>;
  create(title: string): Promise<Conversation>;
  rename(id: string, title: string, revision: number): Promise<Conversation>;
  deleteSession(id: string, revision: number): Promise<void>;
  messages(id: string, offset?: number, signal?: AbortSignal): Promise<Page<Message>>;
  addMessage(id: string, content: string, revision: number): Promise<Message>;
  editMessage(id: string, messageId: string, content: string, revision: number): Promise<Message>;
  deleteMessage(id: string, messageId: string, revision: number): Promise<void>;
  runs(offset?: number, signal?: AbortSignal, sessionId?: string): Promise<Page<Run>>;
  run(id: string, signal?: AbortSignal): Promise<Run>;
  deleteRun(id: string): Promise<void>;
  submit(id: string, task: string): Promise<Run>;
  cancel(id: string): Promise<void>;
  approvals(id: string, signal?: AbortSignal): Promise<Approvals>;
  resume(id: string, decisions: Decisions): Promise<Run>;
}

export function isActive(run: Run): boolean {
  return ['created', 'preparing', 'thinking', 'acting', 'observing'].includes(run.status);
}
