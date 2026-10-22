import { useEffect, useState } from 'react';
import type { AgentGateway, Approvals, Conversation, Message, Run } from './api/contracts';
import { isActive } from './api/contracts';
import { ApiError } from './api/http';

export const errorText = (error: unknown): string => error instanceof ApiError ? error.message : '操作未完成，请重试。';

/** 只轮询读取接口；切换会话、连接或卸载时取消请求，写入始终由明确的用户动作触发。 */
export function useWorkspace(gateway: AgentGateway | null) {
  const [sessions, setSessions] = useState<Conversation[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [current, setCurrent] = useState<Conversation | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [runs, setRuns] = useState<Run[]>([]);
  const [approvals, setApprovals] = useState<Approvals>({});
  const [error, setError] = useState('');
  const [healthy, setHealthy] = useState(false);
  const [loading, setLoading] = useState(false);
  const [pages, setPages] = useState(1);
  const [hasMore, setHasMore] = useState(false);
  const [pulse, setPulse] = useState(0);

  useEffect(() => {
    setSessions([]); setSelectedId(null); setCurrent(null); setMessages([]); setRuns([]);
    setApprovals({}); setError(''); setHealthy(false); setPages(1);
  }, [gateway]);

  useEffect(() => {
    if (!gateway) return;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    let previousRevision = -1;
    const { signal } = controller;
    setLoading(true);

    async function read(): Promise<void> {
      if (!gateway) return;
      try {
        const loaded: Conversation[] = [];
        let offset: number | null = 0;
        for (let index = 0; index < pages && offset !== null; index++) {
          const page = await gateway.sessions(offset, signal);
          loaded.push(...page.items); offset = page.next_offset;
        }
        const nextRuns = await gateway.runs(signal);
        if (signal.aborted) return;
        setSessions(loaded); setHasMore(offset !== null); setRuns(nextRuns);
        if (selectedId === null && loaded[0]) {
          setSelectedId(loaded[0].id);
        } else if (selectedId) {
          const conversation = await gateway.session(selectedId, signal);
          if (conversation.revision !== previousRevision) {
            const history: Message[] = [];
            let next: number | null = 0;
            while (next !== null) {
              if (next > 100_000) throw new ApiError(0, 'history_limit', '历史消息超出可读取范围，请使用新的会话继续。');
              const page = await gateway.messages(selectedId, next, signal);
              history.push(...page.items); next = page.next_offset;
            }
            if (signal.aborted) return;
            setMessages(history); previousRevision = conversation.revision;
          }
          if (signal.aborted) return;
          // 消息与其版本在同一批更新，避免用户看到新消息却拿旧版本提交修改。
          setCurrent(conversation);
          if (!nextRuns.some(isActive)) {
            const pending = await gateway.approvals(selectedId, signal);
            if (signal.aborted) return;
            setApprovals(pending);
          }
        }
        if (!signal.aborted) { setHealthy(true); setError(''); }
      } catch (failure) {
        if (signal.aborted) return;
        if (failure instanceof ApiError && failure.status === 404 && selectedId) {
          setSelectedId(null); setCurrent(null); setMessages([]); setApprovals({});
        } else {
          setError(errorText(failure));
          if (!(failure instanceof ApiError && failure.status === 409)) setHealthy(false);
        }
      } finally {
        if (!signal.aborted) { setLoading(false); timer = setTimeout(() => { void read(); }, 1800); }
      }
    }
    void read();
    return () => { controller.abort(); clearTimeout(timer); };
  }, [gateway, selectedId, pages, pulse]);

  function select(id: string | null): void {
    setSelectedId(id); setCurrent(null); setMessages([]); setApprovals({});
  }
  function refresh(): void { setPulse((value) => value + 1); }
  function acceptRun(run: Run): void { setRuns((value) => [run, ...value.filter((item) => item.id !== run.id)]); }
  return { sessions, selectedId, current, messages, runs, approvals, error, healthy, loading,
    hasMore, select, refresh, acceptRun, moreSessions: () => setPages((value) => value + 1) };
}
