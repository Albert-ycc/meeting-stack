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

// 和后端一致：原话最多 1000 字；至少有一个字母、汉字或数字，纯标点、全空白的不算原话
const QUOTE_MAX = 1000;
const HAS_WORDS = /[\p{L}\p{N}]/u;
// 浮条的大致尺寸：贴边、夹在可见范围里时用
const PICK_WIDTH = 240;
const PICK_HEIGHT = 44;

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
  /** 有原因不能建时，浮条上只写原因、不给按钮（逐字稿有没保存的修改：原话会带上没存下来的字） */
  pickBlockedReason?: string;
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
  pickBlockedReason,
}: TranscriptPanelProps) {
  const [term, setTerm] = useState("");
  // 选中的一段和浮条的位置（相对滚动内容）
  const [pick, setPick] = useState<(TranscriptPick & { top: number; left: number }) | null>(null);
  // 在滚动框里按下了鼠标、还没松开（可能在框外松开）
  const selectingRef = useRef(false);
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
  // 打开会议后头一回跟到的那句（从原话时间锚、决议时间点跳进来的）滚到框的正中，一眼看得到（R02-10）；
  // 之后播放推进只挪到刚好看得见，免得一句一句地晃
  const followedRef = useRef(false);
  useEffect(() => {
    if (editable) return;
    if (panelRef.current?.contains(document.activeElement)) return;
    if (Date.now() - manualScrollAtRef.current < 4000) return;
    if (typeof activeRef.current?.scrollIntoView === "function") {
      activeRef.current.scrollIntoView({ block: followedRef.current ? "nearest" : "center", behavior: "smooth" });
      followedRef.current = true;
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
    selectingRef.current = false;
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
    // 浮条贴在选区最后一行下面，也就是松手的地方：拖着让逐字稿自动滚动选了好几屏时，第一行早滚出去了，
    // 放在那儿就看不见（审查 M6）；从说话人那里拖起时也不压住选中的字。夹在滚动框的可见范围里。
    // jsdom 的 Range 没有量位置的方法，放在左上角
    const rects = typeof range.getClientRects === "function" ? Array.from(range.getClientRects()) : [];
    const last =
      rects[rects.length - 1] ??
      (typeof range.getBoundingClientRect === "function" ? range.getBoundingClientRect() : undefined);
    const boxRect = box.getBoundingClientRect();
    const lowest = Math.max(box.scrollTop, box.scrollTop + box.clientHeight - PICK_HEIGHT);
    setPick({
      ...found,
      top: last ? Math.min(Math.max(last.bottom - boxRect.top + box.scrollTop + 6, box.scrollTop), lowest) : 0,
      left: last
        ? Math.max(0, Math.min(last.right - boxRect.left - PICK_WIDTH / 2, box.clientWidth - PICK_WIDTH))
        : 0,
    });
  };
  const readSelectionRef = useRef(readSelection);
  readSelectionRef.current = readSelection;

  // 在逐字稿里按下、拖到框外才松开：框上收不到 mouseup，在文档上补收一次（审查 M6）
  useEffect(() => {
    const finish = (event: globalThis.MouseEvent) => {
      if (!selectingRef.current || scrollRef.current?.contains(event.target as Node)) return;
      readSelectionRef.current();
    };
    document.addEventListener("mouseup", finish);
    return () => document.removeEventListener("mouseup", finish);
  }, []);

  const clearPick = (event: MouseEvent) => {
    selectingRef.current = true;
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
            {pickBlockedReason ? (
              <small>{pickBlockedReason}</small>
            ) : [...pick.quote].length > QUOTE_MAX ? (
              <small>选中的超过 {QUOTE_MAX} 字，少选几句</small>
            ) : !HAS_WORDS.test(pick.quote) ? (
              <small>选中的只有标点，换一句</small>
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
