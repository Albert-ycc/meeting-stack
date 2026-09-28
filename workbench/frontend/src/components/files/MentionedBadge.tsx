import "../links/related.css";

/** 列表里的小签「3 场会提到」，悬停「在 3 场会上被提到」；0 时不画；没给 onClick 时不可点（4d） */
export function MentionedBadge({ count, onClick }: { count: number | undefined; onClick?: () => void }) {
  if (!count) return null;
  const label = `${count} 场会提到`;
  const title = `在 ${count} 场会上被提到`;
  if (!onClick) {
    return (
      <span className="mentioned-badge" title={title}>
        {label}
      </span>
    );
  }
  return (
    <button className="mentioned-badge mentioned-badge--button" onClick={onClick} title={title} type="button">
      {label}
    </button>
  );
}
