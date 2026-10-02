import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type MouseEvent as ReactMouseEvent,
  type PointerEvent as ReactPointerEvent,
  type RefObject,
} from "react";

import type { ApiClient } from "../../api";
import {
  dayStamp,
  formatDurationText,
  formatMonthDayClock,
  formatSpeakerLabel,
  formatTime,
  type DayStamp,
} from "../../format";
import type { MeetingSummary, RequirementSource, Segment } from "../../types";
import { useDialogEscape, useDialogFocus } from "../useDialog";
import { anchorLabel } from "./PosterCard";
import "./PoolDialogs.css";
import "./SourcePickerDialog.css";

/** 表单上的来源：提出它的会议、会上原话、时间锚，加上画来源面板要用的会名、时间、时长和录音 */
export type SourceDraft = Omit<RequirementSource, "id" | "kind" | "via_candidate_title">;

/** 和后端一致：原话最多 1000 字 */
export const QUOTE_MAX = 1000;

const PAGE_SIZE = 30;
// 列表滚到离底边这么近就取更早的一页
const LOAD_MORE_DISTANCE = 160;
// 拖选时指针贴近列表上下沿这么多像素内，列表自己往那边滚
const AUTO_SCROLL_EDGE = 36;

/**
 * 选中的几句连成原话：原话取选中的文字，时间锚取第一句有字的开头（R01 异常与边界，和逐字稿页一致）。
 * picked 是屏幕上看得见、高亮着的那几句，按逐字稿先后排好；positions 是它们在整份逐字稿里的位置。
 * 查找过滤以后连选、拖选会跨过被藏起来的句子：不挨着的地方补「……」，不把隔开的话接成一口气说的
 * （第二轮审查一般-1：「没有签收……拒收了……签收入库」接成一句，「拒收」那层意思就没了）。
 */
export function quoteOf(picked: Segment[], positions?: number[]): { quote: string; anchor_ms: number } {
  const first = picked.find((segment) => hasContent(segment.text)) ?? picked[0];
  const quote = picked
    .map((segment, order) => {
      const gap = order > 0 && positions !== undefined && positions[order] !== positions[order - 1] + 1;
      return `${gap ? "……" : ""}${segment.text.trim()}`;
    })
    .join("");
  return { quote, anchor_ms: first?.start_ms ?? 0 };
}

/** 有字（字母、汉字或数字）才算一句原话：纯标点、空白、看不见的格式字符不算，和后端 has_words 一致 */
export function hasContent(text: string): boolean {
  return /[\p{L}\p{N}]/u.test(text);
}

export interface DayGroup {
  key: string;
  title: string;
  meetings: MeetingSummary[];
}

function dayTitle(stamp: DayStamp, currentYear: number): string {
  if (stamp.key === "unknown") return stamp.monthDay;
  return `${stamp.year === currentYear ? "" : `${stamp.year}年`}${stamp.monthDay} ${stamp.weekday}`;
}

/**
 * 按录音当天（本机日期）分组，组标题如「9月29日 星期二」，不是今年的带上年份。
 * 只合并挨着的同一天：接口已经按时间排好，分页拼起来也不会错位。
 */
export function groupByDay(meetings: MeetingSummary[], currentYear = new Date().getFullYear()): DayGroup[] {
  const groups: DayGroup[] = [];
  for (const meeting of meetings) {
    const stamp = dayStamp(meeting.recording_date ?? meeting.created_at);
    const last = groups[groups.length - 1];
    if (last && last.key === stamp.key) last.meetings.push(meeting);
    else groups.push({ key: stamp.key, title: dayTitle(stamp, currentYear), meetings: [meeting] });
  }
  return groups;
}

interface MeetingPages {
  items: MeetingSummary[] | null;
  total: number;
  error: string;
  /** 往下取更早一页失败了：停在列表底下给个重试，不在每次滚动时重发 */
  moreError: string;
  loadingMore: boolean;
  /** 搜索词或范围每变一次、新的第一页到手就加一，列表据此回到顶部 */
  firstPage: number;
  loadMore: () => void;
  retryMore: () => void;
}

/** 会议列表一页一页取：换了搜索词或范围就从头来，还在路上的旧请求作废；翻页靠 total，不靠「这页满没满」 */
function useMeetingPages(apiClient: ApiClient, query: string, projectId: string | null): MeetingPages {
  const [items, setItems] = useState<MeetingSummary[] | null>(null);
  const [total, setTotal] = useState(0);
  const [error, setError] = useState("");
  const [moreError, setMoreError] = useState("");
  const [loadingMore, setLoadingMore] = useState(false);
  const [firstPage, setFirstPage] = useState(0);
  const generation = useRef(0);
  const busy = useRef(false);

  const filters = useMemo(() => ({ q: query.trim() || undefined, project_id: projectId ?? undefined }), [query, projectId]);

  // 搜索打字停 300ms 再查
  useEffect(() => {
    const mine = generation.current;
    busy.current = false;
    setLoadingMore(false);
    setMoreError("");
    const timer = window.setTimeout(
      () => {
        apiClient
          .meetings({ ...filters, limit: PAGE_SIZE, offset: 0 })
          .then((payload) => {
            if (mine !== generation.current) return;
            setItems(payload.items);
            setTotal(payload.total);
            setError("");
            setFirstPage((count) => count + 1);
          })
          .catch((reason: unknown) => {
            if (mine === generation.current) setError(reason instanceof Error ? reason.message : "会议读取失败");
          });
      },
      filters.q ? 300 : 0,
    );
    return () => {
      generation.current += 1;
      window.clearTimeout(timer);
    };
  }, [apiClient, filters]);

  const fetchMore = useCallback(() => {
    if (items === null || items.length >= total || busy.current) return;
    busy.current = true;
    setLoadingMore(true);
    setMoreError("");
    const mine = generation.current;
    apiClient
      .meetings({ ...filters, limit: PAGE_SIZE, offset: items.length })
      .then((payload) => {
        if (mine !== generation.current) return;
        const known = new Set(items.map((item) => item.id));
        const added = payload.items.filter((item) => !known.has(item.id));
        setItems([...items, ...added]);
        // 后端说还有、这页却一条新的都没给：当作到底了，别一直往下要
        setTotal(added.length === 0 ? items.length : payload.total);
      })
      .catch((reason: unknown) => {
        if (mine === generation.current) setMoreError(reason instanceof Error ? reason.message : "会议读取失败");
      })
      .finally(() => {
        if (mine !== generation.current) return;
        busy.current = false;
        setLoadingMore(false);
      });
  }, [apiClient, filters, items, total]);

  const loadMore = useCallback(() => {
    if (!moreError) fetchMore();
  }, [fetchMore, moreError]);

  return { items, total, error, moreError, loadingMore, firstPage, loadMore, retryMore: fetchMore };
}

function SearchIcon() {
  return (
    <svg aria-hidden="true" fill="none" height="15" stroke="currentColor" strokeLinecap="round" strokeWidth="1.6" viewBox="0 0 16 16" width="15">
      <circle cx="7" cy="7" r="4.6" />
      <path d="M10.5 10.5L14 14" />
    </svg>
  );
}

function ArrowIcon() {
  return (
    <svg aria-hidden="true" fill="none" height="14" stroke="currentColor" strokeLinecap="round" strokeLinejoin="round" strokeWidth="1.6" viewBox="0 0 16 16" width="14">
      <path d="M3 8h10M9 4l4 4-4 4" />
    </svg>
  );
}

interface MeetingStepProps {
  pages: MeetingPages;
  query: string;
  onQueryChange: (value: string) => void;
  projectId: string | null;
  projectName: string | null;
  onlyProject: boolean;
  onOnlyProjectChange: (value: boolean) => void;
  /** 从第二步换一场会回来时，列表停在离开时的位置 */
  scrollTop: RefObject<number>;
  onPick: (meeting: MeetingSummary) => void;
  onClose: () => void;
}

/** 第一步：选来源会议。按日期分组，每行会名加项目标签，往下滚自动取更早的会 */
function MeetingStep({
  pages,
  query,
  onQueryChange,
  projectId,
  projectName,
  onlyProject,
  onOnlyProjectChange,
  scrollTop,
  onPick,
  onClose,
}: MeetingStepProps) {
  const listRef = useRef<HTMLDivElement>(null);
  const groups = useMemo(() => groupByDay(pages.items ?? []), [pages.items]);

  // 从第二步回来：列表停回离开时的位置（只在挂载时还原）
  useLayoutEffect(() => {
    if (listRef.current) listRef.current.scrollTop = scrollTop.current;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const seenFirstPage = useRef(pages.firstPage);
  useEffect(() => {
    if (seenFirstPage.current === pages.firstPage) return;
    seenFirstPage.current = pages.firstPage;
    scrollTop.current = 0;
    if (listRef.current) listRef.current.scrollTop = 0;
  }, [pages.firstPage, scrollTop]);

  const hasMore = pages.items !== null && pages.items.length < pages.total;

  // 一页没把列表撑出滚动条（比如会议很短、或者后端一页给得少）就没有滚动可触发：接着取，直到撑满或到底。
  // 没有真实布局（高度为 0）时不判断
  const { loadMore, loadingMore } = pages;
  useEffect(() => {
    const list = listRef.current;
    if (hasMore && !loadingMore && list && list.clientHeight > 0 && list.scrollHeight <= list.clientHeight) loadMore();
  }, [hasMore, loadMore, loadingMore, pages.items]);

  return (
    <>
      <header className="pool-dialog__head">
        <div>
          <h2>选择来源会议</h2>
          <p>先挑一场会，下一步再从逐字稿里选原话</p>
        </div>
        <button aria-label="关闭" className="pool-dialog__close" onClick={onClose} type="button">
          ✕
        </button>
      </header>

      <div className="source-picker__bar">
        <label className="source-picker__search">
          <SearchIcon />
          <input
            aria-label="搜会议"
            data-autofocus
            onChange={(event) => onQueryChange(event.target.value)}
            placeholder="搜会议标题或原话"
            value={query}
          />
        </label>
        {projectId && (
          <select
            aria-label="会议范围"
            className="source-picker__scope"
            onChange={(event) => onOnlyProjectChange(event.target.value === "project")}
            value={onlyProject ? "project" : "all"}
          >
            <option value="project">{projectName ?? "本项目"}</option>
            <option value="all">全部项目</option>
          </select>
        )}
      </div>

      {pages.error ? (
        <p className="pool-dialog__error" role="alert">
          {pages.error}
        </p>
      ) : pages.items === null ? (
        <p className="pool-dialog__state">正在读取…</p>
      ) : pages.items.length === 0 ? (
        <p className="pool-dialog__state">没有找到会议</p>
      ) : (
        <div
          className="source-picker__meetings"
          onScroll={(event) => {
            const list = event.currentTarget;
            scrollTop.current = list.scrollTop;
            if (list.scrollHeight - list.scrollTop - list.clientHeight < LOAD_MORE_DISTANCE) pages.loadMore();
          }}
          ref={listRef}
        >
          {groups.map((group, groupIndex) => (
            <section aria-labelledby={`source-day-${groupIndex}`} className="source-picker__day" key={`${group.key}-${groupIndex}`}>
              <h3 className="source-picker__day-title" id={`source-day-${groupIndex}`}>
                {group.title}
              </h3>
              <ul>
                {group.meetings.map((item) => (
                  <li key={item.id}>
                    <button className="source-picker__meeting" onClick={() => onPick(item)} title={item.title} type="button">
                      <span className="source-picker__meeting-title">{item.title}</span>
                      <span className="source-picker__tag">{item.project_name ?? "未归项目"}</span>
                      <ArrowIcon />
                    </button>
                  </li>
                ))}
              </ul>
            </section>
          ))}
          {pages.loadingMore && <p className="source-picker__more">正在取更早的会议…</p>}
          {pages.moreError && (
            <p className="source-picker__more is-error" role="alert">
              {pages.moreError}
              <button onClick={pages.retryMore} type="button">
                重试
              </button>
            </p>
          )}
        </div>
      )}

      <footer className="pool-dialog__foot source-picker__foot">
        <p className="source-picker__hint">{hasMore ? "更早的会议往下滚动或搜索" : ""}</p>
        <button className="pool-dialog__cancel" onClick={onClose} type="button">
          取消
        </button>
      </footer>
    </>
  );
}

interface Selection {
  /** 连选的起点：Shift+点击、拖选都以它为准 */
  anchor: number;
  /** 选中的几句在整篇逐字稿里的下标，选的当时就按看得见的几句取好 */
  indices: number[];
}

function speakerOf(segment: Segment): string {
  return segment.speaker_name || formatSpeakerLabel(segment.speaker_label);
}

interface QuoteStepProps {
  apiClient: ApiClient;
  meeting: MeetingSummary;
  onBack: () => void;
  onClose: () => void;
  onPicked: (source: SourceDraft) => void;
}

/**
 * 第二步：从这场会的逐字稿里选原话。点一句选一句；按住鼠标拖过几句、或先点一句再按住 Shift 点另一句，
 * 选中间连着的几句。用查找过滤以后，连选只取看得见的句子，选中的和高亮的永远是同一批。
 */
function QuoteStep({ apiClient, meeting, onBack, onClose, onPicked }: QuoteStepProps) {
  const [segments, setSegments] = useState<Segment[] | null>(null);
  const [error, setError] = useState("");
  const [find, setFind] = useState("");
  const [selection, setSelection] = useState<Selection | null>(null);
  const titleRef = useRef<HTMLHeadingElement>(null);
  const listRef = useRef<HTMLOListElement>(null);
  const suppressClick = useRef(false);
  const dragCleanup = useRef<(() => void) | null>(null);

  // 进到第二步：键盘焦点从已经消失的那一行挪到标题上，读屏会念出「选原话」
  useEffect(() => {
    titleRef.current?.focus();
  }, []);

  useEffect(() => {
    let active = true;
    apiClient
      .meeting(meeting.id)
      .then((detail) => {
        if (active) setSegments(detail.segments);
      })
      .catch((reason: unknown) => {
        if (active) setError(reason instanceof Error ? reason.message : "逐字稿读取失败");
      });
    return () => {
      active = false;
    };
  }, [apiClient, meeting.id]);

  const needle = find.trim().toLocaleLowerCase();
  const shown = useMemo(
    () =>
      (segments ?? [])
        .map((segment, index) => ({ segment, index }))
        .filter(
          ({ segment }) =>
            !needle ||
            segment.text.toLocaleLowerCase().includes(needle) ||
            speakerOf(segment).toLocaleLowerCase().includes(needle),
        ),
    [needle, segments],
  );
  // 拖选时指针事件挂在 window 上，要读到最新的一份看得见的句子
  const shownRef = useRef(shown);
  useEffect(() => {
    shownRef.current = shown;
  }, [shown]);

  useEffect(() => () => dragCleanup.current?.(), []);

  /** 起点到终点之间看得见的句子（查找过滤掉的不算）；起点已经被过滤掉就只取终点这一句 */
  const between = (anchor: number, target: number): number[] => {
    const rows = shownRef.current;
    const from = rows.findIndex((row) => row.index === anchor);
    const to = rows.findIndex((row) => row.index === target);
    if (from < 0 || to < 0) return [target];
    return rows.slice(Math.min(from, to), Math.max(from, to) + 1).map((row) => row.index);
  };

  // 选中的永远是看得见的：查找词变了，被过滤掉的那几句既不高亮也不进原话
  const pickedRows = useMemo(() => {
    if (!selection) return [];
    const chosen = new Set(selection.indices);
    return shown.filter(({ index }) => chosen.has(index));
  }, [selection, shown]);
  const picked = pickedRows.map(({ segment }) => segment);
  const pickedIndexes = new Set(pickedRows.map(({ index }) => index));

  const { quote, anchor_ms } = quoteOf(
    picked,
    pickedRows.map(({ index }) => index),
  );
  const tooLong = [...quote].length > QUOTE_MAX;
  const blank = picked.length > 0 && !hasContent(quote);
  const problem = tooLong ? `原话最多 ${QUOTE_MAX} 字，少选几句` : blank ? "选中的只有标点或空白，再选一句有字的" : null;
  const canConfirm = picked.length > 0 && problem === null;

  const choose = (index: number, extend: boolean) => {
    setSelection((current) =>
      extend && current
        ? { anchor: current.anchor, indices: between(current.anchor, index) }
        : { anchor: index, indices: [index] },
    );
  };

  /** 鼠标在哪一行上：拖出列表以后取靠边的那一行，选区跟着自动滚动一起延伸 */
  const rowUnder = (x: number, y: number, fallback: EventTarget | null): number | null => {
    const list = listRef.current;
    let target = fallback instanceof Element ? fallback : null;
    if (list && typeof document.elementFromPoint === "function") {
      const rect = list.getBoundingClientRect();
      target = document.elementFromPoint(
        Math.min(Math.max(x, rect.left + 4), rect.right - 4),
        Math.min(Math.max(y, rect.top + 4), rect.bottom - 4),
      );
    }
    const row = target?.closest("[data-segment-index]");
    return row ? Number(row.getAttribute("data-segment-index")) : null;
  };

  const startDrag = (index: number, event: ReactPointerEvent<HTMLElement>) => {
    // 每次按下都先放掉上一次拖选留下的「吞掉补发的单击」，Shift 点击、触屏点按不能被它吞掉
    suppressClick.current = false;
    // 触屏让它照常滚动，只认点按；Shift、Ctrl、Cmd 点击是连选，不起拖选
    if (event.button !== 0 || event.pointerType === "touch" || event.shiftKey || event.metaKey || event.ctrlKey) return;
    dragCleanup.current?.();
    const drag = { anchor: index, last: index, moved: false, x: event.clientX, y: event.clientY, frame: 0 };

    const extend = (target: EventTarget | null) => {
      const hit = rowUnder(drag.x, drag.y, target);
      if (hit === null || hit === drag.last) return;
      drag.last = hit;
      drag.moved = true;
      setSelection({ anchor: drag.anchor, indices: between(drag.anchor, hit) });
    };
    const tick = () => {
      const list = listRef.current;
      if (list) {
        const rect = list.getBoundingClientRect();
        const over =
          drag.y < rect.top + AUTO_SCROLL_EDGE
            ? drag.y - (rect.top + AUTO_SCROLL_EDGE)
            : drag.y > rect.bottom - AUTO_SCROLL_EDGE
              ? drag.y - (rect.bottom - AUTO_SCROLL_EDGE)
              : 0;
        if (over !== 0) list.scrollTop += Math.max(-24, Math.min(24, over / 2));
      }
      // 列表滚了（自动滚动、或按住时用滚轮），指针没动、底下换了一行：选区也要跟上
      extend(null);
      drag.frame = window.requestAnimationFrame(tick);
    };
    const move = (moveEvent: PointerEvent) => {
      drag.x = moveEvent.clientX;
      drag.y = moveEvent.clientY;
      extend(moveEvent.target);
    };
    const stop = () => {
      window.cancelAnimationFrame(drag.frame);
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", end);
      window.removeEventListener("pointercancel", end);
      dragCleanup.current = null;
    };
    // 拖过几句再松手：浏览器随后补发的那一下 click 不再当成单击、把选区缩回一句
    const end = () => {
      suppressClick.current = drag.moved;
      stop();
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", end);
    window.addEventListener("pointercancel", end);
    drag.frame = window.requestAnimationFrame(tick);
    dragCleanup.current = stop;
  };

  const clickRow = (index: number, event: ReactMouseEvent) => {
    // detail 为 0 是键盘（回车、空格）触发的，不受刚才那次拖选影响
    if (suppressClick.current && event.detail > 0) {
      suppressClick.current = false;
      return;
    }
    suppressClick.current = false;
    choose(index, event.shiftKey);
  };

  const finish = (withQuote: boolean) => {
    onPicked({
      meeting_id: meeting.id,
      meeting_title: meeting.title,
      recording_date: meeting.recording_date ?? null,
      duration_ms: meeting.duration_ms ?? null,
      audio_artifact_id: meeting.audio_artifact_id ?? null,
      quote: withQuote ? quote : "",
      anchor_ms: withQuote ? anchor_ms : null,
    });
  };

  const info = [
    meeting.title,
    meeting.recording_date ? formatMonthDayClock(meeting.recording_date) : null,
    meeting.duration_ms ? formatDurationText(meeting.duration_ms) : null,
    meeting.project_name ?? "未归项目",
  ]
    .filter(Boolean)
    .join(" · ");
  const withSpeakers = (segments ?? []).some((segment) => speakerOf(segment));

  return (
    <>
      <header className="pool-dialog__head source-picker__head">
        <div>
          <button className="source-picker__back" onClick={onBack} type="button">
            ← 换一场会
          </button>
          <h2 ref={titleRef} tabIndex={-1}>
            选原话
          </h2>
          <p>{info}</p>
        </div>
        <button aria-label="关闭" className="pool-dialog__close" onClick={onClose} type="button">
          ✕
        </button>
      </header>

      <div className="source-picker__bar">
        <p className="source-picker__guide">点一句当原话，或按住拖过几句一起选</p>
        <label className="source-picker__search source-picker__search--find">
          <SearchIcon />
          <input
            aria-label="在逐字稿中查找"
            onChange={(event) => setFind(event.target.value)}
            placeholder="在逐字稿中查找"
            value={find}
          />
        </label>
      </div>

      {error ? (
        <p className="pool-dialog__error" role="alert">
          {error}
        </p>
      ) : segments === null ? (
        <p className="pool-dialog__state">正在读取逐字稿…</p>
      ) : segments.length === 0 ? (
        <p className="pool-dialog__state">这场会还没有逐字稿，只能关联这场会</p>
      ) : shown.length === 0 ? (
        <p className="pool-dialog__state">没有句子含「{find.trim()}」</p>
      ) : (
        <ol aria-label="逐字稿" className={`source-picker__lines ${withSpeakers ? "has-speakers" : ""}`} ref={listRef}>
          {shown.map(({ segment, index }) => {
            const isPicked = pickedIndexes.has(index);
            return (
              <li key={segment.id}>
                <button
                  aria-pressed={isPicked}
                  className={`source-picker__line ${isPicked ? "is-picked" : ""}`}
                  data-segment-index={index}
                  onClick={(event) => clickRow(index, event)}
                  onDragStart={(event) => event.preventDefault()}
                  onPointerDown={(event) => startDrag(index, event)}
                  type="button"
                >
                  <span className="source-picker__time">{formatTime(segment.start_ms)}</span>
                  {withSpeakers && <span className="source-picker__speaker">{speakerOf(segment)}</span>}
                  <span className="source-picker__text">{segment.text}</span>
                </button>
              </li>
            );
          })}
        </ol>
      )}

      <div aria-live="polite" className="source-picker__summary">
        {picked.length === 0 ? (
          <p className="source-picker__preview is-empty">还没挑原话</p>
        ) : (
          <>
            <p className="source-picker__summary-head">
              <strong>已选 {picked.length} 句</strong>
              <span className="source-picker__anchor">▶ {anchorLabel(anchor_ms)}</span>
              <small>时间锚取第一句的开始时间</small>
            </p>
            <p className={`source-picker__preview ${problem ? "is-over" : ""}`}>{problem ?? `「${quote}」`}</p>
          </>
        )}
      </div>

      <footer className="pool-dialog__foot source-picker__foot">
        <button className="source-picker__link" onClick={() => finish(false)} type="button">
          只关联这场会
        </button>
        <div>
          <button className="pool-dialog__cancel" onClick={onClose} type="button">
            取消
          </button>
          <button className="pool-dialog__submit" disabled={!canConfirm} onClick={() => finish(true)} type="button">
            确定
          </button>
        </div>
      </footer>
    </>
  );
}

interface SourcePickerDialogProps {
  apiClient: ApiClient;
  /** 需求所属项目：默认只列这个项目的会，可以放开看全部；没选项目时列全部 */
  projectId: string | null;
  projectName: string | null;
  onClose: () => void;
  onPicked: (source: SourceDraft) => void;
}

/**
 * 选来源（R04-3、R04-7）：第一步选来源会议，第二步从这场会的逐字稿里点选一句或拖选几句当原话，
 * 时间锚随原话带出；也可以只关联这场会、不挑原话。
 */
export function SourcePickerDialog({ apiClient, projectId, projectName, onClose, onPicked }: SourcePickerDialogProps) {
  const dialogRef = useRef<HTMLDivElement>(null);
  useDialogFocus(dialogRef);
  useDialogEscape(dialogRef, onClose);
  const [query, setQuery] = useState("");
  const [onlyProject, setOnlyProject] = useState(Boolean(projectId));
  const [meeting, setMeeting] = useState<MeetingSummary | null>(null);
  const pages = useMeetingPages(apiClient, query, onlyProject && projectId ? projectId : null);
  const listScrollTop = useRef(0);

  return (
    <div className="pool-dialog__overlay">
      <div aria-label="选来源" aria-modal="true" className="pool-dialog source-picker" ref={dialogRef} role="dialog">
        {meeting ? (
          <QuoteStep apiClient={apiClient} meeting={meeting} onBack={() => setMeeting(null)} onClose={onClose} onPicked={onPicked} />
        ) : (
          <MeetingStep
            onClose={onClose}
            onOnlyProjectChange={setOnlyProject}
            onPick={setMeeting}
            onQueryChange={setQuery}
            onlyProject={onlyProject}
            pages={pages}
            projectId={projectId}
            projectName={projectName}
            query={query}
            scrollTop={listScrollTop}
          />
        )}
      </div>
    </div>
  );
}
