import { lazy, Suspense, useRef, useState } from 'react';
import { BookOpen, ExternalLink, X } from 'lucide-react';
import type { Message } from '../api/contracts';

const MarkdownBody = lazy(() => import('./MarkdownBody'));

/** 引用只打开同一消息保存的证据；原文片段按文本显示，避免执行其中的链接或指令。 */
export function AnswerMessage({ message }: { message: Message }) {
  const [selected, setSelected] = useState<string | null>(null);
  const panel = useRef<HTMLDivElement>(null);
  const source = message.sources.find((item) => item.id === selected);
  function open(id: string): void {
    setSelected(id);
    requestAnimationFrame(() => { panel.current?.focus(); panel.current?.scrollIntoView({ block: 'nearest', behavior: 'smooth' }); });
  }
  return <>
    <Suspense fallback={<div className="user-content">{message.content}</div>}>
      <MarkdownBody content={message.content} onSource={open} sourceIds={message.sources.map((item) => item.id)} />
    </Suspense>
    {message.sources.length > 0 && <div className="answer-sources">
      <div className="sources-heading"><BookOpen size={14} /><span>本次检索来源</span><small>{message.sources.length}</small></div>
      <div className="source-list">{message.sources.map((item, index) => <button key={item.id}
        className={`source-chip ${item.id === selected ? 'selected' : ''}`} aria-expanded={item.id === selected}
        onClick={() => selected === item.id ? setSelected(null) : open(item.id)}>
        <span className="source-number">{index + 1}</span><span>{item.title}</span>
      </button>)}</div>
      {source && <div ref={panel} className="source-detail" role="region" aria-label="来源详情" tabIndex={-1}>
        <div className="source-detail-heading"><strong>{source.title}</strong><button className="icon-button" aria-label="关闭来源详情" onClick={() => setSelected(null)}><X size={15} /></button></div>
        <p className="source-location">第 {source.line_start}–{source.line_end} 行{source.source_version && <> · 版本 {source.source_version}</>}</p>
        <blockquote className="source-excerpt">{source.content}</blockquote>
        <p className="source-saved-note">此片段保留检索时的原文。</p>
        <div className="source-footer"><code title={source.checksum}>{source.checksum.slice(0, 12)}</code>
          {source.original_url && <a href={source.original_url} target="_blank" rel="noopener noreferrer">打开工作台原笔记<ExternalLink size={13} /></a>}
        </div>
        <p className="source-uri">{source.source_uri}</p>
        {source.original_url && <p className="source-saved-note">工作台中显示当前版本，内容可能已更新。</p>}
      </div>}
    </div>}
  </>;
}
