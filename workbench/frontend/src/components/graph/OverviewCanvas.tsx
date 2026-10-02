/* 全部项目概览的画布：港湾、幽灵岛、项目岛、没挂的文件夹和跨项目的线。平移缩放和键盘操作与项目图一致。 */
import { useEffect, useMemo, useState, type CSSProperties, type KeyboardEvent, type ReactNode } from "react";

import { FolderIcon } from "../FolderIcon";
import {
  OVERVIEW_REGION_NAMES,
  OVERVIEW_REGION_ORDER,
  ghostText,
  islandCountText,
  nearestOverviewNode,
  waitingTotal,
  type OverviewLayout,
  type OverviewNode,
  type OverviewRegion,
} from "./layoutOverview";
import type { GraphOverview, OverviewHarbour, OverviewIsland } from "./overviewTypes";
import { useGraphViewport } from "./useGraphViewport";
import "./GraphCanvas.css";
import "./OverviewGraph.css";

/** 超过这么多条跨项目的线就只在悬停或选中时画 */
export const BRIDGE_ALWAYS_MAX = 30;
const PANEL_W = 400;
const BASE_FONT = 13;
/** 打开时至少缩放到这么多：1280×800 下项目名全名读得清 */
const MIN_FIT_ZOOM = 0.8;

/** 这几类节点点开是右侧面板；其余的点了直接做事（进资料库、开认领框） */
export const PANEL_KINDS = new Set<OverviewNode["kind"]>(["island", "island_more", "harbour", "ghost", "folder", "folder_hint"]);

const HARBOUR_LABELS: Array<{ key: keyof OverviewHarbour["counts"]; label: string }> = [
  { key: "ai_pending", label: "等 AI 判断" },
  { key: "needs_review", label: "待你选" },
  { key: "none", label: "AI 没认出" },
  { key: "new_project", label: "像新项目" },
];

/** 港湾上的两行：「9 场没归项目的会」和四种状态的数（是 0 的不写） */
export function harbourLines(harbour: OverviewHarbour): { head: string; counts: string } {
  const head = harbour.ai_configured
    ? harbour.total
      ? `${harbour.total} 场没归项目的会`
      : "这段时间没有没归项目的会"
    : `没配置 AI：${harbour.total} 场会要你自己选项目`;
  const counts = HARBOUR_LABELS.filter((item) => harbour.counts[item.key] > 0)
    .map((item) => `${item.label} ${harbour.counts[item.key]}`)
    .join(" · ");
  return { head, counts };
}

/** 琥珀点悬停时的说明：「2 场归属待复核、1 场可能是这个项目的、3 条任务待确认」 */
export function waitingTitle(island: OverviewIsland): string {
  return [
    island.waiting.review ? `${island.waiting.review} 场归属待复核` : "",
    island.waiting.doorstep ? `${island.waiting.doorstep} 场可能是这个项目的` : "",
    island.waiting.tasks ? `${island.waiting.tasks} 条任务待确认` : "",
  ]
    .filter(Boolean)
    .join("、");
}

type Detail = "full" | "short" | "summary";

/** 和项目图一样按屏幕上的实际字号决定详略 */
function detailFor(scale: number): Detail {
  const px = BASE_FONT * scale;
  if (px >= 11) return "full";
  if (px >= 7) return "short";
  return "summary";
}

export interface OverviewCanvasProps {
  overview: GraphOverview;
  layout: OverviewLayout;
  selectedId: string | null;
  panelOpen: boolean;
  /** 总文件夹没设（空状态和提示岛上给［选项目总文件夹…］） */
  parentUnset: boolean;
  /** N 键依次跳的节点；没有时调 onNothingToDo */
  attention: string[];
  onSelect: (id: string | null) => void;
  /** 双击项目岛：进项目图 */
  onOpenIsland: (projectId: string) => void;
  /** 「还有 N 个像新项目的名字」：进资料库「像新项目」筛选 */
  onOpenMoreNames: () => void;
  /** 文件夹那一列的「还有 N 个」：打开认领框 */
  onOpenMoreFolders: () => void;
  onPickParent: () => void;
  onNothingToDo: () => void;
}

function isFocusable(node: OverviewNode) {
  return node.kind !== "empty" && node.kind !== "folder_status";
}

export function OverviewCanvas({
  overview,
  layout,
  selectedId,
  panelOpen,
  parentUnset,
  attention,
  onSelect,
  onOpenIsland,
  onOpenMoreNames,
  onOpenMoreFolders,
  onPickParent,
  onNothingToDo,
}: OverviewCanvasProps) {
  const { viewportRef, transform, fitView, zoomBy, reveal } = useGraphViewport({
    viewKey: "overview",
    initialBounds: layout.focusBounds,
    ignorePointer: (target) => Boolean(target.closest?.(".overview-node__actions")),
    minFitZoom: MIN_FIT_ZOOM,
  });
  const [hoverId, setHoverId] = useState<string | null>(null);
  const [active, setActive] = useState<Partial<Record<OverviewRegion, string>>>({});
  const detail = detailFor(transform.k);
  const selectedNode = selectedId ? layout.byId.get(selectedId) ?? null : null;
  const focusId = hoverId ?? selectedNode?.id ?? null;
  const focusable = useMemo(() => layout.nodes.filter(isFocusable), [layout.nodes]);

  // 选中的岛在屏幕外或被面板挡住时，平移到看得见的地方（深链进来、面板里点别的岛）
  useEffect(() => {
    if (!selectedNode) return;
    reveal(selectedNode.box, panelOpen ? PANEL_W + 24 : 0);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedNode?.id, panelOpen]);

  // 跨项目的线：不多时都画；超过 30 条只画悬停或选中的岛连出去的
  const bridges = layout.bridges.filter(
    (bridge) => layout.bridges.length <= BRIDGE_ALWAYS_MAX || bridge.a === focusId || bridge.b === focusId,
  );

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
    if ((event.key === "n" || event.key === "N") && !event.metaKey && !event.ctrlKey && !event.altKey) {
      event.preventDefault();
      if (attention.length === 0) {
        onNothingToDo();
        return;
      }
      const at = selectedId ? attention.indexOf(selectedId) : -1;
      const next = attention[(at + 1) % attention.length];
      const node = layout.byId.get(next);
      if (node) setActive((current) => ({ ...current, [node.region]: next }));
      onSelect(next);
      window.requestAnimationFrame(() => focusNode(next));
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
      const id = target.closest<HTMLElement>("[data-node-id]")?.dataset.nodeId;
      const from = (id ? layout.byId.get(id) : undefined) ?? focusable.find((node) => node.region === "islands") ?? focusable[0];
      if (!from) return;
      const next = nearestOverviewNode(focusable, from, event.key as "ArrowUp");
      if (next) {
        event.preventDefault();
        setActive((current) => ({ ...current, [next.region]: next.id }));
        focusNode(next.id);
      }
    }
  };

  // 每个区一个 Tab 停靠点，区里用方向键走
  const tabIndexFor = (node: OverviewNode) => {
    // 记下的节点可能已经藏起来了（认领了、标成不是项目），这时退回这个区的第一个
    const remembered = active[node.region];
    const chosen =
      remembered && focusable.some((item) => item.id === remembered)
        ? remembered
        : focusable.find((item) => item.region === node.region)?.id;
    return chosen === node.id ? 0 : -1;
  };

  // 悬停在一个有跨项目线的岛上时，和它不相连的岛变淡
  const linkedToHover = useMemo(() => {
    if (!hoverId || layout.byId.get(hoverId)?.kind !== "island") return null;
    const linked = new Set<string>([hoverId]);
    for (const bridge of layout.bridges) {
      if (bridge.a === hoverId) linked.add(bridge.b);
      if (bridge.b === hoverId) linked.add(bridge.a);
    }
    return linked.size > 1 ? linked : null;
  }, [hoverId, layout]);

  const nodeClass = (node: OverviewNode, extra = "") => {
    const classes = ["overview-node", `overview-node--${node.kind}`, extra];
    if (node.id === selectedId) classes.push("is-selected");
    if (linkedToHover && node.kind === "island" && !linkedToHover.has(node.id)) classes.push("is-dim");
    return classes.filter(Boolean).join(" ");
  };

  const place = (node: OverviewNode, extra: CSSProperties = {}): CSSProperties => ({
    left: node.x,
    top: node.y,
    width: node.box.w,
    height: node.box.h,
    ...extra,
  });

  const common = (node: OverviewNode, onClick?: () => void) => ({
    "data-node-id": node.id,
    "aria-label": node.label,
    tabIndex: tabIndexFor(node),
    onClick: onClick ?? (() => onSelect(node.id === selectedId ? null : node.id)),
    onMouseEnter: () => setHoverId(node.id),
    onMouseLeave: () => setHoverId((current) => (current === node.id ? null : current)),
    onFocus: () => setActive((current) => ({ ...current, [node.region]: node.id })),
  });

  const renderNode = (node: OverviewNode): ReactNode => {
    switch (node.kind) {
      case "island": {
        const island = node.data;
        const waiting = waitingTotal(island);
        const r = node.diameter / 2;
        const quiet = island.meetings === 0;
        return (
          <button
            {...common(node)}
            aria-pressed={node.id === selectedId}
            className={nodeClass(node, `overview-island--s${node.size}${quiet ? " is-quiet" : ""}`)}
            key={node.id}
            onDoubleClick={() => onOpenIsland(island.id)}
            style={place(node, { ["--project-color" as string]: island.color, ["--r" as string]: `${r}px` })}
            title={island.name}
            type="button"
          >
            <i aria-hidden="true" className="overview-island__disc" />
            <span className="overview-island__text">
              <strong>{island.name}</strong>
              {detail !== "summary" && <small>{islandCountText(overview.window.days, island.meetings)}</small>}
            </span>
            {waiting > 0 && (
              <span className="overview-island__amber" title={waitingTitle(island)}>
                {waiting}
              </span>
            )}
            {island.stopped_cards > 0 && (
              <span className="overview-island__stopped" title={`${island.stopped_cards} 张卡片停了`}>
                ⊘
              </span>
            )}
          </button>
        );
      }
      case "island_more":
        return (
          <button
            {...common(node)}
            aria-pressed={node.id === selectedId}
            className={nodeClass(node)}
            key={node.id}
            style={place(node, { ["--r" as string]: `${node.diameter / 2}px` })}
            type="button"
          >
            <i aria-hidden="true" className="overview-island__disc" />
            <span className="overview-island__text">
              <strong>其余 {node.data.count} 个项目</strong>
            </span>
          </button>
        );
      case "empty":
        return (
          <div className={nodeClass(node)} key={node.id} role="note" style={place(node)}>
            <p>
              {parentUnset
                ? "还没有项目。设好项目总文件夹后，下面的文件夹可以直接建成项目"
                : "还没有项目。项目总文件夹下面的文件夹可以直接建成项目"}
            </p>
            {parentUnset && (
              <span className="overview-node__actions">
                <button onClick={onPickParent} type="button">
                  选项目总文件夹…
                </button>
              </span>
            )}
          </div>
        );
      case "harbour": {
        const lines = harbourLines(node.data);
        return (
          <button
            {...common(node)}
            aria-pressed={node.id === selectedId}
            className={nodeClass(node, node.data.ai_configured ? "" : "is-no-ai")}
            key={node.id}
            style={place(node)}
            type="button"
          >
            <span className="overview-harbour__eyebrow">港湾</span>
            <strong>{lines.head}</strong>
            {detail !== "summary" && lines.counts && <small>{lines.counts}</small>}
          </button>
        );
      }
      case "ghost":
        return (
          <div aria-label={node.label} className={nodeClass(node)} key={node.id} role="group" style={place(node)}>
            <button
              {...common(node)}
              aria-pressed={node.id === selectedId}
              className="overview-ghost__title"
              style={{ ["--r" as string]: `${node.diameter / 2}px` }}
              title={ghostText(node.data)}
              type="button"
            >
              <i aria-hidden="true" className="overview-ghost__disc" />
              <span className="overview-ghost__text">{detail === "summary" ? node.data.name : ghostText(node.data)}</span>
            </button>
            {detail !== "summary" && (
              <span className="overview-node__actions">
                {/* 两个小按钮都在右侧面板里打开「像是新项目」提示 */}
                <button onClick={() => onSelect(node.id)} tabIndex={-1} type="button">
                  建成项目
                </button>
                <button onClick={() => onSelect(node.id)} tabIndex={-1} type="button">
                  建成需求
                </button>
              </span>
            )}
          </div>
        );
      case "ghost_more":
        return (
          <button {...common(node, onOpenMoreNames)} className={nodeClass(node)} key={node.id} style={place(node)} type="button">
            还有 {node.data.count} 个像新项目的名字
          </button>
        );
      case "folder":
        return (
          <button
            {...common(node)}
            aria-pressed={node.id === selectedId}
            className={nodeClass(node)}
            key={node.id}
            style={place(node)}
            title={node.data.path}
            type="button"
          >
            <i aria-hidden="true" className="overview-folder__disc">
              <FolderIcon />
            </i>
            {detail !== "summary" && <span className="overview-folder__name">{node.data.name}</span>}
          </button>
        );
      case "folder_more":
        return (
          <button {...common(node, onOpenMoreFolders)} className={nodeClass(node)} key={node.id} style={place(node)} type="button">
            还有 {node.data.count} 个
          </button>
        );
      case "folder_hint":
        return (
          <div aria-label={node.label} className={nodeClass(node)} key={node.id} role="group" style={place(node)}>
            <button {...common(node)} aria-pressed={node.id === selectedId} className="overview-hint__title" type="button">
              设项目总文件夹后，这里会列出还没挂的文件夹
            </button>
            <span className="overview-node__actions">
              <button onClick={onPickParent} tabIndex={-1} type="button">
                选项目总文件夹…
              </button>
            </span>
          </div>
        );
      case "folder_status":
        return (
          <div className={nodeClass(node)} key={node.id} role="status" style={place(node)}>
            {node.data.text}
          </div>
        );
      default:
        return null;
    }
  };

  return (
    <div
      aria-label="全部项目关系图"
      className={`graph-viewport overview-viewport overview-viewport--${detail}`}
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
        <svg aria-hidden="true" className="graph-edges overview-bridges" height="1" width="1">
          {bridges.map((bridge) => {
            const lit = focusId !== null && (bridge.a === focusId || bridge.b === focusId);
            return (
              <line
                className={`overview-bridge${lit ? " is-lit" : ""}`}
                key={bridge.id}
                x1={bridge.x1}
                x2={bridge.x2}
                y1={bridge.y1}
                y2={bridge.y2}
              />
            );
          })}
        </svg>
        {OVERVIEW_REGION_ORDER.map((region) => {
          const group = layout.nodes.filter((node) => node.region === region);
          if (group.length === 0) return null;
          return (
            <div aria-label={OVERVIEW_REGION_NAMES[region]} className="graph-group" key={region} role="group">
              {group.map(renderNode)}
            </div>
          );
        })}
        {detail !== "summary" &&
          bridges.map((bridge) => (
            <span
              aria-hidden="true"
              className="graph-edge__label overview-bridge__label"
              key={`label-${bridge.id}`}
              style={{ left: (bridge.x1 + bridge.x2) / 2, top: (bridge.y1 + bridge.y2) / 2 }}
            >
              ×{bridge.count}
            </span>
          ))}
      </div>
      <div className="graph-zoom" role="group" aria-label="缩放">
        <button aria-label="放大" onClick={() => zoomBy(1.25)} type="button">+</button>
        <button aria-label="缩小" onClick={() => zoomBy(0.8)} type="button">−</button>
        <button aria-label="复位" onClick={() => fitView(layout.focusBounds, true)} type="button">0</button>
      </div>
    </div>
  );
}
