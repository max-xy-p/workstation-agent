import { lazy, Suspense, useEffect, useRef, useState, type FormEvent } from 'react';
import { ArrowDown, ArrowUp, Check, ChevronUp, Copy, FileText, FolderSearch, LoaderCircle, MessageSquare, Pencil, Plus, ShieldCheck, Square, Trash2 } from 'lucide-react';
import type { Message, Run } from '../api/contracts';
import { isActive } from '../api/contracts';

const MarkdownBody = lazy(() => import('./MarkdownBody'));

export function Brand({ small = false }: { small?: boolean }) {
  return <span className={`brand-mark ${small ? 'small' : ''}`} aria-hidden="true"><span /><span /><span /></span>;
}

export function ConversationView({ sessionId, messages, run, locked, loading, busy, connected, otherRunning, approvalCount,
  onSend, onCancel, onEdit, onDelete, onBackground, onConnect, onDemo }: {
  sessionId: string | null; messages: Message[]; run: Run | undefined; locked: boolean; loading: boolean;
  busy: boolean; connected: boolean; otherRunning: boolean; approvalCount: number;
  onSend: (task: string) => Promise<boolean>; onCancel: () => void;
  onEdit: (message: Message) => void; onDelete: (message: Message) => void;
  onBackground: () => void; onConnect: () => void; onDemo: () => void;
}) {
  const [draft, setDraft] = useState('');
  const [visibleCount, setVisibleCount] = useState(50);
  const [copied, setCopied] = useState('');
  const [copyError, setCopyError] = useState('');
  const [away, setAway] = useState(false);
  const scroll = useRef<HTMLDivElement>(null);
  const textarea = useRef<HTMLTextAreaElement>(null);
  const drafts = useRef(new Map<string, string>());
  const draftKey = sessionId ?? 'new';
  const keyRef = useRef(draftKey);
  useEffect(() => {
    keyRef.current = draftKey; setDraft(drafts.current.get(draftKey) ?? '');
    setVisibleCount(50); setAway(false);
  }, [draftKey]);
  useEffect(() => {
    if (!away) scroll.current?.scrollTo({ top: scroll.current.scrollHeight, behavior: 'instant' });
  }, [messages, away, run?.status]);
  useEffect(() => { const timeout = setTimeout(() => setCopied(''), 2000); return () => clearTimeout(timeout); }, [copied]);
  useEffect(() => {
    if (textarea.current) { textarea.current.style.height = 'auto'; textarea.current.style.height = `${Math.min(textarea.current.scrollHeight, 180)}px`; }
  }, [draft]);
  const active = run && isActive(run);
  const canSend = connected && !loading && !busy && !locked && !otherRunning && draft.trim().length > 0;

  async function send(event?: FormEvent): Promise<void> {
    event?.preventDefault();
    if (!canSend) return;
    const key = keyRef.current;
    const success = await onSend(draft.trim());
    if (success) {
      drafts.current.delete(key);
      if (keyRef.current === key) setDraft('');
      setAway(false);
    }
  }
  async function copy(message: Message): Promise<void> {
    try { await navigator.clipboard.writeText(message.content); setCopied(message.id); setCopyError(''); }
    catch { setCopyError('浏览器未允许复制，请选中文字后复制。'); }
  }
  function fill(value: string): void { setDraft(value); drafts.current.set(draftKey, value); textarea.current?.focus(); }

  return <div className="conversation-view">
    <div ref={scroll} className="conversation-scroll" onScroll={() => {
      const element = scroll.current;
      if (element) setAway(element.scrollHeight - element.scrollTop - element.clientHeight > 100);
    }}>
      {messages.length === 0 ? <div className="welcome">
        <Brand /><h1>从一个具体任务开始</h1><p>讨论一个想法，查阅工作区，或接着完成手头的事。</p>
        {loading ? <p className="muted loading-line"><LoaderCircle size={16} className="spin" />正在读取会话…</p> :
          <div className="suggestions">
            <button onClick={() => fill('梳理当前项目的目录结构，说明各模块的职责。')}><FolderSearch size={18} /><span>梳理当前项目<small>了解代码结构与模块分工</small></span></button>
            <button onClick={() => fill('查阅项目文档，说明 Agent 的工具权限如何检查。')}><FileText size={18} /><span>从文档中找答案<small>带着来源理解已有实现</small></span></button>
            <button onClick={() => fill('我们继续讨论前端工作台的交互设计。')}><MessageSquare size={18} /><span>一起完善一个想法<small>把问题拆开，逐步推进</small></span></button>
          </div>}
        {!connected && <div className="welcome-connect"><button className="primary" onClick={onConnect}>连接本机 Agent</button><button className="text-button" onClick={onDemo}>先看看页面演示</button></div>}
      </div> : <div className="message-stream">
        {messages.length > visibleCount && <button className="history-more" onClick={() => { setVisibleCount((value) => value + 50); setAway(true); }}><ChevronUp size={14} />显示更早的消息（{messages.length - visibleCount}）</button>}
        <div className="conversation-start"><span />会话记录<span /></div>
        {messages.slice(-visibleCount).map((message) => <article key={message.id} className={`message ${message.role}`}>
          <div className="message-avatar">{message.role === 'assistant' ? <Brand small /> : <span>你</span>}</div>
          <div className="message-body"><div className="message-heading"><strong>{message.role === 'assistant' ? 'Workstation' : '你'}</strong><time dateTime={message.created_at}>{new Date(message.created_at).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })}</time></div>
            {message.role === 'assistant' ? <Suspense fallback={<div className="user-content">{message.content}</div>}><MarkdownBody content={message.content} /></Suspense> : <div className="user-content">{message.content}</div>}
            <div className="message-actions"><button className="icon-button" title="复制消息" aria-label="复制消息" onClick={() => { void copy(message); }}>{copied === message.id ? <Check size={14} /> : <Copy size={14} />}</button>
              {message.role === 'user' && <button className="icon-button" title="编辑消息" aria-label="编辑消息" disabled={locked || busy || !connected} onClick={() => onEdit(message)}><Pencil size={14} /></button>}
              <button className="icon-button" title="删除此条及后续消息" aria-label="删除消息" disabled={locked || busy || !connected} onClick={() => onDelete(message)}><Trash2 size={14} /></button>
            </div>
          </div>
        </article>)}
        {active && <div className="running-message"><Brand small /><span><LoaderCircle size={14} className="spin" />Agent 正在处理任务…</span></div>}
        {approvalCount > 0 && <div className="approval-notice"><ShieldCheck size={17} /><span>有 {approvalCount} 项操作等待决定，请在运行详情中核对。</span></div>}
      </div>}
    </div>
    {away && messages.length > 0 && <button className="jump-bottom" onClick={() => { setAway(false); scroll.current?.scrollTo({ top: scroll.current.scrollHeight, behavior: 'smooth' }); }}><ArrowDown size={16} />回到最新</button>}
    <div className="composer-wrap">
      {copyError && <p className="inline-error" role="alert">{copyError}</p>}
      <form className={`composer ${locked ? 'locked' : ''}`} onSubmit={(event) => { void send(event); }}>
        <textarea ref={textarea} aria-label="任务内容" placeholder={approvalCount ? '请先处理待审批操作，再继续会话' : '描述任务，或补充你的想法…'}
          value={draft} maxLength={32000} rows={2} disabled={busy || locked}
          onChange={(event) => fill(event.target.value)} onKeyDown={(event) => {
            if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) { event.preventDefault(); void send(); }
          }} />
        <div className="composer-toolbar"><button type="button" className="background-button" disabled={!sessionId || !connected || busy || locked} onClick={onBackground} title="仅保存背景文本，不运行 Agent"><Plus size={16} />补充背景</button>
          <div className="composer-end"><span>{otherRunning ? '另一会话正在运行' : 'Enter 发送'}</span>
            {active ? <button type="button" className="send-button stop" aria-label="停止运行" disabled={busy} onClick={onCancel}><Square size={14} fill="currentColor" /></button> :
              <button className="send-button" aria-label="发送任务" disabled={!canSend} type="submit">{busy ? <LoaderCircle size={19} className="spin" /> : <ArrowUp size={20} />}</button>}
          </div></div>
      </form>
      <p className="composer-note">Shift + Enter 换行<span />重要结果请结合来源与执行状态核对</p>
    </div>
  </div>;
}
