import { copyText } from "../clipboard";
import { formatDate, formatTime } from "../format";
import type { MaterialHitKind, MaterialSearchItem } from "../types";

const HIT_LABELS: Record<MaterialHitKind, string> = {
  text: "正文命中",
  pdf: "正文命中",
  image: "图片文字命中",
  media: "录音文字命中",
};

export function highlightMaterial(text: string, needle: string, enabled: boolean) {
  if (!enabled || !needle) return text;
  const escaped = needle.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const parts = text.split(new RegExp(`(${escaped})`, "giu"));
  return parts.map((part, index) =>
    part.toLocaleLowerCase() === needle.toLocaleLowerCase() ? <mark key={`${part}-${index}`}>{part}</mark> : part,
  );
}

/** 「正文命中 · 文件名命中」；只按文件名命中又读不了时接「读不了：要密码」 */
export function materialMatchLabel(item: MaterialSearchItem): string {
  const labels: string[] = [];
  for (const hit of item.hits) {
    const label = HIT_LABELS[hit.kind] ?? HIT_LABELS.text;
    if (!labels.includes(label)) labels.push(label);
  }
  if (item.name_hit) labels.push("文件名命中");
  return labels.join(" · ");
}

interface SearchMaterialsProps {
  items: MaterialSearchItem[];
  query: string;
  /** 意思相近的：不高亮，标「材料」 */
  similar?: boolean;
  onOpenMaterial?: (fileId: number, startMs?: number) => void;
  onNotice?: (text: string) => void;
}

/** 搜索页「材料里的」一节和「意思相近的」里的材料：每份一行 */
export function SearchMaterials({ items, query, similar = false, onOpenMaterial, onNotice }: SearchMaterialsProps) {
  const copyPath = async (path: string) => {
    try {
      await copyText(path);
      onNotice?.("路径已复制");
    } catch {
      onNotice?.("复制失败，请手动选中路径");
    }
  };

  return (
    <div className="search-results-list">
      {items.map((item) => {
        const titleId = `material-hit-${similar ? "s" : "m"}-${item.file_id}`;
        const label = similar ? "材料" : materialMatchLabel(item);
        const meta = [
          item.project_name,
          item.modified_at ? `修改于 ${formatDate(item.modified_at)}` : "",
          item.more_hits > 0 ? `另有 ${item.more_hits} 处` : "",
          item.copies > 0 ? `另有 ${item.copies} 个副本` : "",
          item.mentioned_meetings > 0 ? `在 ${item.mentioned_meetings} 场会上被提到` : "",
        ].filter(Boolean);
        return (
          <article className="search-hit search-hit--material" key={titleId}>
            <div className="search-hit__meta">
              <span className="material-hit__name">
                {item.ext && <span className="material-hit__ext">{item.ext.toUpperCase()}</span>}
                <strong id={titleId}>{highlightMaterial(item.name, query, !similar && item.name_hit)}</strong>
              </span>
              <em className="search-match-kind">
                {label}
                {item.state_text ? ` · ${item.state_text}` : ""}
              </em>
              {meta.length > 0 && <em className="search-hit__date">{meta.join(" · ")}</em>}
            </div>
            <div className="material-hit__quotes">
              {item.hits.length === 0 && <p className="material-hit__path">{item.rel_path}</p>}
              {item.hits.map((hit, index) => (
                <p key={index}>
                  {hit.start_ms !== null && hit.kind === "media" ? (
                    item.playable && item.root_online && onOpenMaterial ? (
                      <button
                        aria-label={`从 ${formatTime(hit.start_ms)} 放 ${item.name}`}
                        className="material-hit__seek"
                        onClick={() => onOpenMaterial(item.file_id, hit.start_ms ?? 0)}
                        type="button"
                      >
                        ▶ {formatTime(hit.start_ms)}
                      </button>
                    ) : (
                      <span className="material-hit__loc">{formatTime(hit.start_ms)}</span>
                    )
                  ) : (
                    hit.loc && <span className="material-hit__loc">{hit.loc}</span>
                  )}
                  {highlightMaterial(hit.text, hit.matched || query, !similar)}
                </p>
              ))}
            </div>
            <div className="search-hit__actions">
              {onOpenMaterial && (
                <button
                  aria-describedby={titleId}
                  className="time-anchor"
                  onClick={() => onOpenMaterial(item.file_id)}
                  type="button"
                >
                  预览
                </button>
              )}
              <button
                aria-describedby={titleId}
                className="text-button"
                onClick={() => void copyPath(item.path)}
                type="button"
              >
                复制路径
              </button>
            </div>
          </article>
        );
      })}
    </div>
  );
}
