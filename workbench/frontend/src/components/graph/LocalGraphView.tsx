/* 4f：以文件为中心的局部图和［来龙去脉］的舞台（替换画布，和展开一场会的 MeetingFocusView 一样），
   右侧面板在旁边，回答都在面板里做。版面由 localLayout 算，自己一份 d3-zoom（useGraphViewport，
   视角按 local:file:<id>、local:trace:<节点> 记住）。DOM 节点加一层 SVG 线。 */
import { useMemo, useState } from "react";

import type { GraphLocal, LocalCenter, LocalEdge, LocalGraph, LocalNode, TracePayload } from "./graphTypes";
import { DECISION_CHARS, layoutLocal, type LocalLaidNode } from "./localLayout";
import { fitText } from "./layout";
import { useGraphViewport } from "./useGraphViewport";
import { TASK_STATUS } from "./panelParts";
import { TRACE_BACK_CUT, TRACE_FORWARD_CUT, nodeDay, traceEmptyText } from "../links/TraceList";
import "./LocalGraph.css";

export const LOCAL_LOADING = "正在取这份文件的关系";
export const LOCAL_EMPTY = "还没有会提到这份文件，也没有任务或决议连到它";
export const LOCAL_GONE = "这份文件已经不在资料盘里了，下面是它还在时的关系";
export const MAP_FAILED = "局部图没取到";

/** 挪过位置的那一句：只写新文件夹的最后一段；在根目录最上层时另一种说法 */
export function movedText(folder: string | null | undefined): string {
  return folder ? `这份文件挪到了『${folder}』文件夹里` : "这份文件挪到了项目文件夹的最上层";
}

/** 舞台标题：「以『报价单 v3.xlsx』为中心」「『报价沟通』的来龙去脉」 */
export function localTitle(local: GraphLocal, center: LocalCenter | null): string {
  const name = center ? centerName(center) : "";
  if (local.kind === "file") return name ? `以『${name}』为中心` : "";
  return name ? `『${name}』的来龙去脉` : "来龙去脉";
}

export function centerName(node: LocalNode): string {
  if (node.kind === "file") return node.name ?? "";
  if (node.kind === "decision") return node.text ?? "";
  return node.title ?? "";
}

function nodeLabel(node: LocalNode): string {
  switch (node.kind) {
    case "meeting":
      return `会议：${node.title ?? ""}`;
    case "decision":
      return `决议：${node.text ?? ""}`;
    case "task":
      return `任务：${node.title ?? ""}`;
    case "requirement":
      return `需求：${node.title ?? ""}`;
    default:
      return `文件：${node.name ?? ""}`;
  }
}

function edgeClass(edge: LocalEdge, trace: boolean): string {
  const classes = ["local-edge", `local-edge--${edge.kind}`];
  if (edge.state === "ask") classes.push("local-edge--ask");
  if (trace) classes.push(edge.on_chain ? "local-edge--chain" : "local-edge--thin");
  return classes.join(" ");
}

export interface LocalError {
  text: string;
  retry: boolean;
}

interface LocalGraphViewProps {
  local: GraphLocal;
  payload: LocalGraph | TracePayload | null;
  error: LocalError | null;
  /** 挪过位置：换成新 id 以后这一句还留着 */
  movedFolder?: string | null;
  selectedId: string | null;
  panelOpen: boolean;
  onSelect: (id: string | null) => void;
  /** 双击文件节点：以它为中心（推一条新历史） */
  onRecenter: (fileId: number) => void;
  onBack: () => void;
  onRetry: () => void;
}

export function LocalGraphView({
  local,
  payload,
  error,
  movedFolder,
  selectedId,
  panelOpen,
  onSelect,
  onRecenter,
  onBack,
  onRetry,
}: LocalGraphViewProps) {
  const layout = useMemo(() => (payload ? layoutLocal(payload) : null), [payload]);
  const trace = Boolean(payload && "chain" in payload);
  const { viewportRef, transform } = useGraphViewport({
    viewKey: layout?.viewKey ?? `local:${local.kind === "file" ? `file:${local.fileId}` : `trace:${local.node}`}`,
    initialBounds: layout?.bounds ?? { x: -300, y: -200, w: 600, h: 400 },
  });
  const [hoverEdge, setHoverEdge] = useState<string | null>(null);
  const center = payload?.center ?? null;

  const notes: string[] = [];
  if (payload && !trace) {
    const map = payload as LocalGraph;
    if (movedFolder !== undefined && movedFolder !== null) notes.push(movedText(movedFolder));
    else if (map.center.moved_from !== undefined) notes.push(movedText(map.center.folder));
    if (map.center.gone) notes.push(LOCAL_GONE);
    if (!map.nodes.length) notes.push(LOCAL_EMPTY);
  }
  if (payload && trace) {
    const chain = payload as TracePayload;
    if (chain.chain.length <= 1) notes.push(traceEmptyText(chain.center));
    if (chain.cut.back) notes.push(TRACE_BACK_CUT);
    if (chain.cut.forward) notes.push(TRACE_FORWARD_CUT);
  }

  const renderNode = (item: LocalLaidNode) => {
    const node = item.node;
    const day = nodeDay(node.at);
    let main = "";
    let small = "";
    if (node.kind === "meeting") {
      main = fitText(`${day} ${node.title ?? ""}`.trim(), 110);
    } else if (node.kind === "decision") {
      main = (node.text ?? "").slice(0, DECISION_CHARS);
      small = node.meeting_caption ?? day;
    } else if (node.kind === "task") {
      main = fitText(node.title ?? "", 130);
      small = [node.meeting_caption, TASK_STATUS[node.status ?? ""]].filter(Boolean).join(" · ");
    } else if (node.kind === "requirement") {
      main = fitText(node.title ?? "", 130);
    } else {
      main = fitText(node.name ?? "", 130);
      small = day;
    }
    return (
      <button
        aria-label={nodeLabel(node)}
        aria-pressed={item.id === selectedId}
        className={`local-node local-node--${item.kind}${item.center ? " is-center" : ""}${item.id === selectedId ? " is-selected" : ""}`}
        data-node-id={item.id}
        key={item.id}
        onClick={() => onSelect(item.id === selectedId ? null : item.id)}
        onDoubleClick={() => {
          if (node.kind === "file" && !item.center && node.file_id !== undefined) onRecenter(node.file_id);
        }}
        style={{ left: item.box.x, top: item.box.y, width: item.box.w, minHeight: item.box.h }}
        type="button"
      >
        <span className={node.kind === "decision" ? "local-node__text local-node__text--two" : "local-node__text"}>{main}</span>
        {small && <small>{small}</small>}
      </button>
    );
  };

  return (
    <div className={`local-stage${panelOpen ? " has-panel" : ""}`}>
      <header className="local-stage__head">
        <h2>{local && localTitle(local, center)}</h2>
        <button className="ghost-button" onClick={onBack} type="button">
          回到关系图
        </button>
      </header>
      {notes.map((note) => (
        <p className="local-stage__note" key={note} role="status">
          {note}
        </p>
      ))}
      {error ? (
        <div className="project-graph__empty" role="alert">
          <p>{error.text}</p>
          {error.retry && (
            <button className="ghost-button" onClick={onRetry} type="button">
              重试
            </button>
          )}
        </div>
      ) : !payload || !layout ? (
        <div className="project-graph__empty">
          <p>{LOCAL_LOADING}</p>
        </div>
      ) : (
        <div
          aria-label={localTitle(local, center)}
          className="graph-viewport local-viewport"
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
            <svg className="graph-edges" height="1" width="1">
              <defs>
                <marker className="graph-arrow" id="local-arrow" markerHeight="7" markerWidth="7" orient="auto" refX="8" refY="4" viewBox="0 0 8 8">
                  <path d="M0,0 L8,4 L0,8 z" />
                </marker>
              </defs>
              {layout.edges.map((edge) => {
                const from = layout.byId.get(edge.from);
                const to = layout.byId.get(edge.to);
                if (!from || !to) return null;
                const path = `M${from.x},${from.y} L${to.x},${to.y}`;
                const arrow =
                  edge.kind === "deliverable" || edge.kind === "later_changed" || edge.kind === "restated" ? "url(#local-arrow)" : undefined;
                return (
                  <g className={`${edgeClass(edge, trace)}${edge.id === selectedId || edge.id === hoverEdge ? " is-lit" : ""}`} key={edge.id}>
                    <path className="graph-edge__line" d={path} markerEnd={arrow} />
                    <path
                      aria-label={`连线：${edge.label || edge.kind}`}
                      className="graph-edge__hit"
                      d={path}
                      onClick={() => onSelect(edge.id === selectedId ? null : edge.id)}
                      onMouseEnter={() => setHoverEdge(edge.id)}
                      onMouseLeave={() => setHoverEdge((current) => (current === edge.id ? null : current))}
                      role="button"
                    />
                  </g>
                );
              })}
            </svg>
            {layout.nodes.map(renderNode)}
            {layout.edges
              .filter((edge) => edge.label && (edge.id === selectedId || edge.id === hoverEdge))
              .map((edge) => {
                const from = layout.byId.get(edge.from);
                const to = layout.byId.get(edge.to);
                if (!from || !to) return null;
                return (
                  <span
                    aria-hidden="true"
                    className="graph-edge__label"
                    key={`label-${edge.id}`}
                    style={{ left: (from.x + to.x) / 2, top: (from.y + to.y) / 2 }}
                  >
                    {edge.label}
                  </span>
                );
              })}
          </div>
        </div>
      )}
    </div>
  );
}
