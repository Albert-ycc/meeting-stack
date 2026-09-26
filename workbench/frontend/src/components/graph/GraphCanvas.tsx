import { motion, useReducedMotion } from "framer-motion";
import { select } from "d3-selection";
import "d3-transition";
import { zoom, zoomIdentity, type ZoomBehavior, type ZoomTransform } from "d3-zoom";
import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
  type KeyboardEvent,
  type ReactNode,
} from "react";

import type { DiskState, GraphEdge, GraphPayload, GraphRootsPayload } from "./graphTypes";
import {
  DIRECTION_NAMES,
  DIRECTION_ORDER,
  NODE_H,
  fitText,
  meetingDateLabel,
  nearestInDirection,
  overlaps,
  textWidth,
  type Box,
  type Direction,
  type LaidNode,
  type StarLayout,
} from "./layout";
import "./GraphCanvas.css";

/** 画布上的视角按项目记住：进对象页再回来，选中和视角都还在。 */
const savedViews = new Map<string, { x: number; y: number; k: number }>();

export function forgetGraphViews() {
  savedViews.clear();
}

const MIN_ZOOM = 0.3;
const MAX_ZOOM = 2.5;
const BASE_FONT = 13;
const PANEL_W = 400;
/** 超过这么多条讨论线就只在选中时画 */
const DISCUSSION_ALWAYS_MAX = 30;

export type LabelDetail = "full" | "short" | "summary";

/** 标签按屏幕上的实际字号决定详略：不小于 11px 显示全名，再小只显示日期和前 6 个字，最小时每个方向收成一个汇总胶囊。 */
export function labelDetailFor(scale: number): LabelDetail {
  const px = BASE_FONT * scale;
  if (px >= 11) return "full";
  if (px >= 7) return "short";
  return "summary";
}

export interface DoorstepAnswer {
  meetingId: string;
  projectId: string | null;
}

interface GraphCanvasProps {
  viewKey: string;
  graph: GraphPayload;
  layout: StarLayout;
  roots: GraphRootsPayload | null;
  selectedId: string | null;
  highlight: Set<string> | null;
  panelOpen: boolean;
  busy: boolean;
  onSelect: (id: string | null) => void;
  onAnswerDoorstep: (answer: DoorstepAnswer) => void;
  /** 上一次画布上某个节点的位置（门口作答后节点从这里飞到新位置） */
  previousPositions?: Map<string, { x: number; y: number }>;
}

function neighbours(edges: GraphEdge[], id: string): Set<string> {
  const result = new Set<string>([id]);
  for (const edge of edges) {
    if (edge.from === id) result.add(edge.to);
    if (edge.to === id) result.add(edge.from);
  }
  return result;
}

function edgeVisible(edge: GraphEdge, focus: string | null, discussionCount: number, selectedEdge: string | null) {
  if (edge.id === selectedEdge) return true;
  const touches = focus !== null && (edge.from === focus || edge.to === focus);
  switch (edge.kind) {
    case "folder":
    case "cross":
      return true;
    case "discussion":
      return discussionCount <= DISCUSSION_ALWAYS_MAX || touches;
    default:
      return touches;
  }
}

function edgePath(from: LaidNode, to: LaidNode, kind: GraphEdge["kind"]): string {
  const x1 = from.x;
  const y1 = from.y;
  const x2 = to.x;
  const y2 = to.y;
  if (kind === "discussion" || kind === "cue" || kind === "cross") {
    const mx = (x1 + x2) / 2;
    const my = (y1 + y2) / 2;
    const dx = x2 - x1;
    const dy = y2 - y1;
    const length = Math.hypot(dx, dy) || 1;
    const bend = Math.min(60, length * 0.18);
    const cx = mx - (dy / length) * bend;
    const cy = my + (dx / length) * bend;
    return `M${x1},${y1} Q${cx},${cy} ${x2},${y2}`;
  }
  return `M${x1},${y1} L${x2},${y2}`;
}

/** 线上的字放在线的中段；一头是中心的项目时，取项目圆边到另一头的中点，免得被项目圆盖住 */
function edgeMid(from: LaidNode, to: LaidNode) {
  const [centre, other] = to.kind === "project" ? [to, from] : from.kind === "project" ? [from, to] : [null, null];
  if (!centre || !other) return { x: (from.x + to.x) / 2, y: (from.y + to.y) / 2 };
  const dx = other.x - centre.x;
  const dy = other.y - centre.y;
  const length = Math.hypot(dx, dy) || 1;
  const a = centre.box.w / 2;
  const b = centre.box.h / 2;
  const rim = 1 / Math.sqrt((dx / length / a) ** 2 + (dy / length / b) ** 2);
  const t = (rim + (length - rim) / 2) / length;
  return { x: centre.x + dx * t, y: centre.y + dy * t };
}

function diskState(roots: GraphRootsPayload | null, id: string): DiskState | null {
  if (!roots) return null;
  const root = roots.roots.find((item) => item.id === id);
  if (root) return root.state;
  const folder = roots.folders.find((item) => item.id === id);
  return folder ? folder.state : null;
}

function shortTitle(date: string, title: string, today: string) {
  return `${meetingDateLabel(date, today)} ${title.slice(0, 6)}`;
}

export function GraphCanvas({
  viewKey,
  graph,
  layout,
  roots,
  selectedId,
  highlight,
  panelOpen,
  busy,
  onSelect,
  onAnswerDoorstep,
  previousPositions,
}: GraphCanvasProps) {
  const viewportRef = useRef<HTMLDivElement>(null);
  const zoomRef = useRef<ZoomBehavior<HTMLDivElement, unknown> | null>(null);
  const [transform, setTransform] = useState<{ x: number; y: number; k: number }>(
    () => savedViews.get(viewKey) ?? { x: 500, y: 320, k: 1 },
  );
  const [hoverId, setHoverId] = useState<string | null>(null);
  const [ringHint, setRingHint] = useState<string | null>(null);
  const [active, setActive] = useState<Partial<Record<Direction, string>>>({});
  const reduceMotion = useReducedMotion();

  const selectedNode = selectedId ? layout.byId.get(selectedId) ?? null : null;
  const selectedEdge = selectedId && !selectedNode ? selectedId : null;
  const focusId = hoverId ?? selectedNode?.id ?? null;
  const dimSet = useMemo(() => {
    if (highlight) return highlight;
    if (hoverId) return neighbours(graph.edges, hoverId);
    return null;
  }, [graph.edges, highlight, hoverId]);
  const discussionCount = useMemo(
    () => graph.edges.filter((edge) => edge.kind === "discussion").length,
    [graph.edges],
  );

  const fitView = useCallback(
    (box: Box, animate = false) => {
      const viewport = viewportRef.current;
      const behaviour = zoomRef.current;
      if (!viewport || !behaviour) return;
      const width = viewport.clientWidth || 1000;
      const height = viewport.clientHeight || 600;
      const pad = 32;
      const k = Math.max(
        MIN_ZOOM,
        Math.min(1.25, (width - pad * 2) / Math.max(box.w, 1), (height - pad * 2) / Math.max(box.h, 1)),
      );
      const x = width / 2 - (box.x + box.w / 2) * k;
      const y = height / 2 - (box.y + box.h / 2) * k;
      const target = zoomIdentity.translate(x, y).scale(k);
      const selection = select(viewport);
      if (animate && !reduceMotion) selection.transition().duration(260).call(behaviour.transform, target);
      else selection.call(behaviour.transform, target);
    },
    [reduceMotion],
  );

  // d3-zoom：拖空白处平移；触控板双指滑动（普通滚轮）平移，捏合或 Ctrl 滚动缩放
  useEffect(() => {
    const viewport = viewportRef.current;
    if (!viewport) return;
    const behaviour = zoom<HTMLDivElement, unknown>()
      .scaleExtent([MIN_ZOOM, MAX_ZOOM])
      .clickDistance(6)
      .filter((event: Event) => {
        if (event.type === "wheel" || event.type === "dblclick") return false;
        // 程序派发的鼠标事件没有 view，d3 拖拽要用它找 document
        if (!(event as UIEvent).view) return false;
        const target = event.target as HTMLElement | null;
        if (target?.closest?.(".graph-doorstep__actions")) return false;
        return !(event as MouseEvent).button;
      })
      .on("zoom", (event: { transform: ZoomTransform }) => {
        const next = { x: event.transform.x, y: event.transform.y, k: event.transform.k };
        savedViews.set(viewKey, next);
        setTransform(next);
      });
    zoomRef.current = behaviour;
    const selection = select(viewport);
    selection.call(behaviour);
    const saved = savedViews.get(viewKey);
    if (saved) selection.call(behaviour.transform, zoomIdentity.translate(saved.x, saved.y).scale(saved.k));
    else fitView(layout.focusBounds);
    const onWheel = (event: WheelEvent) => {
      event.preventDefault();
      if (event.ctrlKey || event.metaKey) {
        const factor = Math.pow(2, -event.deltaY * 0.01);
        const rect = viewport.getBoundingClientRect();
        behaviour.scaleBy(selection, factor, [event.clientX - rect.left, event.clientY - rect.top]);
      } else {
        const current = savedViews.get(viewKey) ?? { k: 1 };
        behaviour.translateBy(selection, -event.deltaX / current.k, -event.deltaY / current.k);
      }
    };
    viewport.addEventListener("wheel", onWheel, { passive: false });
    return () => {
      viewport.removeEventListener("wheel", onWheel);
      selection.on(".zoom", null);
    };
    // 只在换项目时重新挂；布局变化不重置视角
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [viewKey]);

  // 选中的节点在屏幕外或被面板挡住时，平移到看得见的地方（深链进来、「← 上一个」、面板里点别的节点都会遇到）
  useEffect(() => {
    const viewport = viewportRef.current;
    const behaviour = zoomRef.current;
    if (!viewport || !behaviour || !selectedNode) return;
    const width = viewport.clientWidth;
    const height = viewport.clientHeight;
    if (!width || !height) return;
    const margin = 24;
    const right = width - (panelOpen ? PANEL_W + margin : 0) - margin;
    const box = selectedNode.box;
    const left = box.x * transform.k + transform.x;
    const top = box.y * transform.k + transform.y;
    const boxRight = left + box.w * transform.k;
    const bottom = top + box.h * transform.k;
    let dx = 0;
    let dy = 0;
    if (boxRight > right) dx = right - boxRight;
    if (left + dx < margin) dx = margin - left;
    if (bottom > height - margin) dy = height - margin - bottom;
    if (top + dy < margin) dy = margin - top;
    if (!dx && !dy) return;
    const selection = select(viewport);
    if (reduceMotion) behaviour.translateBy(selection, dx / transform.k, dy / transform.k);
    else selection.transition().duration(220).call(behaviour.translateBy, dx / transform.k, dy / transform.k);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedNode?.id, panelOpen]);

  const detail = labelDetailFor(transform.k);

  const zoomBy = (factor: number) => {
    const viewport = viewportRef.current;
    const behaviour = zoomRef.current;
    if (!viewport || !behaviour) return;
    behaviour.scaleBy(select(viewport), factor);
  };

  const focusNode = (id: string) => {
    const elements = viewportRef.current?.querySelectorAll<HTMLElement>("[data-node-id]") ?? [];
    Array.from(elements).find((element) => element.dataset.nodeId === id)?.focus();
  };

  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    // 打中文时不触发单字母快捷键
    if (event.nativeEvent.isComposing || event.key === "Process") return;
    const target = event.target as HTMLElement;
    if (target.closest("select, input, textarea")) return;
    if (event.key === "Escape") {
      onSelect(null);
      return;
    }
    if (event.key === "0") {
      event.preventDefault();
      fitView(layout.focusBounds, true);
      return;
    }
    if (event.key === "+" || event.key === "=") {
      event.preventDefault();
      zoomBy(1.25);
      return;
    }
    if (event.key === "-" || event.key === "_") {
      event.preventDefault();
      zoomBy(0.8);
      return;
    }
    if (event.key.startsWith("Arrow")) {
      const id = target.getAttribute("data-node-id");
      const from = id ? layout.byId.get(id) : layout.byId.get("project");
      if (!from) return;
      const next = nearestInDirection(layout.nodes, from, event.key as "ArrowUp");
      if (next) {
        event.preventDefault();
        setActive((current) => ({ ...current, [next.direction]: next.id }));
        focusNode(next.id);
      }
    }
  };

  const tabIndexFor = (node: LaidNode) => {
    const chosen = active[node.direction] ?? layout.nodes.find((item) => item.direction === node.direction)?.id;
    return chosen === node.id ? 0 : -1;
  };

  const nodeClass = (node: LaidNode, extra = "") => {
    const classes = [`graph-node`, `graph-node--${node.kind}`, extra];
    if (node.id === selectedId) classes.push("is-selected");
    if (dimSet && !dimSet.has(node.id)) classes.push("is-dim");
    if (highlight?.has(node.id)) classes.push("is-lit");
    return classes.filter(Boolean).join(" ");
  };

  const common = (node: LaidNode) => ({
    "data-node-id": node.id,
    "aria-label": node.label,
    "aria-pressed": node.id === selectedId,
    tabIndex: tabIndexFor(node),
    onClick: () => onSelect(node.id === selectedId ? null : node.id),
    onDoubleClick: () => {
      if (node.kind === "project") fitView(layout.focusBounds, true);
    },
    onMouseEnter: () => setHoverId(node.id),
    onMouseLeave: () => setHoverId((current) => (current === node.id ? null : current)),
    onFocus: () => setActive((current) => ({ ...current, [node.direction]: node.id })),
  });

  // dx：左侧节点的圆点在按钮最右端、右侧节点的圆点在最左端，让圆点圆心正好落在布局坐标上
  const positioned = (node: LaidNode, style: CSSProperties, children: ReactNode, extra = "", dx = 0) => {
    const from = previousPositions?.get(node.id);
    const animate = { left: node.x + dx, top: node.y };
    const initial = from && !reduceMotion ? { left: from.x + dx, top: from.y } : false;
    return (
      <motion.button
        {...common(node)}
        animate={animate}
        className={nodeClass(node, extra)}
        initial={initial}
        key={node.id}
        style={style}
        transition={{ duration: reduceMotion ? 0 : 0.45, ease: [0.16, 1, 0.3, 1] }}
        type="button"
      >
        {children}
      </motion.button>
    );
  };

  const renderNode = (node: LaidNode): ReactNode => {
    switch (node.kind) {
      case "project":
        return positioned(
          node,
          { ["--project-color" as string]: node.data.color },
          <>
            <strong>{node.data.name}</strong>
            <span>
              {graph.window.days ? `${graph.window.days} 天` : "全部"} {node.data.meeting_count} 场
            </span>
          </>,
        );
      case "meeting": {
        const meeting = node.data;
        const text =
          detail === "full" ? node.text : detail === "short" ? shortTitle(meeting.date, meeting.title, graph.today) : "";
        return positioned(
          node,
          { ["--project-color" as string]: graph.project.color },
          <>
            {meeting.card === "waiting" && <span aria-label="卡片在等" className="graph-node__mark" title={meeting.card_text ?? ""}>◷</span>}
            {meeting.card === "stopped" && <span aria-label="卡片停了" className="graph-node__mark graph-node__mark--stopped" title={meeting.card_text ?? ""}>⊘</span>}
            {meeting.open_tasks > 0 && (
              <span
                aria-label={`${meeting.open_tasks} 条未完成任务${meeting.pending_tasks ? `，${meeting.pending_tasks} 条待确认` : ""}`}
                className={`graph-node__badge${meeting.pending_tasks ? " graph-node__badge--waiting" : ""}`}
              >
                {meeting.open_tasks}
              </span>
            )}
            {text && <span className="graph-node__label" title={meeting.title}>{text}</span>}
            <i aria-hidden="true" className={`graph-node__dot${meeting.state === "needs_review" ? " graph-node__dot--review" : ""}`} />
          </>,
          "graph-node--left",
          6,
        );
      }
      case "collapsed":
        return positioned(
          node,
          {},
          <>
            <span className="graph-node__label">
              {node.data.label}
              <small>
                {node.data.from.slice(5).replace("-", "/")} – {node.data.to.slice(5).replace("-", "/")}
              </small>
            </span>
            <i aria-hidden="true" className="graph-node__dot graph-node__dot--hollow" />
          </>,
          "graph-node--left",
          6,
        );
      case "doorstep": {
        const item = node.data;
        const other = item.candidates.find((candidate) => candidate.project_id !== graph.project.id);
        const from = previousPositions?.get(node.id);
        return (
          <motion.div
            animate={{ left: node.x, top: node.y, opacity: 1 }}
            className={nodeClass(node)}
            initial={from && !reduceMotion ? { left: from.x, top: from.y, opacity: 0 } : false}
            key={node.id}
            role="group"
            aria-label={node.label}
          >
            <button {...common(node)} className="graph-doorstep__title" type="button">
              <span aria-hidden="true" className="graph-doorstep__q">?</span>
              {detail === "summary" ? "" : fitText(`${meetingDateLabel(item.date, graph.today)} ${item.title}`, 220)}
            </button>
            {detail !== "summary" && (
              <span className="graph-doorstep__actions">
                <button
                  disabled={busy}
                  onClick={() => onAnswerDoorstep({ meetingId: item.meeting_id, projectId: graph.project.id })}
                  type="button"
                >
                  归这里
                </button>
                {other && (
                  <button
                    disabled={busy}
                    onClick={() => onAnswerDoorstep({ meetingId: item.meeting_id, projectId: other.project_id })}
                    type="button"
                  >
                    归 {other.project_name}
                  </button>
                )}
                <button
                  disabled={busy}
                  onClick={() => onAnswerDoorstep({ meetingId: item.meeting_id, projectId: null })}
                  type="button"
                >
                  都不是
                </button>
              </span>
            )}
          </motion.div>
        );
      }
      case "doorstep_more":
        return positioned(node, {}, <span className="graph-node__label">还有 {node.data.count} 场可能是这个项目的</span>);
      case "requirement": {
        const requirement = node.data;
        return positioned(
          node,
          {},
          <>
            <i aria-hidden="true" className={`graph-requirement__bar graph-requirement__bar--${requirement.priority}`} />
            <span className="graph-node__label">
              {detail === "full" ? fitText(requirement.title, 130) : requirement.title.slice(0, 6)}
              {requirement.stale_text && <small>{requirement.stale_text}</small>}
            </span>
            {requirement.open_tasks > 0 && (
              <span className={`graph-node__badge${requirement.pending_tasks ? " graph-node__badge--waiting" : ""}`}>
                {requirement.open_tasks}
              </span>
            )}
          </>,
        );
      }
      case "requirement_more":
        return positioned(node, {}, <span className="graph-node__label">其余 {node.data.count} 个需求</span>);
      case "folder": {
        const folder = node.data;
        const state = diskState(roots, folder.id);
        const offline = state === "volume_offline";
        const missing = state === "missing";
        const extra = offline ? "is-offline" : missing ? "is-missing" : "";
        const label = folder.kind === "cards" ? folder.name : `${folder.name}/`;
        return positioned(
          node,
          {},
          <>
            <i aria-hidden="true" className={`graph-folder__icon graph-folder__icon--${folder.kind}`} />
            {detail !== "summary" && (
              <span className="graph-node__label" title={folder.path}>
                {detail === "full" ? fitText(label, 170) : label.slice(0, 6)}
                {folder.kind === "cards" && folder.enabled !== false && (
                  <small>
                    已写 {folder.written ?? 0}
                    {folder.stopped ? ` · 停 ${folder.stopped}` : ""}
                  </small>
                )}
                {offline && <small>资料盘未连接</small>}
                {missing && <small>找不到了</small>}
              </span>
            )}
          </>,
          `graph-node--right ${extra}`,
          -6,
        );
      }
      case "loose": {
        const count = roots?.loose.count;
        if (roots && count === 0) return null;
        return positioned(
          node,
          {},
          <>
            <i aria-hidden="true" className="graph-folder__icon graph-folder__icon--loose" />
            <span className="graph-node__label">{count === undefined || count === null ? "散放文件…" : `散放 ${count} 个`}</span>
          </>,
          "graph-node--right graph-node--fade",
          -6,
        );
      }
      case "folder_more":
        return positioned(
          node,
          {},
          <span className="graph-node__label">其余 {node.data.count} 个文件夹</span>,
          "graph-node--right",
          -6,
        );
      case "cue":
        return positioned(
          node,
          { fontSize: node.fontSize },
          <span className="graph-node__label">{node.data.text}</span>,
        );
      case "beacon":
        return positioned(
          node,
          { ["--project-color" as string]: node.data.project_color },
          <span className="graph-node__label">{node.data.label}</span>,
        );
      default:
        return null;
    }
  };

  const edges = graph.edges
    .map((edge) => ({ edge, from: layout.byId.get(edge.from), to: layout.byId.get(edge.to) }))
    .filter(
      (item): item is { edge: GraphEdge; from: LaidNode; to: LaidNode } =>
        Boolean(item.from && item.to) && edgeVisible(item.edge, focusId, discussionCount, selectedEdge),
    );

  // 同一时刻亮起的几条线，字挨得太近时朝远离中心的方向错开一行；不压中心的项目圆和正在看的节点
  const edgeLabels: Array<{ id: string; text: string; review: boolean; x: number; y: number }> = [];
  const placed: Box[] = [layout.byId.get("project"), focusId ? layout.byId.get(focusId) : undefined]
    .filter((node): node is LaidNode => Boolean(node))
    .map((node) => node.box);
  for (const { edge, from, to } of edges) {
    const lit = edge.id === selectedEdge || focusId === edge.from || focusId === edge.to;
    if (!lit || !edge.label) continue;
    const text = edge.state === "review" ? `? ${edge.label}` : edge.label;
    const mid = edgeMid(from, to);
    const w = textWidth(text, 11) + 16;
    const box: Box = { x: mid.x - w / 2, y: mid.y - 10, w, h: 20 };
    const step = mid.y < 0 ? -22 : 22;
    for (let guard = 0; guard < 6 && placed.some((other) => overlaps(other, box)); guard += 1) box.y += step;
    placed.push(box);
    edgeLabels.push({ id: edge.id, text, review: edge.state === "review", x: mid.x, y: box.y + 10 });
  }

  const summaries = DIRECTION_ORDER.filter((direction) => direction !== "center").map((direction) => {
    const count = layout.nodes.filter((node) => node.direction === direction).length;
    const position = { left: { x: -260, y: 0 }, right: { x: 260, y: 0 }, top: { x: 0, y: -260 }, bottom: { x: 0, y: 235 } }[
      direction as "left"
    ];
    return { direction, count, ...position };
  });

  return (
    <div
      aria-label={`${graph.project.name} 关系图`}
      className={`graph-viewport graph-viewport--${detail}`}
      onKeyDown={onKeyDown}
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) setHoverId(null);
      }}
      ref={viewportRef}
      role="application"
    >
      <div
        className="graph-world"
        onClick={(event) => {
          if (event.target === event.currentTarget) onSelect(null);
        }}
        style={{ transform: `translate(${transform.x}px, ${transform.y}px) scale(${transform.k})` }}
      >
        <svg aria-hidden="true" className="graph-rings" height="1" width="1">
          {layout.rings.map((ring) => (
            <g key={ring.name}>
              <ellipse className="graph-ring" cx={0} cy={0} rx={ring.rx} ry={ring.ry} />
              <ellipse
                className="graph-ring__hit"
                cx={0}
                cy={0}
                onMouseEnter={() => setRingHint(ring.hint)}
                onMouseLeave={() => setRingHint(null)}
                rx={ring.rx}
                ry={ring.ry}
              />
              {/* 圈名写在圈的正下方：上方是需求卡片，会压住 */}
              <text className="graph-ring__name" x={0} y={ring.ry + 15}>
                {ring.name}
              </text>
            </g>
          ))}
          <text className="graph-ring__name" x={0} y={324}>
            更早
          </text>
        </svg>
        <svg className="graph-edges" height="1" width="1">
          {edges.map(({ edge, from, to }) => {
            const path = edgePath(from, to, edge.kind);
            const lit = edge.id === selectedEdge || focusId === edge.from || focusId === edge.to;
            const width =
              edge.kind === "cue" ? (edge.count && edge.count >= 6 ? 2.6 : edge.count && edge.count >= 3 ? 1.8 : 1.1) : undefined;
            return (
              <g
                className={`graph-edge graph-edge--${edge.kind}${edge.state ? ` graph-edge--${edge.state}` : ""}${lit ? " is-lit" : ""}${
                  dimSet && !lit && !(dimSet.has(edge.from) && dimSet.has(edge.to)) ? " is-dim" : ""
                }`}
                key={edge.id}
              >
                <path className="graph-edge__line" d={path} style={width ? { strokeWidth: width } : undefined} />
                <path
                  aria-label={`连线：${edge.label || edge.kind}`}
                  className="graph-edge__hit"
                  d={path}
                  onClick={() => onSelect(edge.id === selectedEdge ? null : edge.id)}
                  onMouseEnter={() => setHoverId(null)}
                  role="button"
                />
              </g>
            );
          })}
        </svg>
        {DIRECTION_ORDER.map((direction) => {
          const group = layout.nodes.filter((node) => node.direction === direction);
          if (group.length === 0) return null;
          return (
            <div aria-label={DIRECTION_NAMES[direction]} className="graph-group" key={direction} role="group">
              {group.map(renderNode)}
            </div>
          );
        })}
        {/* 线上的字浮在节点上面，只在悬停或选中时出现 */}
        {detail !== "summary" &&
          edgeLabels.map((item) => (
            <span
              aria-hidden="true"
              className={`graph-edge__label${item.review ? " graph-edge__label--review" : ""}`}
              key={`label-${item.id}`}
              style={{ left: item.x, top: item.y }}
            >
              {item.text}
            </span>
          ))}
        {detail === "summary" &&
          summaries.map((summary) =>
            summary.count ? (
              <span className="graph-summary" key={summary.direction} style={{ left: summary.x, top: summary.y }}>
                {DIRECTION_NAMES[summary.direction]} {summary.count}
              </span>
            ) : null,
          )}
      </div>
      {ringHint && <div className="graph-ring-hint" role="status">{ringHint}，远近表示新旧，不表示重要</div>}
      <div className="graph-zoom" role="group" aria-label="缩放">
        <button aria-label="放大" onClick={() => zoomBy(1.25)} type="button">+</button>
        <button aria-label="缩小" onClick={() => zoomBy(0.8)} type="button">−</button>
        <button aria-label="复位" onClick={() => fitView(layout.focusBounds, true)} type="button">0</button>
      </div>
    </div>
  );
}

export const GRAPH_NODE_HEIGHT = NODE_H;
