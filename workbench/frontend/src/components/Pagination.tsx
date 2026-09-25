import "./Pagination.css";

interface PaginationProps {
  /** 当前页，从 0 开始 */
  page: number;
  pageCount: number;
  onChange: (page: number) => void;
}

/**
 * 页码条：‹ 1 2 3 … N ›，超过 7 页时中间折叠成省略号。
 * 项目详情的需求/会议子表与任务池都要用同一套页码样式（按三次法则抽成共享组件，未在简报第 6 节列出，回执里已说明）。
 */
export function Pagination({ page, pageCount, onChange }: PaginationProps) {
  // 原型只有一页时也画页码条（选中态「1」，上一页/下一页置灰），不是整条隐藏；调用方保证 pageCount >= 1
  if (pageCount < 1) return null;

  const pages: Array<number | "…"> = [];
  if (pageCount <= 7) {
    for (let i = 0; i < pageCount; i++) pages.push(i);
  } else {
    pages.push(0);
    if (page > 2) pages.push("…");
    for (let i = Math.max(1, page - 1); i <= Math.min(pageCount - 2, page + 1); i++) pages.push(i);
    if (page < pageCount - 3) pages.push("…");
    pages.push(pageCount - 1);
  }

  return (
    <nav aria-label="分页" className="pagination-numbers">
      <button disabled={page <= 0} onClick={() => onChange(page - 1)} type="button">
        ‹
      </button>
      {pages.map((entry, index) =>
        entry === "…" ? (
          <span aria-hidden="true" className="pagination-numbers__ellipsis" key={`ellipsis-${index}`}>
            …
          </span>
        ) : (
          <button
            aria-current={entry === page ? "page" : undefined}
            className={entry === page ? "is-active" : undefined}
            key={entry}
            onClick={() => onChange(entry)}
            type="button"
          >
            {entry + 1}
          </button>
        ),
      )}
      <button disabled={page >= pageCount - 1} onClick={() => onChange(page + 1)} type="button">
        ›
      </button>
    </nav>
  );
}
