// 4f：画哪些线（纯函数，取代 GraphCanvas 里的 edgeVisible）。
//
// - 碰到焦点（悬停或选中的节点）的线最多画 FOCUS_EDGE_MAX 条，按 15 级顺序取：选中的那条、可能过时、
//   产出、待复核的归属、像是新需求、字面的提到、放宽的提到、交付物、讨论、归属、相关、写入、线索词
//   （次数多的先）、跨项目、文件夹；同级的先取会议一端新的，再按边 id。多出来的给面板列出来。
// - 不碰焦点、一直画的线全图最多 EDGE_DRAW_MAX 条；超了依次去掉相关、讨论、放宽的提到、每场会第 2 条
//   以后的字面提到。
// 调用前先滤掉两端不在图上的线（和［提到］关着时的提到线），这里只管取舍。

import type { GraphEdge } from "./graphTypes";

export const FOCUS_EDGE_MAX = 12;
export const EDGE_DRAW_MAX = 150;
/** 超过这么多条讨论线就只在碰到焦点时画（第一期的规矩） */
export const DISCUSSION_ALWAYS_MAX = 30;

const KNOWN_KINDS = new Set([
  "attribution",
  "cue",
  "discussion",
  "folder",
  "write",
  "cross",
  "suggested",
  "mentioned",
  "produced",
  "deliverable",
  "affects",
  "related",
]);

/** 认不出的新种类（后台比页面新）按讨论线处理 */
export function edgeStyleKind(kind: string): GraphEdge["kind"] {
  return (KNOWN_KINDS.has(kind) ? kind : "discussion") as GraphEdge["kind"];
}

/** 碰焦点的线的级别，越小越先画 */
export function focusRank(edge: GraphEdge): number {
  const kind = edgeStyleKind(edge.kind);
  switch (kind) {
    case "affects":
      return 1;
    case "produced":
      return 2;
    case "attribution":
      return edge.state === "review" ? 3 : 9;
    case "suggested":
      return 4;
    case "mentioned":
      return edge.relation_id !== undefined && edge.relation_id !== null ? 6 : 5;
    case "deliverable":
      return 7;
    case "discussion":
      return 8;
    case "related":
      return 10;
    case "write":
      return 11;
    case "cue":
      return 12;
    case "cross":
      return 13;
    case "folder":
      return 14;
    default:
      return 8;
  }
}

/** 不碰焦点时也一直画的线 */
function alwaysDrawn(edge: GraphEdge, discussionCount: number): boolean {
  switch (edgeStyleKind(edge.kind)) {
    case "folder":
    case "cross":
    case "suggested":
    case "mentioned":
    case "produced":
    case "affects":
    case "deliverable":
    case "related":
      return true;
    case "discussion":
      return discussionCount <= DISCUSSION_ALWAYS_MAX;
    default:
      return false;
  }
}

export interface DrawOptions {
  /** 会议离今天几天（新的在前）；不给时只按边 id */
  meetingAge?: Map<string, number>;
}

function sorter(options: DrawOptions) {
  const age = (edge: GraphEdge) => {
    const meetingId = edge.meeting_id ?? (edge.from.startsWith("m:") ? edge.from.slice(2) : null);
    return meetingId !== null ? options.meetingAge?.get(meetingId) ?? Number.MAX_SAFE_INTEGER : Number.MAX_SAFE_INTEGER;
  };
  return (a: GraphEdge, b: GraphEdge) =>
    focusRank(a) - focusRank(b) ||
    (edgeStyleKind(a.kind) === "cue" ? (b.count ?? 0) - (a.count ?? 0) : 0) ||
    age(a) - age(b) ||
    (a.id < b.id ? -1 : a.id > b.id ? 1 : 0);
}

export interface DrawnEdges {
  /** 要画的线 */
  drawn: GraphEdge[];
  /** 碰到焦点却没画出来的线（面板里「还有 N 条线没画出来」） */
  hidden: GraphEdge[];
}

/**
 * 从已经滤过两端的线里取要画的：focus 是悬停或选中的节点，selectedEdge 是选中的线。
 * 结果里 drawn 保留传入的先后（画的层次不变）。
 */
export function drawnEdges(
  edges: GraphEdge[],
  focus: string | null,
  selectedEdge: string | null,
  options: DrawOptions = {},
): DrawnEdges {
  const order = sorter(options);
  const keep = new Set<string>();
  const hidden: GraphEdge[] = [];
  const selected = selectedEdge ? edges.find((edge) => edge.id === selectedEdge) : undefined;
  if (selected) keep.add(selected.id);
  if (focus) {
    const touching = edges
      .filter((edge) => (edge.from === focus || edge.to === focus) && edge.id !== selectedEdge)
      .sort(order);
    // 选中的那条也碰到焦点时，它占 12 条里的第一条
    const selectedTouches = Boolean(selected && (selected.from === focus || selected.to === focus));
    const room = FOCUS_EDGE_MAX - (selectedTouches ? 1 : 0);
    touching.forEach((edge, index) => {
      if (index < room) keep.add(edge.id);
      else hidden.push(edge);
    });
  }
  const discussionCount = edges.filter((edge) => edge.kind === "discussion").length;
  const hiddenIds = new Set(hidden.map((edge) => edge.id));
  const background = edges.filter(
    (edge) => !keep.has(edge.id) && !hiddenIds.has(edge.id) && alwaysDrawn(edge, discussionCount),
  );
  const backgroundKeep = capBackground(background, order);
  for (const edge of backgroundKeep) keep.add(edge.id);
  return { drawn: edges.filter((edge) => keep.has(edge.id)), hidden };
}

/** 一直画的线超过 EDGE_DRAW_MAX 时，依次去掉相关、讨论、放宽的提到、每场会第 2 条以后的字面提到 */
function capBackground(edges: GraphEdge[], order: (a: GraphEdge, b: GraphEdge) => number): GraphEdge[] {
  if (edges.length <= EDGE_DRAW_MAX) return edges;
  const dropped = new Set<string>();
  const firstPerMeeting = new Set<string>();
  const literal = edges.filter((edge) => edge.kind === "mentioned" && (edge.relation_id === undefined || edge.relation_id === null));
  const extraLiteral = [...literal].sort(order).filter((edge) => {
    if (firstPerMeeting.has(edge.from)) return true;
    firstPerMeeting.add(edge.from);
    return false;
  });
  const stages: GraphEdge[][] = [
    edges.filter((edge) => edge.kind === "related"),
    edges.filter((edge) => edgeStyleKind(edge.kind) === "discussion"),
    edges.filter((edge) => edge.kind === "mentioned" && edge.relation_id !== undefined && edge.relation_id !== null),
    extraLiteral,
  ];
  let count = edges.length;
  for (const stage of stages) {
    // 同一类里排在后面的（会议旧的）先去掉
    const victims = [...stage].sort(order).reverse();
    for (const edge of victims) {
      if (count <= EDGE_DRAW_MAX) break;
      if (dropped.has(edge.id)) continue;
      dropped.add(edge.id);
      count -= 1;
    }
    if (count <= EDGE_DRAW_MAX) break;
  }
  return edges.filter((edge) => !dropped.has(edge.id));
}

/** 画出来的线上的邻居：变暗的范围和看到的一致 */
export function drawnNeighbours(edges: GraphEdge[], id: string): Set<string> {
  const result = new Set<string>([id]);
  for (const edge of edges) {
    if (edge.from === id) result.add(edge.to);
    if (edge.to === id) result.add(edge.from);
  }
  return result;
}

/** 两端都在图上的线；［提到］关着时藏起提到线（文件节点和版面不动） */
export function drawableEdges(edges: GraphEdge[], has: (id: string) => boolean, hideMentions = false): GraphEdge[] {
  return edges.filter((edge) => has(edge.from) && has(edge.to) && !(hideMentions && edge.kind === "mentioned"));
}

/** 会议离今天几天：同级的线先取会议一端新的 */
export function meetingAges(meetings: Array<{ meeting_id: string; age_days: number }>): Map<string, number> {
  return new Map(meetings.map((meeting) => [meeting.meeting_id, meeting.age_days]));
}
