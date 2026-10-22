import { ChevronRight, LoaderCircle, MessageSquare, PanelLeftClose, Pencil, Plus, Search, Settings2, Trash2, X } from 'lucide-react';
import type { Conversation, Run } from '../api/contracts';
import { Brand } from './Conversation';
import { useState } from 'react';

export function Sidebar({ sessions, selectedId, runs, open, connected, demo, healthy, busy, hasMore,
  onSelect, onCreate, onRename, onDelete, onConnect, onClose, onMore }: {
  sessions: Conversation[]; selectedId: string | null; runs: Run[]; open: boolean;
  connected: boolean; demo: boolean; healthy: boolean; busy: boolean; hasMore: boolean;
  onSelect: (id: string) => void; onCreate: () => void; onRename: (chat: Conversation) => void;
  onDelete: (chat: Conversation) => void; onConnect: () => void; onClose: () => void; onMore: () => void;
}) {
  const [search, setSearch] = useState('');
  const filtered = sessions.filter((session) => session.title.toLocaleLowerCase().includes(search.toLocaleLowerCase()));
  return <aside className={`sidebar ${open ? 'is-open' : ''}`} aria-label="会话导航">
    <div className="sidebar-brand"><Brand /><div><strong>Workstation</strong><span>个人 Agent 工作站</span></div>
      <button className="icon-button mobile-only" onClick={onClose} aria-label="收起会话导航"><PanelLeftClose size={18} /></button>
    </div>
    <button className="new-conversation" onClick={onCreate} disabled={!connected || busy}><Plus size={18} />新建会话</button>
    <div className="search"><Search size={15} /><input aria-label="搜索会话" placeholder="搜索已加载会话" value={search} onChange={(event) => setSearch(event.target.value)} />
      {search && <button className="icon-button" aria-label="清空搜索" onClick={() => setSearch('')}><X size={13} /></button>}</div>
    <div className="sidebar-section-title"><span>最近会话</span><span>{sessions.length}</span></div>
    <nav className="session-list">
      {filtered.map((session) => {
        const waiting = runs.some((run) => run.session_id === session.id && run.status === 'waiting_approval');
        const active = runs.some((run) => run.session_id === session.id && ['thinking', 'preparing', 'acting', 'observing'].includes(run.status));
        return <div className={`session-row ${selectedId === session.id ? 'selected' : ''}`} key={session.id}>
          <button className="session-select" disabled={busy} aria-label={session.title} aria-description={waiting ? '等待审批' : active ? '正在运行' : undefined} aria-current={selectedId === session.id ? 'page' : undefined} onClick={() => { onSelect(session.id); onClose(); }} title={session.title}>
            {active ? <LoaderCircle size={16} className="spin" /> : <MessageSquare size={16} />}
            <span>{session.title}</span>{waiting && <span className="waiting-dot" aria-label="等待审批" />}
          </button>
          <div className="session-actions"><button className="icon-button" aria-label={`更名：${session.title}`} disabled={busy} onClick={() => onRename(session)}><Pencil size={13} /></button>
            <button className="icon-button" aria-label={`删除：${session.title}`} disabled={busy || active || waiting} onClick={() => onDelete(session)}><Trash2 size={13} /></button></div>
        </div>;
      })}
      {filtered.length === 0 && <p className="sidebar-empty">{search ? '没有匹配的会话' : connected ? '新建一个会话，开始工作。' : '连接服务后显示你的会话。'}</p>}
      {hasMore && <button className="load-sessions" onClick={onMore}>加载更多会话<ChevronRight size={13} /></button>}
    </nav>
    <div className="sidebar-bottom"><div className="local-label"><span className="workspace-symbol">W</span><div><strong>本机工作区</strong><span>{demo ? '演示数据，仅在当前页面保存' : '会话由你的 Agent 服务保存'}</span></div></div>
      <button className="connection-button" onClick={onConnect} disabled={busy}><span className={`connection-dot ${connected && healthy ? 'online' : ''}`} /><span>{demo ? '演示模式' : connected && healthy ? '服务已连接' : connected ? '连接暂不可用' : '连接服务'}</span><Settings2 size={16} /></button>
    </div>
  </aside>;
}
