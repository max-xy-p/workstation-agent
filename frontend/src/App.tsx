import { useEffect, useState, type FormEvent } from 'react';
import { AlertCircle, ChevronRight, FlaskConical, LoaderCircle, Menu, PanelRight, Pencil, Plug, Trash2 } from 'lucide-react';
import { DemoGateway } from './api/demo';
import { HttpGateway } from './api/http';
import { isActive, type AgentGateway, type Conversation, type Message } from './api/contracts';
import { useWorkspace, errorText } from './useWorkspace';
import { ConversationView } from './components/Conversation';
import { Inspector, statusName } from './components/Inspector';
import { Sidebar } from './components/Sidebar';
import { Modal } from './components/Modal';

type Editor = { kind: 'delete-run'; session: Conversation; runId: string }
  | { kind: 'rename' | 'delete-session'; session: Conversation }
  | { kind: 'edit-message' | 'delete-message'; session: Conversation; message: Message }
  | { kind: 'background'; session: Conversation };

export default function App() {
  const [gateway, setGateway] = useState<AgentGateway | null>(() => new URLSearchParams(location.search).get('demo') === '1' ? new DemoGateway() : null);
  const workspace = useWorkspace(gateway);
  const [sidebar, setSidebar] = useState(false);
  const [inspector, setInspector] = useState(Boolean(gateway?.demo));
  const [connecting, setConnecting] = useState(false);
  const [editor, setEditor] = useState<Editor | null>(null);
  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState('');
  const [inspectedId, setInspectedId] = useState<string | null>(null);
  useEffect(() => { setInspectedId(null); }, [workspace.selectedId]);
  const current = workspace.current;
  const selectedRun = workspace.runs.filter((run) => run.session_id === workspace.selectedId)
    .sort((a, b) => b.updated_at.localeCompare(a.updated_at))
    .find((run) => isActive(run) || run.status === 'waiting_approval' || workspace.messages.some((message) => message.run_id === run.id));
  const sessionRuns = workspace.runs.filter((run) => run.session_id === workspace.selectedId)
    .sort((a, b) => b.updated_at.localeCompare(a.updated_at));
  const inspectedRun = sessionRuns.find((run) => run.id === inspectedId) ?? selectedRun ?? sessionRuns[0];
  const globalActive = workspace.runs.some(isActive);
  const approvalCount = Object.keys(workspace.approvals).length;
  const locked = Boolean(selectedRun && (isActive(selectedRun) || ['waiting_approval', 'waiting_user'].includes(selectedRun.status))) || approvalCount > 0;
  useEffect(() => { if (approvalCount > 0) setInspector(true); }, [approvalCount]);

  async function mutate(operation: () => Promise<void>): Promise<boolean> {
    setBusy(true); setActionError('');
    try { await operation(); workspace.refresh(); return true; }
    catch (error) { setActionError(errorText(error)); workspace.refresh(); return false; }
    finally { setBusy(false); }
  }
  function openEditor(value: Editor): void { setActionError(''); setEditor(value); }
  function demo(): void { setGateway(new DemoGateway()); setConnecting(false); setInspector(true); setActionError(''); }

  async function send(task: string): Promise<boolean> {
    if (!gateway || (workspace.selectedId !== null && !current)) return false;
    return mutate(async () => {
      let chat = current;
      if (!chat) chat = await gateway.create(task.slice(0, 32));
      else if (chat.title === '新会话' && workspace.messages.length === 0) chat = await gateway.rename(chat.id, task.slice(0, 32), chat.revision);
      const run = await gateway.submit(chat.id, task);
      setInspectedId(null);
      workspace.acceptRun(run);
      if (workspace.selectedId !== run.session_id) workspace.select(run.session_id);
      setInspector(true);
    });
  }

  async function saveEditor(content: string): Promise<void> {
    if (!gateway || !editor) return;
    const operation = editor;
    const success = await mutate(async () => {
      const { id, revision } = operation.session;
      switch (operation.kind) {
        case 'delete-run': await gateway.deleteRun(operation.runId); setInspectedId(null); break;
        case 'rename': await gateway.rename(id, content.trim(), revision); break;
        case 'delete-session':
          await gateway.deleteSession(id, revision);
          if (workspace.selectedId === id) workspace.select(null);
          break;
        case 'background': await gateway.addMessage(id, content, revision); break;
        case 'edit-message': await gateway.editMessage(id, operation.message.id, content, revision); break;
        case 'delete-message': await gateway.deleteMessage(id, operation.message.id, revision); break;
      }
    });
    if (success) setEditor(null);
  }

  return <div className={`workspace ${inspector ? 'with-inspector' : ''}`}>
    {sidebar && <button className="sidebar-scrim" aria-label="关闭导航遮罩" onClick={() => setSidebar(false)} />}
    <Sidebar sessions={workspace.sessions} selectedId={workspace.selectedId} runs={workspace.runs} open={sidebar}
      connected={Boolean(gateway)} demo={Boolean(gateway?.demo)} healthy={workspace.healthy} busy={busy}
      hasMore={workspace.hasMore} onMore={workspace.moreSessions} onClose={() => setSidebar(false)}
      onConnect={() => setConnecting(true)} onSelect={workspace.select}
      onCreate={() => { if (gateway) void mutate(async () => { const chat = await gateway.create('新会话'); workspace.select(chat.id); setSidebar(false); }); }}
      onRename={(session) => openEditor({ kind: 'rename', session })} onDelete={(session) => openEditor({ kind: 'delete-session', session })} />
    <main className="main-pane">
      <header className="main-header"><div className="header-title"><button className="icon-button mobile-only" aria-label="展开会话导航" onClick={() => setSidebar(true)}><Menu size={20} /></button>
        <span className="header-workspace">工作站</span><ChevronRight size={14} className="header-chevron" />
        <h1>{current?.title ?? '新会话'}</h1>
        {current && <button className="icon-button rename-current" aria-label="更改会话标题" disabled={busy} onClick={() => openEditor({ kind: 'rename', session: current })}><Pencil size={14} /></button>}
      </div><div className="header-actions">
        {selectedRun && <span className={`header-status ${selectedRun.status}`}><span className="status-dot" />{statusName[selectedRun.status]}</span>}
        {current && <button className="icon-button" aria-label="删除当前会话" disabled={busy || locked} onClick={() => openEditor({ kind: 'delete-session', session: current })}><Trash2 size={16} /></button>}
        <button className={`icon-button panel-button ${inspector ? 'pressed' : ''}`} aria-label="切换运行详情" aria-expanded={inspector} onClick={() => setInspector((value) => !value)}><PanelRight size={18} /></button>
      </div></header>
      {gateway?.demo && <div className="demo-banner"><FlaskConical size={14} /><span>页面演示，操作不会发送到 Agent</span><button disabled={busy} onClick={() => setConnecting(true)}>连接真实服务</button></div>}
      {(workspace.error || actionError) && !editor && <div className="error-banner" role="alert"><AlertCircle size={16} /><span>{actionError || workspace.error}</span>
        <button className="text-button" onClick={() => { setActionError(''); workspace.refresh(); }}>重新读取</button></div>}
      <ConversationView sessionId={workspace.selectedId} messages={workspace.messages} run={selectedRun} locked={locked}
        loading={workspace.loading} busy={busy} connected={Boolean(gateway && workspace.healthy)}
        otherRunning={globalActive && !Boolean(selectedRun && isActive(selectedRun))} approvalCount={approvalCount}
        onSend={send} onConnect={() => setConnecting(true)} onDemo={demo}
        onCancel={() => { if (gateway && selectedRun) void mutate(() => gateway.cancel(selectedRun.id)); }}
        onEdit={(message) => { if (current) openEditor({ kind: 'edit-message', session: current, message }); }}
        onDelete={(message) => { if (current) openEditor({ kind: 'delete-message', session: current, message }); }}
        onBackground={() => { if (current) openEditor({ kind: 'background', session: current }); }} />
    </main>
    {inspector && <><button className="inspector-scrim" aria-label="关闭运行详情遮罩" onClick={() => setInspector(false)} />
      <Inspector run={inspectedRun} history={sessionRuns} onSelect={setInspectedId}
        hasMore={workspace.hasMoreRuns} onMore={workspace.moreRuns}
        onDelete={() => { if (current && inspectedRun) openEditor({ kind: 'delete-run', session: current, runId: inspectedRun.id }); }} approvals={workspace.approvals} busy={busy || globalActive}
        onClose={() => setInspector(false)} onResume={(decisions) => {
          const id = workspace.selectedId;
          return gateway && id ? mutate(async () => { await gateway.resume(id, decisions); }) : Promise.resolve(false);
        }} /></>}
    {connecting && <ConnectionDialog connected={Boolean(gateway)} busy={busy} onClose={() => setConnecting(false)} onDemo={demo}
      onDisconnect={() => { setGateway(null); setConnecting(false); setEditor(null); setActionError(''); }}
      onConnect={(client) => { setGateway(client); setConnecting(false); setInspector(false); setActionError(''); }} />}
    {editor && <EditorDialog key={`${editor.kind}:${editor.session.id}`} editor={editor} busy={busy} error={actionError}
      onClose={() => { setEditor(null); setActionError(''); }} onSave={saveEditor} />}
  </div>;
}

function ConnectionDialog({ connected, busy, onClose, onConnect, onDisconnect, onDemo }: {
  connected: boolean; busy: boolean; onClose: () => void; onConnect: (gateway: HttpGateway) => void;
  onDisconnect: () => void; onDemo: () => void;
}) {
  const [token, setToken] = useState('');
  const [checking, setChecking] = useState(false);
  const [error, setError] = useState('');
  async function connect(event: FormEvent): Promise<void> {
    event.preventDefault(); setChecking(true); setError('');
    try {
      const client = new HttpGateway(token.trim());
      await client.sessions(); onConnect(client);
    } catch (failure) { setError(errorText(failure)); }
    finally { setChecking(false); }
  }
  return <Modal title="连接本机 Agent" onClose={onClose} busy={checking || busy}>
    <p className="modal-description">启动本机 Agent 服务后，输入该服务的访问令牌。令牌只在当前页面内存中保留，刷新后需重新输入。</p>
    <form onSubmit={(event) => { void connect(event); }}><label className="field-label" htmlFor="token">访问令牌</label>
      <input className="field" type="password" id="token" autoComplete="off" autoFocus value={token} onChange={(event) => setToken(event.target.value)} minLength={32} maxLength={256} required disabled={checking} placeholder="输入 Agent HTTP 令牌" />
      {error && <p className="inline-error" role="alert">{error}</p>}
      <div className="modal-actions"><button type="button" className="text-button" onClick={onDemo} disabled={checking || busy}>使用页面演示</button>
        <button className="primary" disabled={checking || busy || token.trim().length < 32}>{checking ? <LoaderCircle size={16} className="spin" /> : <Plug size={16} />}连接服务</button></div>
    </form>
    {connected && <button className="disconnect" disabled={checking || busy} onClick={onDisconnect}>断开当前连接</button>}
  </Modal>;
}

function EditorDialog({ editor, busy, error, onClose, onSave }: {
  editor: Editor; busy: boolean; error: string; onClose: () => void; onSave: (content: string) => Promise<void>;
}) {
  const [content, setContent] = useState(editor.kind === 'rename' ? editor.session.title : editor.kind === 'edit-message' ? editor.message.content : '');
  const deleting = editor.kind === 'delete-session' || editor.kind === 'delete-message' || editor.kind === 'delete-run';
  const title = { rename: '更改会话标题', 'delete-run': '删除这次运行记录？', 'delete-session': '删除这个会话？', 'edit-message': '编辑用户消息', 'delete-message': '删除这条及后续消息？', background: '补充背景文本' }[editor.kind];
  return <Modal title={title} onClose={onClose} busy={busy}>
    <form onSubmit={(event) => { event.preventDefault(); void onSave(content); }}>
      <p className="modal-description">{editor.kind === 'delete-run' ? '本次运行的状态与执行步骤将被删除。会话消息会保留，已经发生的工具操作不会撤销。' :
        editor.kind === 'delete-session' ? `“${editor.session.title}”的全部消息、运行记录与执行检查点将被删除。长期记忆和外部文件会保留。` :
        editor.kind === 'delete-message' ? '这条消息及其后的全部消息将被删除，运行上下文会重置。已发生的工具操作不会撤销。' :
          editor.kind === 'edit-message' ? '保存后会删除这条消息之后的全部消息，并重置运行上下文。不会自动重新执行任务。' :
            editor.kind === 'background' ? '只保存到当前会话，不调用模型。需要 Agent 回答时，请在主输入框发送任务。' : '用一个容易辨认的标题，方便下次找到这段对话。'}</p>
      {!deleting && <><label className="field-label" htmlFor="editor-value">{editor.kind === 'rename' ? '会话标题' : '消息内容'}</label>
        {editor.kind === 'rename' ? <input className="field" id="editor-value" autoFocus required maxLength={200} value={content} disabled={busy} onChange={(event) => setContent(event.target.value)} /> :
          <textarea className="field editor-textarea" id="editor-value" autoFocus required rows={7} maxLength={32000} value={content} disabled={busy} onChange={(event) => setContent(event.target.value)} />}</>}
      {error && <p className="inline-error" role="alert">{error}</p>}
      <div className="modal-actions"><button type="button" className="secondary" disabled={busy} onClick={onClose}>取消</button>
        <button className={deleting ? 'danger' : 'primary'} disabled={busy || (!deleting && !content.trim())}>{busy && <LoaderCircle size={15} className="spin" />}{deleting ? '确认删除' : '保存'}</button></div>
    </form>
  </Modal>;
}
