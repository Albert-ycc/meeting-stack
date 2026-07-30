import { formatDate, formatTime } from "../format";
import type { SearchItem } from "../types";
import { CopyFolderPathButton } from "./CopyFolderPathButton";

interface SearchResultsProps {
  items: SearchItem[];
  mode: "exact" | "semantic";
  onOpen: (meetingId: string, startMs: number) => void;
  query: string;
}

function highlight(text: string, query: string, enabled: boolean) {
  if (!enabled || !query) return text;
  const escaped = query.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const parts = text.split(new RegExp(`(${escaped})`, "giu"));
  return parts.map((part, index) =>
    part.toLocaleLowerCase() === query.toLocaleLowerCase() ? (
      <mark key={`${part}-${index}`}>{part}</mark>
    ) : (
      part
    ),
  );
}

export function SearchResults({ items, mode, onOpen, query }: SearchResultsProps) {
  return (
    <div className="search-results-list">
      {items.map((item) => {
        const titleId = `search-hit-${item.meeting_id}-${item.segment_id}`;
        return (
          <article className="search-hit" key={`${item.meeting_id}-${item.segment_id}`}>
            <div className="search-hit__meta">
              <span className="archive-code">{item.meeting_id}</span>
              <strong id={titleId}>
                {highlight(item.title, query, mode === "exact" && item.match_kind === "title")}
              </strong>
              <em className="search-match-kind">
                {item.match_kind === "title" ? "标题命中" : "逐字稿命中"}
              </em>
              {item.recording_date && (
                <em className="search-hit__date">{formatDate(item.recording_date)}</em>
              )}
            </div>
            <p>
              <span className="speaker-name">{item.speaker_name || item.speaker_label || "未标记说话人"}</span>
              {highlight(
                item.text,
                query,
                mode === "exact" && (item.match_kind ?? "segment") === "segment",
              )}
            </p>
            <div className="search-hit__actions">
              <button
                className="time-anchor"
                onClick={() => onOpen(item.meeting_id, item.start_ms)}
                type="button"
              >
                <span aria-hidden="true">▶</span> {formatTime(item.start_ms)}
              </button>
              <CopyFolderPathButton
                className="archive-path-copy--inline"
                describedById={titleId}
                path={item.canonical_dir}
              />
            </div>
          </article>
        );
      })}
    </div>
  );
}
