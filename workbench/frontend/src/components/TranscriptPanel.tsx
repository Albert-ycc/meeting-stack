import { useCallback, useEffect, useMemo, useRef, useState, type KeyboardEvent, type MouseEvent } from "react";

import { formatSpeakerLabel, formatTime } from "../format";
import type { Segment } from "../types";

/** 行的开始时间和它在滚动框里的上沿（像素，从滚动内容顶部算） */
export interface RowTop {
  startMs: number;
  top: number;
}

/** 阅读线（滚动框高度 ⅓ 处）下那一行的开始时间：上沿不超过阅读线的最后一行；都在线下时取第一行。 */
export function rowAtLine(rows: RowTop[], scrollTop: number, height: number): number | null {
  if (!rows.length) return null;
  const line = scrollTop + height / 3;
  let found = rows[0].startMs;
  for (const row of rows) {
    if (row.top <= line) found = row.startMs;
    else break;
  }
  return found;
}

/** 逐字稿里选中的一段：原话取选中的文字（时间和说话人不算），时间锚取第一句的开始（R01 异常与边界） */
export interface TranscriptPick {
  quote: string;
  anchorMs: number;
}

// 和后端一致：原话最多 1000 字
const QUOTE_MAX = 1000;

/** 选区盖到的几句正文里被选中的字连起来；一个字都没选到正文时为 null */
export function selectedQuote(container: HTMLElement, range: Range): TranscriptPick | null {
  const parts: string[] = [];
  let anchorMs: number | null = null;
  for (const body of Array.from(container.querySelectorAll<HTMLElement>("[data-start-ms] .segment-text"))) {
    if (!range.intersectsNode(body)) continue;
    const piece = document.createRange();
    piece.selectNodeContents(body);
    if (body.contains(range.startContainer)) piece.setStart(range.startContainer, range.startOffset);
    if (body.contains(range.endContainer)) piece.setEnd(range.endContainer, range.endOffset);
    const text = piece.toString();
    if (!text.trim()) continue;
    if (anchorMs === null) anchorMs = Number(body.closest<HTMLElement>("[data-start-ms]")?.dataset.startMs ?? 0);
    parts.push(text);
  }
  const quote = parts.join("").trim();
  return quote && anchorMs !== null ? { quote, anchorMs } : null;
}

function FlagIcon() {
  return (
    <svg aria-hidden="true" fill="none" stroke="currentColor" strokeLinecap="round" strokeLinejoin="round" strokeWidth="1.5" viewBox="0 0 15 15">
      <path d="M3.2 13V2" />
      <path d="M3.2 2.6c1.3-.8 2.5-.8 3.8 0s2.5.8 3.8 0v5.6c-1.3.8-2.5.8-3.8 0s-2.5-.8-3.8 0" />
    </svg>
  );
}

// 翻页键、方向键也算用户自己滚
const SCROLL_KEYS = new Set(["PageUp", "PageDown", "Home", "End", "ArrowUp", "ArrowDown", " "]);
const MANUAL_SCROLL_MS = 4000;

interface TranscriptPanelProps {
  currentTimeMs: number;
  disabled?: boolean;
  editable: boolean;
  onChange?: (segments: Segment[]) => void;
  onMerge?: (firstId: string, secondId: string) => void;
  onSeek: (milliseconds: number) => void;
  onSplit?: (segmentId: string, characterIndex: number) => void;
  segments: Segment[];
  /** 4d：用户自己滚动时上报阅读线下那一行的开始时间；自动跟随滚到播放行时上报 null */
  onReadingTimeChange?: (milliseconds: number | null) => void;
  /** 选中一段逐字稿时旁边浮出［建成需求］（R01-10、S04）；不传时不浮（编辑态、手机、只读） */
  onCreateRequirement?: (pick: TranscriptPick) => void;
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
  onReadingTimeChange,
  onCreateRequirement,
}: TranscriptPanelProps) {
  const [term, setTerm] = useState("");
  // 选中的一段和浮条的位置（相对滚动内容）
  const [pick, setPick] = useState<(TranscriptPick & { top: number; left: number }) | null>(null);
  const [cursorById, setCursorById] = useState<Record<string, number>>({});
  const activeRef = useRef<HTMLElement | null>(null);
  const panelRef = useRef<HTMLElement | null>(null);
  // 用户自己滚动过之后暂停跟随几秒，免得播放推进时把视线从正在看的地方拽走。
  const manualScrollAtRef = useRef(0);
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const frameRef = useRef<number | null>(null);
  const reportRef = useRef(onReadingTimeChange);
  reportRef.current = onReadingTimeChange;
  const activeId = useMemo(
    () => segments.find((segment) => currentTimeMs >= segment.start_ms && currentTimeMs < segment.end_ms)?.id,
    [currentTimeMs, segments],
  );

  // 播放时跟随当前句。编辑模式、焦点在面板里（正在打字或检索）、或刚手动滚过时不跟，
  // 否则一边听一边改字，页面会被拽到播放位置，光标所在的那句跑出视野。
  useEffect(() => {
    if (editable) return;
    if (panelRef.current?.contains(document.activeElement)) return;
    if (Date.now() - manualScrollAtRef.current < 4000) return;
    if (typeof activeRef.current?.scrollIntoView === "function") {
      activeRef.current.scrollIntoView({ block: "nearest", behavior: "smooth" });
    }
    reportRef.current?.(null);
  }, [activeId, editable]);

  const noteManualScroll = () => {
    manualScrollAtRef.current = Date.now();
  };

  const noteScrollKey = (event: KeyboardEvent<HTMLElement>) => {
    if (SCROLL_KEYS.has(event.key)) noteManualScroll();
  };

  // 只在用户自己滚动之后 4 秒内的 scroll 事件里上报，rAF 节流
  const handleScroll = useCallback(() => {
    if (!reportRef.current || Date.now() - manualScrollAtRef.current >= MANUAL_SCROLL_MS) return;
    if (frameRef.current !== null) return;
    let ran = false;
    const frame = window.requestAnimationFrame(() => {
      ran = true;
      frameRef.current = null;
      const box = scrollRef.current;
      if (!box) return;
      const boxTop = box.getBoundingClientRect().top;
      const rows = Array.from(box.querySelectorAll<HTMLElement>("[data-start-ms]")).map((node) => ({
        startMs: Number(node.dataset.startMs),
        top: node.getBoundingClientRect().top - boxTop + box.scrollTop,
      }));
      reportRef.current?.(rowAtLine(rows, box.scrollTop, box.clientHeight));
    });
    if (!ran) frameRef.current = frame;
  }, []);

  useEffect(
    () => () => {
      if (frameRef.current !== null) window.cancelAnimationFrame(frameRef.current);
    },
    [],
  );

  // 渲染时按 id 找原来的下标（以前每行 findIndex 一遍）
  const indexById = useMemo(() => new Map(segments.map((segment, index) => [segment.id, index])), [segments]);

  const visible = useMemo(() => {
    const normalized = term.trim().toLocaleLowerCase();
    if (!normalized) return segments;
    return segments.filter(
      (segment) =>
        segment.text.toLocaleLowerCase().includes(normalized) ||
        (segment.speaker_name ?? segment.speaker_label ?? "").toLocaleLowerCase().includes(normalized),
    );
  }, [segments, term]);

  // 改字、换稿以后原来的选区作废
  useEffect(() => {
    setPick(null);
  }, [editable, segments]);

  const readSelection = () => {
    const box = scrollRef.current;
    const selection = window.getSelection();
    if (!onCreateRequirement || editable || !box || !selection || selection.isCollapsed || !selection.rangeCount) {
      setPick(null);
      return;
    }
    const range = selection.getRangeAt(0);
    const found = box.contains(range.commonAncestorContainer) ? selectedQuote(box, range) : null;
    if (!found) {
      setPick(null);
      return;
    }
    // 浮条放在选区第一行的末尾（S04）；jsdom 的 Range 没有这两个量位置的方法，放在左上角
    const first =
      (typeof range.getClientRects === "function" ? range.getClientRects()[0] : undefined) ??
      (typeof range.getBoundingClientRect === "function" ? range.getBoundingClientRect() : undefined);
    const boxRect = box.getBoundingClientRect();
    setPick({
      ...found,
      top: first ? Math.max(0, first.top - boxRect.top + box.scrollTop - 8) : 0,
      left: first ? Math.max(0, Math.min(first.right - boxRect.left + 12, box.clientWidth - 240)) : 0,
    });
  };

  const clearPick = (event: MouseEvent) => {
    if (!(event.target as HTMLElement).closest(".transcript-pick")) setPick(null);
  };

  const updateSegment = (id: string, value: string) => {
    onChange?.(segments.map((segment) => (segment.id === id ? { ...segment, text: value } : segment)));
  };

  return (
    <section
      className="transcript-panel"
      onKeyDown={noteScrollKey}
      onTouchMove={noteManualScroll}
      onWheel={noteManualScroll}
      ref={panelRef}
    >
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

      <div
        className="transcript-scroll"
        onKeyDown={(event) => {
          if (event.key === "Escape") setPick(null);
        }}
        onMouseDown={clearPick}
        onMouseUp={readSelection}
        onScroll={handleScroll}
        ref={scrollRef}
      >
        {pick && onCreateRequirement && (
          <div
            aria-label="选中的原话"
            className="transcript-pick"
            // 点浮条不收起选区
            onMouseDown={(event) => event.preventDefault()}
            role="toolbar"
            style={{ top: pick.top, left: pick.left }}
          >
            <span className="transcript-pick__time">{formatTime(pick.anchorMs, true)}</span>
            {[...pick.quote].length > QUOTE_MAX ? (
              <small>选中的超过 {QUOTE_MAX} 字，少选几句</small>
            ) : (
              <button
                onClick={() => {
                  onCreateRequirement({ quote: pick.quote, anchorMs: pick.anchorMs });
                  setPick(null);
                }}
                type="button"
              >
                <FlagIcon />
                建成需求
              </button>
            )}
          </div>
        )}
        {visible.map((segment) => {
          const sourceIndex = indexById.get(segment.id) ?? -1;
          const isActive = segment.id === activeId;
          const cursor = cursorById[segment.id] ?? 0;
          return (
            <article
              aria-current={isActive ? "true" : undefined}
              className={`transcript-row ${isActive ? "is-current" : ""}`}
              data-start-ms={segment.start_ms}
              data-testid={`segment-${segment.id}`}
              key={segment.id}
              ref={isActive ? (node) => { activeRef.current = node; } : undefined}
            >
              <button className="segment-time" onClick={() => onSeek(segment.start_ms)} type="button">
                {formatTime(segment.start_ms)}
              </button>
              <div className="segment-body">
                <span className="segment-speaker">
                  {segment.speaker_name || formatSpeakerLabel(segment.speaker_label) || "说话人"}
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
                  <p className="segment-text">{segment.text}</p>
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
