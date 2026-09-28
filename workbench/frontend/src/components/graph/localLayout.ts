// 4f：局部图和来龙去脉的版面。纯函数，和 focusLayout 一样确定、不量 DOM（只用 textWidth 估字宽）。
//
// - 列：局部图按有时间的节点的本地日期从早到晚排，列宽 COL_W，中心那份文件的列在 x = 0；
//   来龙去脉按链上的顺序，中心在 x = 0。
// - 行：会议 −140；决议 −70，在它的会那一列；文件（中心和同名版本）0；任务 +70，在它的会那一列；
//   需求 +150，x = 0。
// - 同一列同一行放不下时往远离文件行的方向每个错开 STAGGER 像素，按时间再按 id 排。

import type { Box } from "./layout";
import type { LocalCenter, LocalEdge, LocalGraph, LocalNode, TracePayload } from "./graphTypes";

export const COL_W = 150;
export const STAGGER = 34;
export const ROW_Y: Record<LocalNode["kind"], number> = {
  meeting: -140,
  decision: -70,
  file: 0,
  task: 70,
  requirement: 150,
};
export const NODE_W: Record<LocalNode["kind"], number> = {
  meeting: 120,
  decision: 150,
  file: 140,
  task: 140,
  requirement: 140,
};
const NODE_H: Record<LocalNode["kind"], number> = {
  meeting: 28,
  decision: 32,
  file: 28,
  task: 28,
  requirement: 28,
};
/** 决议两行，最多 28 个字 */
export const DECISION_CHARS = 28;
/** 画的先后：会议先占位，决议、任务、文件、需求再往外错开 */
const PLACE_ORDER: LocalNode["kind"][] = ["meeting", "decision", "task", "file", "requirement"];

export interface LocalLaidNode {
  id: string;
  kind: LocalNode["kind"];
  node: LocalNode;
  x: number;
  y: number;
  box: Box;
  center: boolean;
}

export interface LocalLayout {
  nodes: LocalLaidNode[];
  byId: Map<string, LocalLaidNode>;
  edges: LocalEdge[];
  bounds: Box;
  viewKey: string;
}

/** 本地日期 YYYY-MM-DD；没有时间时为 null */
export function localDate(at: string | null | undefined): string | null {
  if (!at) return null;
  const moment = new Date(at);
  if (Number.isNaN(moment.getTime())) return null;
  const pad = (value: number) => String(value).padStart(2, "0");
  return `${moment.getFullYear()}-${pad(moment.getMonth() + 1)}-${pad(moment.getDate())}`;
}

function time(node: LocalNode): number {
  const value = node.at ? Date.parse(node.at) : Number.NaN;
  return Number.isNaN(value) ? Number.MAX_SAFE_INTEGER : value;
}

function overlapsBox(a: Box, b: Box): boolean {
  return a.x < b.x + b.w && b.x < a.x + a.w && a.y < b.y + b.h && b.y < a.y + a.h;
}

function unionBox(boxes: Box[]): Box {
  if (!boxes.length) return { x: 0, y: 0, w: 0, h: 0 };
  const left = Math.min(...boxes.map((box) => box.x));
  const top = Math.min(...boxes.map((box) => box.y));
  const right = Math.max(...boxes.map((box) => box.x + box.w));
  const bottom = Math.max(...boxes.map((box) => box.y + box.h));
  return { x: left, y: top, w: right - left, h: bottom - top };
}

function isTrace(payload: LocalGraph | TracePayload): payload is TracePayload {
  return Array.isArray((payload as TracePayload).chain);
}

/** 局部图（file_map）或来龙去脉（trace）的坐标 */
export function layoutLocal(payload: LocalGraph | TracePayload): LocalLayout {
  const center: LocalCenter = payload.center;
  const all: LocalNode[] = [center, ...payload.nodes.filter((node) => node.id !== center.id)];
  const byNodeId = new Map(all.map((node) => [node.id, node]));
  const columnX = new Map<string, number>();
  const trace = isTrace(payload);

  if (trace) {
    payload.chain.forEach((id, index) => columnX.set(id, (index - payload.center_index) * COL_W));
  } else {
    const dates = [...new Set(all.map((node) => localDate(node.at)).filter((day): day is string => day !== null))].sort();
    const centerDate = localDate(center.at);
    const centerIndex = centerDate ? dates.indexOf(centerDate) : -1;
    const x = (node: LocalNode) => {
      const day = localDate(node.at);
      if (!day || centerIndex < 0) return 0;
      return (dates.indexOf(day) - centerIndex) * COL_W;
    };
    for (const node of all) columnX.set(node.id, node.id === center.id ? 0 : x(node));
    // 决议、任务在它的会那一列（会画出来时）
    for (const node of all) {
      if ((node.kind === "decision" || node.kind === "task") && node.meeting_id) {
        const meeting = columnX.get(`m:${node.meeting_id}`);
        if (meeting !== undefined) columnX.set(node.id, meeting);
      }
      if (node.kind === "requirement") columnX.set(node.id, 0);
    }
  }

  const placed: LocalLaidNode[] = [];
  const ordered = [...all].sort(
    (a, b) =>
      (a.id === center.id ? -1 : b.id === center.id ? 1 : 0) ||
      PLACE_ORDER.indexOf(a.kind) - PLACE_ORDER.indexOf(b.kind) ||
      time(a) - time(b) ||
      (a.id < b.id ? -1 : a.id > b.id ? 1 : 0),
  );
  for (const node of ordered) {
    const x = columnX.get(node.id) ?? 0;
    const w = NODE_W[node.kind] ?? 140;
    const h = NODE_H[node.kind] ?? 28;
    const base = ROW_Y[node.kind] ?? 0;
    // 往远离文件行的方向错开：文件行上方的往上，其余往下
    const step = base < 0 ? -STAGGER : STAGGER;
    let y = base;
    const boxAt = (at: number): Box => ({ x: x - w / 2, y: at - h / 2, w, h });
    for (let guard = 0; guard < 40 && placed.some((other) => overlapsBox(other.box, boxAt(y))); guard += 1) y += step;
    placed.push({ id: node.id, kind: node.kind, node, x, y, box: boxAt(y), center: node.id === center.id });
  }
  const byId = new Map(placed.map((item) => [item.id, item]));
  const edges = payload.edges.filter((edge) => byNodeId.has(edge.from) && byNodeId.has(edge.to));
  return {
    nodes: placed,
    byId,
    edges,
    bounds: unionBox(placed.map((item) => item.box)),
    viewKey: trace ? `local:trace:${center.id}` : `local:file:${center.file_id ?? center.id}`,
  };
}
