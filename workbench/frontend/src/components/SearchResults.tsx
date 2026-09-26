import { formatDate, formatSpeakerLabel, formatTime } from "../format";
import type { SearchItem } from "../types";
import { CopyFolderPathButton } from "./CopyFolderPathButton";

interface SearchResultsProps {
  items: SearchItem[];
  /** 意思相近的结果不高亮：原词不一定在里面 */
  highlight: boolean;
  /** tab 为 "minutes" 时打开纪要页签 */
  onOpen: (meetingId: string, startMs: number, tab?: "minutes") => void;
  query: string;
}

const MATCH_LABELS = {
  title: "标题命中",
  minutes: "纪要命中",
  segment: "逐字稿命中",
} as const;

function highlightText(text: string, needle: string, enabled: boolean) {
  if (!enabled || !needle) return text;
  const escaped = needle.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const parts = text.split(new RegExp(`(${escaped})`, "giu"));
  return parts.map((part, index) =>
    part.toLocaleLowerCase() === needle.toLocaleLowerCase() ? (
      <mark key={`${part}-${index}`}>{part}</mark>
    ) : (
      part
    ),
  );
}

export function SearchResults({ items, highlight, onOpen, query }: SearchResultsProps) {
  return (
    <div className="search-results-list">
      {items.map((item, index) => {
        const kind = item.match_kind ?? "segment";
        const key = `${kind}-${item.meeting_id}-${item.segment_id ?? index}`;
        const titleId = `search-hit-${key}`;
        const needle = item.matched || query;
        return (
          <article className="search-hit" key={key}>
            <div className="search-hit__meta">
              <span className="archive-code">{item.meeting_id}</span>
              <strong id={titleId}>{highlightText(item.title, needle, highlight && kind === "title")}</strong>
              <em className="search-match-kind">{MATCH_LABELS[kind]}</em>
              {(item.recording_date || item.project_name) && (
                <em className="search-hit__date">
                  {[item.recording_date ? formatDate(item.recording_date) : "", item.project_name ?? ""]
                    .filter(Boolean)
                    .join(" · ")}
                </em>
              )}
            </div>
            <p>
              {kind !== "minutes" && (
                <span className="speaker-name">
                  {item.speaker_name || formatSpeakerLabel(item.speaker_label) || "未标记说话人"}
                </span>
              )}
              {highlightText(item.text, needle, highlight && kind !== "title")}
            </p>
            <div className="search-hit__actions">
              {kind === "minutes" ? (
                <button
                  className="time-anchor"
                  onClick={() => onOpen(item.meeting_id, item.start_ms ?? 0, "minutes")}
                  type="button"
                >
                  {item.start_ms === null ? (
                    "打开纪要"
                  ) : (
                    <>
                      <span aria-hidden="true">▶</span> {formatTime(item.start_ms)}
                    </>
                  )}
                </button>
              ) : (
                <button
                  className="time-anchor"
                  onClick={() => onOpen(item.meeting_id, item.start_ms ?? 0)}
                  type="button"
                >
                  <span aria-hidden="true">▶</span> {formatTime(item.start_ms ?? 0)}
                </button>
              )}
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
