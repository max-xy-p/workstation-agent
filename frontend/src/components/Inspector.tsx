import { useEffect, useState } from 'react';
import { Activity, ArrowRight, Check, CheckCheck, ChevronDown, Circle, Clock3, LoaderCircle, ShieldCheck, X } from 'lucide-react';
import type { Approvals, Decisions, Run, Step } from '../api/contracts';
import { isActive } from '../api/contracts';

export const statusName: Record<Run['status'], string> = {
  created: '准备中', preparing: '准备中', thinking: '运行中', acting: '运行中', observing: '运行中',
  waiting_approval: '等待审批', waiting_user: '等待回复', completed: '已完成', failed: '执行失败',
  cancelled: '已取消', limit_reached: '达到限制',
};
const itemNames = { pending: '待开始', in_progress: '进行中', pending_confirmation: '待验收', completed: '已验收', blocked: '有阻塞' };
const stepName = (step: Step): string => ({ model: '调用模型', context_usage: '检查上下文预算',
  context_sources: '记录检索来源', memory_extraction: '提取长期记忆', read_text_file: '读取文本文件',
  list_files: '查看工作区目录', search_documents: '检索知识库' })[step.name] ?? step.name;

export function Inspector({ run, approvals, busy, onResume, onClose }: {
  run: Run | undefined; approvals: Approvals; busy: boolean;
  onResume: (decisions: Decisions) => Promise<boolean>; onClose: () => void;
}) {
  const [decisions, setDecisions] = useState<Decisions>({});
  const pending = Object.entries(approvals);
  const identity = pending.map(([key, request]) => `${key}:${request.fingerprint}`).join('|');
  useEffect(() => { setDecisions({}); }, [identity]);
  const status = pending.length > 0 ? 'waiting_approval' : run?.status;
  return <aside className="inspector" aria-label="运行详情">
    <header className="inspector-header"><span><Activity size={17} />运行详情</span>
      <button className="icon-button" aria-label="收起运行详情" onClick={onClose}><X size={17} /></button>
    </header>
    <div className="inspector-scroll">
      <div className="run-summary"><span className={`status-pill ${status ?? 'idle'}`}>
        {status && ['thinking', 'preparing', 'acting', 'observing'].includes(status) ? <LoaderCircle size={13} className="spin" /> : <span className="status-dot" />}
        {status ? statusName[status] : '尚未运行'}</span>
        <p>{run ? `更新于 ${new Date(run.updated_at).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })}` : '任务开始后，在这里查看进度'}</p>
      </div>
      {run && isActive(run) && <p className="current-phase"><LoaderCircle size={13} className="spin" />{
        run.node === 'model' ? '正在调用模型' : run.node === 'tools' ? '正在执行工具' : '正在整理上下文与运行状态'
      }</p>}
      {pending.length > 0 && <section className="approval-section">
        <div className="section-heading"><ShieldCheck size={16} /><h3>需要你的决定</h3><span>{pending.length}</span></div>
        <p className="section-note">核对每项操作及完整参数后，选择是否允许执行。</p>
        {pending.map(([key, request]) => <div className="approval" key={key}>
          <h4>{request.name}</h4><pre>{JSON.stringify(request.arguments, null, 2)}</pre>
          <fieldset disabled={busy}><legend className="sr-only">{request.name} 的审批决定</legend>
            <label className={decisions[key] === 'reject' ? 'chosen reject' : ''}>
              <input type="radio" name={key} checked={decisions[key] === 'reject'} onChange={() => setDecisions((value) => ({ ...value, [key]: 'reject' }))} />拒绝
            </label>
            <label className={decisions[key] === 'approve' ? 'chosen approve' : ''}>
              <input type="radio" name={key} checked={decisions[key] === 'approve'} onChange={() => setDecisions((value) => ({ ...value, [key]: 'approve' }))} />允许
            </label>
          </fieldset>
        </div>)}
        <button className="primary full" disabled={busy || pending.some(([key]) => !decisions[key])}
          onClick={() => { void onResume(decisions); }}>提交决定<ArrowRight size={15} /></button>
      </section>}
      {run?.task_status && <section>
        <div className="section-heading"><CheckCheck size={16} /><h3>任务进度</h3></div>
        <p className="task-goal">{run.task_status.goal}</p>
        <ul className="task-list">{run.task_status.items.map((item) => <li key={item.id}>
          {item.status === 'completed' ? <Check size={15} className="success-text" /> :
            item.status === 'in_progress' ? <LoaderCircle size={15} className="spin accent-text" /> :
              item.status === 'pending_confirmation' ? <Clock3 size={15} className="amber-text" /> : <Circle size={13} />}
          <div><span>{item.text}</span><small className={item.status === 'pending_confirmation' ? 'amber-text' : ''}>{itemNames[item.status]}</small></div>
        </li>)}</ul>
        {run.task_status.items.some((item) => item.status === 'pending_confirmation') &&
          <p className="section-note">待验收项目尚未由用户确认，不代表已经通过验收。</p>}
        {run.task_status.next_action && <div className="next-action"><ArrowRight size={14} /><div><small>下一步</small><p>{run.task_status.next_action}</p></div></div>}
        {run.task_status.blockers.map((blocker, index) => <p className="inline-error" key={index}>{blocker}</p>)}
      </section>}
      <section><div className="section-heading"><Activity size={16} /><h3>执行步骤</h3><span>{run?.steps.length ?? 0}</span></div>
        {run && run.steps.length > 0 ? <ol className="step-list">{run.steps.map((step) => <li key={step.id}>
          <span className={`step-indicator ${step.status}`}>{step.status === 'completed' ? <Check size={12} /> : step.status === 'started' ? <LoaderCircle size={12} className="spin" /> : <X size={12} />}</span>
          <div><span>{stepName(step)}</span><small>{step.status === 'completed' ? '已完成' : step.status === 'started' ? '执行中' : '未成功'}</small>
            {step.error && <p className="inline-error">{step.error}</p>}</div>
        </li>)}</ol> : <p className="section-note">{run && isActive(run) ? '等待首个步骤完成…' : '还没有执行步骤。'}</p>}
      </section>
      {run?.error && !pending.length && <p className="inline-error">{run.error}</p>}
      {run && <details className="run-identity"><summary>运行标识<ChevronDown size={13} /></summary><code>{run.id}</code></details>}
    </div>
    <footer className="inspector-footer"><ShieldCheck size={14} />工具操作仍受后端权限检查</footer>
  </aside>;
}
