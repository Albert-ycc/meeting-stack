import { useReducedMotion } from "framer-motion";
import { select } from "d3-selection";
import "d3-transition";
import { zoom, zoomIdentity, type ZoomBehavior, type ZoomTransform } from "d3-zoom";
import {
  useEffect,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent,
  type MouseEvent as ReactMouseEvent,
  type ReactNode,
} from "react";

import { formatTime } from "../../format";
import { BAR_H, CARD_H, layoutMeetingFocus, msToX, xToMs, type FocusItem } from "./focusLayout";
import type { MeetingFocus } from "./graphTypes";
import { meetingDateLabel } from "./layout";
import "./MeetingFocus.css";

const MIN_ZOOM = 0.3;
const MAX_ZOOM = 3;
/** 左右两边给前一场、后一场的签留的宽度；录音条占中间，字按原大显示 */
const SIDE_PAD = 136;
const MIN_BAR_W = 360;
const PANEL_W = 400;

const TASK_STATE: Record<string, string> = {
  pending_confirm: "待确认",
  confirmed: "已确认",
  in_progress: "进行中",
  done: "已完成",
};

export interface FocusPlayer {
  play: (url: string, atMs: number, label: string) => void;
  clip: { url: string } | null;
  positionMs: number;
  node: ReactNode;
}

export interface MeetingFocusViewProps {
  meetingId: string;
  focus: MeetingFocus | null;
  error: string;
  today: string;
  selectedId: string | null;
  panelOpen: boolean;
  player: FocusPlayer;
  onSelect: (id: string | null) => void;
  onCollapse: () => void;
  onExpand: (meetingId: string) => void;
  onOpenMeeting: (meetingId: string) => void;
  onRetry: () => void;
}

function itemLabel(item: FocusItem) {
  const kind = item.kind === "decision" ? "决议" : `任务（${TASK_STATE[item.status ?? ""] ?? item.status ?? ""}）`;
  return `${kind}：${item.text}，${item.atMs === null ? "没有时间点" : formatTime(item.atMs)}`;
}

export function MeetingFocusView({
  meetingId,
  focus,
  error,
  today,
  selectedId,
  panelOpen,
  player,
  onSelect,
  onCollapse,
  onExpand,
  onOpenMeeting,
  onRetry,
}: MeetingFocusViewProps) {
  const viewportRef = useRef<HTMLDivElement>(null);
  const zoomRef = useRef<ZoomBehavior<HTMLDivElement, unknown> | null>(null);
  const fittedRef = useRef<string | null>(null);
  const [transform, setTransform] = useState({ x: SIDE_PAD, y: 260, k: 1 });
  const transformRef = useRef(transform);
  transformRef.current = transform;
  const reduceMotion = useReducedMotion();
  // 录音条按视口宽度排，按 40px 取整，拖窗口时不每一帧重排
  const [viewWidth, setViewWidth] = useState(1000);
  const barW = Math.max(MIN_BAR_W, Math.floor((viewWidth - SIDE_PAD * 2) / 40) * 40);
  const layout = useMemo(
    () => (focus && focus.meeting.id === meetingId ? layoutMeetingFocus(focus, barW) : null),
    [barW, focus, meetingId],
  );
  const audio = focus?.meeting.audio_url ?? null;
  const title = focus?.meeting.title ?? "";

  const fit = (animate = false) => {
    const viewport = viewportRef.current;
    const behaviour = zoomRef.current;
    if (!viewport || !behaviour || !layout) return;
    const width = viewport.clientWidth || 1000;
    const height = viewport.clientHeight || 560;
    const box = layout.bounds;
    // 原大放得下就用原大；卡片排得太高时才缩小
    const k = Math.max(MIN_ZOOM, Math.min(1, (height - 40) / box.h, width / box.w));
    const x = width / 2 - (layout.barW / 2) * k;
    const y = height / 2 - (box.y + box.h / 2) * k;
    const target = zoomIdentity.translate(x, y).scale(k);
    const selection = select(viewport);
    if (animate && !reduceMotion) selection.transition().duration(240).call(behaviour.transform, target);
    else selection.call(behaviour.transform, target);
  };

  // 和星图一样：拖空白处平移；触控板双指滑动平移，捏合或 Ctrl 滚动缩放
  useEffect(() => {
    const viewport = viewportRef.current;
    if (!viewport) return;
    const behaviour = zoom<HTMLDivElement, unknown>()
      .scaleExtent([MIN_ZOOM, MAX_ZOOM])
      .clickDistance(6)
      .filter((event: Event) => {
        if (event.type === "wheel" || event.type === "dblclick") return false;
        if (!(event as UIEvent).view) return false;
        return !(event as MouseEvent).button;
      })
      .on("zoom", (event: { transform: ZoomTransform }) => {
        setTransform({ x: event.transform.x, y: event.transform.y, k: event.transform.k });
      });
    zoomRef.current = behaviour;
    const selection = select(viewport);
    selection.call(behaviour);
    const onWheel = (event: WheelEvent) => {
      event.preventDefault();
      if (event.ctrlKey || event.metaKey) {
        const rect = viewport.getBoundingClientRect();
        behaviour.scaleBy(selection, Math.pow(2, -event.deltaY * 0.01), [event.clientX - rect.left, event.clientY - rect.top]);
      } else {
        const { k } = transformRef.current;
        behaviour.translateBy(selection, -event.deltaX / k, -event.deltaY / k);
      }
    };
    viewport.addEventListener("wheel", onWheel, { passive: false });
    return () => {
      viewport.removeEventListener("wheel", onWheel);
      selection.on(".zoom", null);
    };
  }, []);

  useEffect(() => {
    const viewport = viewportRef.current;
    if (!viewport) return;
    const measure = () => {
      if (viewport.clientWidth) setViewWidth(viewport.clientWidth);
    };
    measure();
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(measure);
    observer.observe(viewport);
    return () => observer.disconnect();
  }, []);

  // 换一场会、录音条变长变短时重新对准；同一场会的数据刷新不动视角
  useEffect(() => {
    const key = `${meetingId}|${barW}`;
    if (!layout || fittedRef.current === key) return;
    fittedRef.current = key;
    fit();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [layout, meetingId, barW]);

  // 选中的卡片被右侧面板挡住时，平移到看得见的地方
  useEffect(() => {
    const viewport = viewportRef.current;
    const behaviour = zoomRef.current;
    if (!viewport || !behaviour || !layout || !selectedId) return;
    const item = [...layout.items, ...layout.more].find((entry) => entry.id === selectedId);
    if (!item) return;
    const { x: tx, k } = transformRef.current;
    const right = (viewport.clientWidth || 1000) - (panelOpen ? PANEL_W + 24 : 24);
    const left = item.box.x * k + tx;
    const boxRight = left + item.box.w * k;
    let dx = 0;
    if (boxRight > right) dx = right - boxRight;
    if (left + dx < 24) dx = 24 - left;
    if (!dx) return;
    const selection = select(viewport);
    if (reduceMotion) behaviour.translateBy(selection, dx / k, 0);
    else selection.transition().duration(220).call(behaviour.translateBy, dx / k, 0);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedId, panelOpen]);

  // 打开进来先把焦点放到画布上，←/→、Esc 直接能用
  useEffect(() => {
    viewportRef.current?.focus({ preventScroll: true });
  }, [meetingId]);

  const zoomBy = (factor: number) => {
    const viewport = viewportRef.current;
    if (viewport && zoomRef.current) zoomRef.current.scaleBy(select(viewport), factor);
  };

  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.nativeEvent.isComposing || event.key === "Process") return;
    if ((event.target as HTMLElement).closest("input, textarea, select")) return;
    if (event.metaKey || event.ctrlKey || event.altKey) return;
    if (event.key === "Escape") {
      event.preventDefault();
      if (selectedId) onSelect(null);
      else onCollapse();
    } else if (event.key === "ArrowLeft" && focus?.previous) {
      event.preventDefault();
      onExpand(focus.previous.meeting_id);
    } else if (event.key === "ArrowRight" && focus?.next) {
      event.preventDefault();
      onExpand(focus.next.meeting_id);
    } else if (event.key === "0") {
      event.preventDefault();
      fit(true);
    } else if (event.key === "+" || event.key === "=") {
      event.preventDefault();
      zoomBy(1.25);
    } else if (event.key === "-" || event.key === "_") {
      event.preventDefault();
      zoomBy(0.8);
    }
  };

  const playAt = (ms: number) => {
    if (audio) player.play(audio, ms, title);
  };

  const onBarClick = (event: ReactMouseEvent<SVGRectElement>) => {
    if (!layout || !audio) return;
    const rect = viewportRef.current?.getBoundingClientRect();
    const worldX = (event.clientX - (rect?.left ?? 0) - transform.x) / transform.k;
    playAt(xToMs(worldX, layout.durationMs, layout.barW));
  };

  const playing = Boolean(audio && player.clip?.url === audio);
  const meeting = focus?.meeting;
  const openTasks = focus ? focus.tasks.filter((task) => task.status !== "done").length : 0;

  let world: ReactNode = null;
  if (layout && focus) {
    const barTop = -BAR_H / 2;
    world = (
      <>
        <svg aria-hidden="true" className="graph-edges meeting-focus__lines" height="1" width="1">
          {layout.items.map((item) =>
            item.anchorX === null ? null : (
              <line
                className={`meeting-focus__leader meeting-focus__leader--${item.kind}${item.id === selectedId ? " is-lit" : ""}`}
                key={`leader-${item.id}`}
                x1={item.anchorX}
                x2={item.x}
                y1={item.side * (BAR_H / 2)}
                y2={item.y - item.side * (CARD_H / 2)}
              />
            ),
          )}
          {layout.ticks.map((tick) => (
            <g className="meeting-focus__tick" key={tick.ms}>
              <line x1={tick.x} x2={tick.x} y1={BAR_H / 2} y2={BAR_H / 2 + 5} />
              <text x={tick.x} y={BAR_H / 2 + 17}>
                {tick.label}
              </text>
            </g>
          ))}
        </svg>
        <svg className="graph-edges meeting-focus__bar-layer" height="1" width="1">
          <rect
            aria-label={audio ? "录音条：点任意处从那里播" : "录音条（没有录音文件）"}
            className={`meeting-focus__bar${audio ? " is-playable" : ""}`}
            height={BAR_H}
            onClick={onBarClick}
            rx={BAR_H / 2}
            width={layout.barW}
            x={0}
            y={barTop}
          />
          {layout.items.map((item) =>
            item.anchorX === null ? null : item.kind === "decision" ? (
              <rect
                aria-hidden="true"
                className="meeting-focus__mark meeting-focus__mark--decision"
                height={8}
                key={`mark-${item.id}`}
                transform={`rotate(45 ${item.anchorX} 0)`}
                width={8}
                x={item.anchorX - 4}
                y={-4}
              />
            ) : (
              <circle
                aria-hidden="true"
                className="meeting-focus__mark meeting-focus__mark--task"
                cx={item.anchorX}
                cy={0}
                key={`mark-${item.id}`}
                r={4}
              />
            ),
          )}
          {playing && layout && (
            <line
              aria-hidden="true"
              className="meeting-focus__playhead"
              x1={msToX(player.positionMs, layout.durationMs, layout.barW)}
              x2={msToX(player.positionMs, layout.durationMs, layout.barW)}
              y1={-22}
              y2={22}
            />
          )}
        </svg>
        {layout.untimedLabels.map((label) => (
          <span className="meeting-focus__untimed" key={`untimed-${label.side}`} style={{ left: label.x, top: label.y }}>
            没有时间点
          </span>
        ))}
        {layout.items.map((item) => (
          <button
            aria-label={itemLabel(item)}
            aria-pressed={item.id === selectedId}
            className={`meeting-focus__card meeting-focus__card--${item.kind}${item.status ? ` is-${item.status}` : ""}${
              item.id === selectedId ? " is-selected" : ""
            }`}
            data-focus-id={item.id}
            key={item.id}
            onClick={() => onSelect(item.id === selectedId ? null : item.id)}
            style={{ left: item.box.x, top: item.box.y, width: item.box.w, height: item.box.h }}
            type="button"
          >
            <span>{item.text}</span>
            {(item.atMs !== null || item.kind === "task") && (
              <small>
                {[
                  item.atMs === null ? "" : formatTime(item.atMs),
                  item.kind === "task" ? TASK_STATE[item.status ?? ""] ?? item.status : "",
                ]
                  .filter(Boolean)
                  .join(" · ")}
              </small>
            )}
          </button>
        ))}
        {layout.more.map((item) => (
          <button
            aria-pressed={item.id === selectedId}
            className={`meeting-focus__more${item.id === selectedId ? " is-selected" : ""}`}
            data-focus-id={item.id}
            key={item.id}
            onClick={() => onSelect(item.id === selectedId ? null : item.id)}
            style={{ left: item.box.x, top: item.box.y, width: item.box.w, height: item.box.h }}
            type="button"
          >
            {item.text}
          </button>
        ))}
      </>
    );
  }

  const neighbour = (side: "previous" | "next") => {
    const item = focus?.[side];
    const prev = side === "previous";
    // 面板开着时右边缘被挡住，后一场先收起来，→ 键照样能用
    if (!focus || (!prev && panelOpen)) return null;
    if (!item) {
      return (
        <span className={`meeting-focus__neighbour meeting-focus__neighbour--${side} is-empty`}>
          {prev ? "这是这个项目最早的会" : "这是这个项目最近的会"}
        </span>
      );
    }
    return (
      <button
        aria-label={`${prev ? "上一场" : "下一场"}：${item.title}`}
        className={`meeting-focus__neighbour meeting-focus__neighbour--${side}`}
        onClick={() => onExpand(item.meeting_id)}
        title={prev ? "上一场（← 键）" : "下一场（→ 键）"}
        type="button"
      >
        {prev && <b aria-hidden="true">‹</b>}
        <span>
          <small>
            {prev ? "上一场" : "下一场"} · {meetingDateLabel(item.date, today)}
          </small>
          <em>{item.title}</em>
        </span>
        {!prev && <b aria-hidden="true">›</b>}
      </button>
    );
  };

  return (
    <div className={`meeting-focus${panelOpen ? " has-panel" : ""}`}>
      <header className="meeting-focus__head">
        <button className="text-button" onClick={onCollapse} type="button">
          ← 回到关系图
        </button>
        <div className="meeting-focus__title">
          <h2>{meeting?.title ?? "正在展开…"}</h2>
          {meeting && focus && (
            <p>
              {meetingDateLabel(meeting.date, today)}
              {meeting.duration_ms ? ` · ${formatTime(meeting.duration_ms)}` : ""} · 定了 {focus.decisions.length} 条 · 任务{" "}
              {focus.tasks.length + focus.tasks_more} 条{openTasks ? `（${openTasks} 条没做完）` : ""}
            </p>
          )}
        </div>
        {player.node}
        <button className="ghost-button" onClick={() => onOpenMeeting(meetingId)} type="button">
          打开会议页 →
        </button>
      </header>
      <div
        aria-label={meeting ? `展开的会：${meeting.title}` : "展开的会"}
        className="graph-viewport meeting-focus__viewport"
        onKeyDown={onKeyDown}
        ref={viewportRef}
        role="application"
        tabIndex={-1}
      >
        <div
          className="graph-world"
          onClick={(event) => {
            if (event.target === event.currentTarget) onSelect(null);
          }}
          style={{ transform: `translate(${transform.x}px, ${transform.y}px) scale(${transform.k})` }}
        >
          {world}
        </div>
        {!focus && !error && <p className="meeting-focus__status">正在展开这场会…</p>}
        {error && (
          <div className="meeting-focus__status" role="alert">
            <p>{error}</p>
            <button className="ghost-button" onClick={onRetry} type="button">
              重试
            </button>
          </div>
        )}
        {layout && !layout.items.length && !layout.more.length && (
          <p className="meeting-focus__status">{focus?.decisions_note ?? "这场会没有记下决议和任务"}</p>
        )}
        {layout && !layout.durationKnown && (
          <p className="meeting-focus__note">
            {layout.hasAnchors ? "不知道录音多长，条的长度按最晚的时间点估" : "不知道录音多长，纪要里也没有时间点"}
          </p>
        )}
        {neighbour("previous")}
        {neighbour("next")}
        <div aria-label="缩放" className="graph-zoom" role="group">
          <button aria-label="放大" onClick={() => zoomBy(1.25)} type="button">
            +
          </button>
          <button aria-label="缩小" onClick={() => zoomBy(0.8)} type="button">
            −
          </button>
          <button aria-label="复位" onClick={() => fit(true)} type="button">
            0
          </button>
        </div>
      </div>
    </div>
  );
}
