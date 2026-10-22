import Markdown from 'react-markdown';
import remarkGfm from 'remark-gfm';

/** 不启用原始 HTML，也不自动加载模型返回的远程图片。 */
export default function MarkdownBody({ content }: { content: string }) {
  return <div className="markdown"><Markdown remarkPlugins={[remarkGfm]} components={{
    a: ({ children, href }) => <a href={href} target="_blank" rel="noreferrer">{children}</a>,
    img: ({ alt }) => <span className="muted">[图片：{alt || '未加载'}]</span>,
  }}>{content}</Markdown></div>;
}
