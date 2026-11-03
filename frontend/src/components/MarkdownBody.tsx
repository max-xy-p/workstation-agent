import Markdown from 'react-markdown';
import remarkGfm from 'remark-gfm';

/** 不启用原始 HTML，也不自动加载模型返回的远程图片。 */
export default function MarkdownBody({ content, onSource, sourceIds = [] }: {
  content: string; onSource?: (id: string) => void; sourceIds?: string[];
}) {
  return <div className="markdown"><Markdown remarkPlugins={[remarkGfm]} components={{
    a: ({ children, href }) => {
      if (href?.startsWith('#source-')) {
        const id = href.slice('#source-'.length);
        return sourceIds.includes(id) && onSource
          ? <button className="citation-link" onClick={() => onSource(id)} title="查看回答依据">{children}</button>
          : <span className="unverified-citation" title="未保存该来源的可核对片段">{children}</span>;
      }
      return <a href={href} target="_blank" rel="noreferrer">{children}</a>;
    },
    img: ({ alt }) => <span className="muted">[图片：{alt || '未加载'}]</span>,
  }}>{content}</Markdown></div>;
}
