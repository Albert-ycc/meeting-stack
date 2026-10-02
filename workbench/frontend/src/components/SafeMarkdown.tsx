import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

export function SafeMarkdown({ children }: { children: string }) {
  return (
    <ReactMarkdown
      components={{
        a: ({ href, children: linkChildren, ...props }) => {
          let safe = false;
          if (href) {
            try {
              safe = ["http:", "https:"].includes(new URL(href).protocol);
            } catch {
              safe = false;
            }
          }
          return safe ? (
            <a {...props} href={href} rel="noreferrer noopener" target="_blank">
              {linkChildren}
            </a>
          ) : (
            <span>{linkChildren}</span>
          );
        },
        img: ({ alt }) => <span>{alt ?? "图片已隐藏"}</span>,
      }}
      remarkPlugins={[remarkGfm]}
      skipHtml
    >
      {children}
    </ReactMarkdown>
  );
}
