import { useEffect, useMemo, useRef, useState } from "react";

import { formatTime } from "../format";
import type { Segment } from "../types";

interface TranscriptPanelProps {
  currentTimeMs: number;
  disabled?: boolean;
  editable: boolean;
  onChange?: (segments: Segment[]) => void;
  onMerge?: (firstId: string, secondId: string) => void;
  onSeek: (milliseconds: number) => void;
  onSplit?: (segmentId: string, characterIndex: number) => void;
  segments: Segment[];
}

export function TranscriptPanel({
  currentTimeMs,
  disabled = false,
  editable,
  onChange,
  onMerge,
  onSeek,
  onSplit,
  segments,
}: TranscriptPanelProps) {
  const [term, setTerm] = useState("");
  const [cursorById, setCursorById] = useState<Record<string, number>>({});
  const activeRef = useRef<HTMLElement | null>(null);
  const activeId = useMemo(
    () => segments.find((segment) => currentTimeMs >= segment.start_ms && currentTimeMs < segment.end_ms)?.id,
    [currentTimeMs, segments],
  );

  useEffect(() => {
    if (typeof activeRef.current?.scrollIntoView === "function") {
      activeRef.current.scrollIntoView({ block: "nearest", behavior: "smooth" });
    }
  }, [activeId]);

  const visible = useMemo(() => {
    const normalized = term.trim().toLocaleLowerCase();
    if (!normalized) return segments;
    return segments.filter(
      (segment) =>
        segment.text.toLocaleLowerCase().includes(normalized) ||
        (segment.speaker_name ?? segment.speaker_label ?? "").toLocaleLowerCase().includes(normalized),
    );
  }, [segments, term]);

  const updateSegment = (id: string, value: string) => {
    onChange?.(segments.map((segment) => (segment.id === id ? { ...segment, text: value } : segment)));
  };

  return (
    <section className="transcript-panel">
      <div className="transcript-tools">
        <label className="inline-search">
          <span aria-hidden="true">⌕</span>
          <input
            aria-label="在本次逐字稿中搜索"
            onChange={(event) => setTerm(event.target.value)}
            placeholder="在逐字稿中查找"
            type="search"
            value={term}
          />
        </label>
        <span className="segment-count">{visible.length} 段</span>
      </div>

      <div className="transcript-scroll">
        {visible.map((segment) => {
          const sourceIndex = segments.findIndex((item) => item.id === segment.id);
          const isActive = segment.id === activeId;
          const cursor = cursorById[segment.id] ?? 0;
          return (
            <article
              aria-current={isActive ? "true" : undefined}
              className={`transcript-row ${isActive ? "is-current" : ""}`}
              data-testid={`segment-${segment.id}`}
              key={segment.id}
              ref={isActive ? (node) => { activeRef.current = node; } : undefined}
            >
              <button className="segment-time" onClick={() => onSeek(segment.start_ms)} type="button">
                {formatTime(segment.start_ms)}
              </button>
              <div className="segment-body">
                <span className="segment-speaker">
                  {segment.speaker_name || segment.speaker_label || "说话人"}
                </span>
                {editable ? (
                  <textarea
                    aria-label={`${formatTime(segment.start_ms)} 逐字稿`}
                    disabled={disabled}
                    onChange={(event) => updateSegment(segment.id, event.target.value)}
                    onClick={(event) => {
                      const selectionStart = event.currentTarget.selectionStart;
                      setCursorById((current) => ({
                        ...current,
                        [segment.id]: selectionStart,
                      }));
                    }}
                    onKeyUp={(event) => {
                      const selectionStart = event.currentTarget.selectionStart;
                      setCursorById((current) => ({
                        ...current,
                        [segment.id]: selectionStart,
                      }));
                    }}
                    rows={Math.max(2, Math.ceil(segment.text.length / 32))}
                    value={segment.text}
                  />
                ) : (
                  <p>{segment.text}</p>
                )}
                {editable && (
                  <div className="segment-actions desktop-only">
                    <button
                      disabled={disabled || cursor <= 0 || cursor >= segment.text.length}
                      onClick={() => onSplit?.(segment.id, cursor)}
                      type="button"
                    >
                      从光标拆分
                    </button>
                    {sourceIndex > 0 && (
                      <button
                        disabled={disabled}
                        onClick={() => onMerge?.(segments[sourceIndex - 1].id, segment.id)}
                        type="button"
                      >
                        与上一段合并
                      </button>
                    )}
                  </div>
                )}
              </div>
            </article>
          );
        })}
      </div>
    </section>
  );
}
