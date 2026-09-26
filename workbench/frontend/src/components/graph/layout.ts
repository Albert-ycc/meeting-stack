// 关系图星图布局：纯函数，同样的数据每次得到完全相同的坐标，和窗口大小无关，不存任何坐标。
//
// 方向是类型，远近是新旧：会议在左、材料在右、进行中的需求在上、线索词在下；内圈最近 7 天、
// 中圈最近 28 天、外圈更早。整体是横向拉宽的椭圆。同一圈、同一方向用固定槽位：
// 会议按天数落槽（今天在最上面），多一场会只占一个空槽，不会把别的节点挤走；
// 需求、材料按顺序从中间往两边填。坐标单位是缩放为 1 时的像素。

import type {
  GraphBeacon,
  GraphCollapsed,
  GraphCue,
  GraphDoorstep,
  GraphFolder,
  GraphMeeting,
  GraphMovedOut,
  GraphPayload,
  GraphRequirement,
  Ring,
} from "./graphTypes";

export type NodeKind =
  | "project"
  | "meeting"
  | "collapsed"
  | "doorstep"
  | "doorstep_more"
  | "requirement"
  | "requirement_more"
  | "folder"
  | "folder_more"
  | "loose"
  | "cue"
  | "beacon"
  | "ghost";

export type Direction = "center" | "left" | "right" | "top" | "bottom";

export interface Box {
  x: number;
  y: number;
  w: number;
  h: number;
}

interface BaseNode {
  id: string;
  direction: Direction;
  ring: Ring | "center" | "door";
  /** 圆点（或节点中心）的位置 */
  x: number;
  y: number;
  /** 节点连同外伸标签占的地方 */
  box: Box;
  /** 键盘和读屏用的一句话 */
  label: string;
}

export type LaidNode =
  | (BaseNode & { kind: "project"; data: GraphPayload["project"] })
  | (BaseNode & { kind: "meeting"; data: GraphMeeting; text: string })
  | (BaseNode & { kind: "collapsed"; data: GraphCollapsed })
  | (BaseNode & { kind: "doorstep"; data: GraphDoorstep })
  | (BaseNode & { kind: "doorstep_more"; data: { count: number } })
  | (BaseNode & { kind: "requirement"; data: GraphRequirement })
  | (BaseNode & { kind: "requirement_more"; data: NonNullable<GraphPayload["requirements_more"]> })
  | (BaseNode & { kind: "folder"; data: GraphFolder })
  | (BaseNode & { kind: "folder_more"; data: NonNullable<GraphPayload["folders_more"]> })
  | (BaseNode & { kind: "loose"; data: NonNullable<GraphPayload["loose"]> })
  | (BaseNode & { kind: "cue"; data: GraphCue; fontSize: number })
  | (BaseNode & { kind: "beacon"; data: GraphBeacon })
  | (BaseNode & { kind: "ghost"; data: GraphMovedOut; text: string });

export interface RingGuide {
  name: string;
  rx: number;
  ry: number;
  hint: string;
}

export interface StarLayout {
  nodes: LaidNode[];
  byId: Map<string, LaidNode>;
  rings: RingGuide[];
  /** 全部节点的范围 */
  bounds: Box;
  /** 打开时要放进可见区的范围：中心、内圈、中圈、门口和线索词 */
  focusBounds: Box;
}

// ------------------------------------------------------------------ 几何常数

export const MEETING_TITLE_MAX = 170;
export const NODE_H = 24;
const DOT_GAP = 12;
const COLUMN_R: Record<Ring, number> = { inner: 150, middle: 370, outer: 610 };
const INNER_SLOTS = 8;
const MIDDLE_SLOTS = 10;
const MEETING_GAP = 38;
const COLLAPSED_SLOTS = 7;
const COLLAPSED_GAP = 50;
const MATERIAL_SLOTS = 8;
const REQUIREMENT_ROW_Y: Record<Ring, number> = { inner: -210, middle: -270, outer: -330 };
const REQUIREMENT_SLOTS_X = [-90, 90, -270, 270];
export const REQUIREMENT_W = 170;
const REQUIREMENT_H = 36;
const CUE_Y = 235;
const CUE_GAP = 18;
const CUE_MIN_X = -170;
const DOOR = { x: -330, y: 250, dy: 72, w: 280, h: 60 };
const BEACON = { y: 305, x0: 160, dx: 190, w: 170, h: 28 };
const PROJECT = { w: 140, h: 88 };

export const RING_GUIDES: RingGuide[] = [
  { name: "7 天", rx: 250, ry: 200, hint: "这一圈是最近 7 天" },
  { name: "28 天", rx: 470, ry: 262, hint: "这一圈是最近 28 天" },
];

const RING_ORDER: Ring[] = ["inner", "middle", "outer"];

// ------------------------------------------------------------------ 文字宽度

/** 估算一行字的宽度：中文按一个字号宽，英文数字按 0.55 个字号。只用来排版，不求精确。 */
export function textWidth(text: string, fontSize = 13): number {
  let width = 0;
  for (const char of text) {
    const code = char.codePointAt(0) ?? 0;
    width += code > 0x2e80 ? fontSize : fontSize * 0.56;
  }
  return Math.ceil(width);
}

/** 按宽度截断，末尾加省略号。 */
export function fitText(text: string, maxWidth: number, fontSize = 13): string {
  if (textWidth(text, fontSize) <= maxWidth) return text;
  let result = "";
  for (const char of text) {
    if (textWidth(`${result}${char}…`, fontSize) > maxWidth) break;
    result += char;
  }
  return `${result}…`;
}

/** 会议节点上的日期：今天写「今天」，其余写「9/24」。 */
export function meetingDateLabel(date: string, today: string): string {
  if (date === today) return "今天";
  const [, month, day] = date.split("-");
  return `${Number(month)}/${Number(day)}`;
}

// ------------------------------------------------------------------ 槽位

/** 横向拉宽的弧：越靠上下越往中间收。 */
function arcX(ring: Ring, y: number, side: 1 | -1): number {
  const r = COLUMN_R[ring];
  return side * Math.sqrt(Math.max(r * r - y * y, (r * 0.45) ** 2));
}

function ladder(count: number, gap: number): number[] {
  return Array.from({ length: count }, (_, index) => (index - (count - 1) / 2) * gap);
}

/** 从中间往两边：0、上 1、下 1、上 2…… */
function centerOut(count: number, gap: number): number[] {
  return Array.from({ length: count }, (_, index) => {
    const step = Math.ceil(index / 2);
    return (index % 2 === 1 ? -1 : 1) * step * gap;
  });
}

/** 先占理想槽位，被占了就往下找，再往上找。 */
function claim(taken: boolean[], ideal: number): number {
  const size = taken.length;
  const start = Math.min(size - 1, Math.max(0, ideal));
  for (let offset = 0; offset < size; offset += 1) {
    for (const candidate of [start + offset, start - offset]) {
      if (candidate >= 0 && candidate < size && !taken[candidate]) {
        taken[candidate] = true;
        return candidate;
      }
    }
  }
  return -1;
}

function idealMeetingSlot(meeting: { ring: Ring; age_days: number }): number {
  if (meeting.ring === "inner") return Math.min(INNER_SLOTS - 1, meeting.age_days);
  if (meeting.age_days < 7) return 0;
  return Math.floor((Math.min(meeting.age_days, 27) - 7) / 2.1);
}

/** 残影的圈：和它还在时一样按天数算；时间窗外或更早的会本来就折叠了，不画残影。 */
function ghostRing(item: GraphMovedOut, windowDays: number | null): "inner" | "middle" | null {
  if (windowDays !== null && item.age_days >= windowDays) return null;
  if (item.age_days < 7) return "inner";
  if (item.age_days < 28) return "middle";
  return null;
}

/** 残影两行：上面是原来的日期和标题（划掉），下面小字「已改到 数据中台 · 点一下撤销」 */
export function ghostText(item: GraphMovedOut, today: string): string {
  return fitText(`${meetingDateLabel(item.date, today)} ${item.title}`, MEETING_TITLE_MAX);
}

export function ghostNote(item: GraphMovedOut): string {
  return fitText(`已改到 ${item.to_project_name ?? "不归项目"} · 点一下撤销`, MEETING_TITLE_MAX, 11);
}

const GHOST_H = 36;

function unionBox(boxes: Box[]): Box {
  if (boxes.length === 0) return { x: 0, y: 0, w: 0, h: 0 };
  const left = Math.min(...boxes.map((box) => box.x));
  const top = Math.min(...boxes.map((box) => box.y));
  const right = Math.max(...boxes.map((box) => box.x + box.w));
  const bottom = Math.max(...boxes.map((box) => box.y + box.h));
  return { x: left, y: top, w: right - left, h: bottom - top };
}

function leftLabelBox(x: number, y: number, width: number): Box {
  return { x: x - DOT_GAP - width, y: y - NODE_H / 2, w: width + DOT_GAP + 6, h: NODE_H };
}

function rightLabelBox(x: number, y: number, width: number): Box {
  return { x: x - 6, y: y - NODE_H / 2, w: width + DOT_GAP + 6, h: NODE_H };
}

function cueFontSize(total: number): number {
  if (total >= 10) return 15;
  if (total >= 5) return 13.5;
  return 12;
}

// ------------------------------------------------------------------ 主函数

export function layoutStarMap(graph: GraphPayload): StarLayout {
  const nodes: LaidNode[] = [];
  const add = (node: LaidNode) => nodes.push(node);

  add({
    id: "project",
    kind: "project",
    direction: "center",
    ring: "center",
    x: 0,
    y: 0,
    box: { x: -PROJECT.w / 2, y: -PROJECT.h / 2, w: PROJECT.w, h: PROJECT.h },
    label: `${graph.project.name}，${graph.project.meeting_count} 场会`,
    data: graph.project,
  });

  // 会议：内圈 8 个槽、中圈 10 个槽，按天数落槽
  const innerY = ladder(INNER_SLOTS, MEETING_GAP);
  const middleY = ladder(MIDDLE_SLOTS, MEETING_GAP);
  const taken: Record<"inner" | "middle", boolean[]> = {
    inner: Array(INNER_SLOTS).fill(false),
    middle: Array(MIDDLE_SLOTS).fill(false),
  };
  // 改走的会留下的残影占着原来的槽位，直到撤销期过去：和会一起按天数落槽，别的会不挪
  type Slotted = { kind: "meeting"; item: GraphMeeting; ring: "inner" | "middle"; order: number }
    | { kind: "ghost"; item: GraphMovedOut; ring: "inner" | "middle"; order: number };
  const slotted: Slotted[] = graph.meetings.map((item, order) => ({
    kind: "meeting" as const,
    item,
    ring: item.ring === "inner" ? ("inner" as const) : ("middle" as const),
    order,
  }));
  (graph.moved_out ?? []).forEach((item, index) => {
    const ring = ghostRing(item, graph.window.days);
    if (ring) slotted.push({ kind: "ghost", item, ring, order: graph.meetings.length + index });
  });
  // 同一天的按会议 id 倒排（id 里带录音时间，越晚越靠上）：残影不知道自己原来排第几，靠 id 找回同一个槽
  slotted.sort(
    (a, b) =>
      a.item.age_days - b.item.age_days ||
      (a.item.meeting_id < b.item.meeting_id ? 1 : a.item.meeting_id > b.item.meeting_id ? -1 : a.order - b.order),
  );
  for (const entry of slotted) {
    const ring = entry.ring;
    const slot = claim(taken[ring], idealMeetingSlot({ ring, age_days: entry.item.age_days }));
    if (slot < 0) continue;
    const y = (ring === "inner" ? innerY : middleY)[slot];
    const x = arcX(ring, y, -1);
    if (entry.kind === "ghost") {
      const ghost = entry.item;
      const text = ghostText(ghost, graph.today);
      add({
        id: `g:${ghost.meeting_id}`,
        kind: "ghost",
        direction: "left",
        ring,
        x,
        y,
        box: {
          ...leftLabelBox(x, y, Math.max(textWidth(text), textWidth(ghostNote(ghost), 11))),
          y: y - GHOST_H / 2,
          h: GHOST_H,
        },
        label: `刚改到 ${ghost.to_project_name ?? "不归项目"} 的会：${ghost.title}，点一下撤销`,
        data: ghost,
        text,
      });
      continue;
    }
    const meeting = entry.item;
    const text = fitText(
      `${meetingDateLabel(meeting.date, graph.today)} ${meeting.title}`,
      MEETING_TITLE_MAX,
    );
    add({
      id: meeting.id,
      kind: "meeting",
      direction: "left",
      ring,
      x,
      y,
      box: leftLabelBox(x, y, textWidth(text)),
      label: `会议：${meeting.title}，${meetingDateLabel(meeting.date, graph.today)}`,
      data: meeting,
      text,
    });
  }

  // 折叠：会议那一侧的外圈，从上往下
  const collapsedY = ladder(COLLAPSED_SLOTS, COLLAPSED_GAP);
  graph.collapsed.slice(0, COLLAPSED_SLOTS).forEach((group, index) => {
    const y = collapsedY[index];
    const x = arcX("outer", y, -1);
    const width = Math.max(textWidth(group.label), textWidth(`${group.from} – ${group.to}`, 11));
    add({
      id: group.id,
      kind: "collapsed",
      direction: "left",
      ring: "outer",
      x,
      y,
      box: { ...leftLabelBox(x, y, width), y: y - 20, h: 40 },
      label: `${group.label}，${group.from} 到 ${group.to}`,
      data: group,
    });
  });

  // 门口：会议那一侧最外面的下方
  graph.doorstep.forEach((item, index) => {
    const y = DOOR.y + index * DOOR.dy;
    add({
      id: item.id,
      kind: "doorstep",
      direction: "left",
      ring: "door",
      x: DOOR.x,
      y,
      box: { x: DOOR.x - DOOR.w / 2, y: y - DOOR.h / 2, w: DOOR.w, h: DOOR.h },
      label: `可能是这个项目的会：${item.title}`,
      data: item,
    });
  });
  if (graph.doorstep_more > 0) {
    const y = DOOR.y + graph.doorstep.length * DOOR.dy - 8;
    add({
      id: "d:more",
      kind: "doorstep_more",
      direction: "left",
      ring: "door",
      x: DOOR.x,
      y,
      box: { x: DOOR.x - DOOR.w / 2, y: y - 12, w: DOOR.w, h: 24 },
      label: `还有 ${graph.doorstep_more} 场可能是这个项目的`,
      data: { count: graph.doorstep_more },
    });
  }

  // 需求：每一圈一排，一排 4 个，满了顺延到外一排
  const rows: Record<Ring, number> = { inner: 0, middle: 0, outer: 0 };
  const placeRequirement = (preferred: Ring): { ring: Ring; slot: number } | null => {
    for (const ring of RING_ORDER.slice(RING_ORDER.indexOf(preferred))) {
      if (rows[ring] < REQUIREMENT_SLOTS_X.length) {
        rows[ring] += 1;
        return { ring, slot: rows[ring] - 1 };
      }
    }
    return null;
  };
  for (const requirement of graph.requirements) {
    const place = placeRequirement(requirement.stale ? "outer" : requirement.ring);
    if (place === null) continue;
    const x = REQUIREMENT_SLOTS_X[place.slot];
    const y = REQUIREMENT_ROW_Y[place.ring];
    add({
      id: requirement.id,
      kind: "requirement",
      direction: "top",
      ring: place.ring,
      x,
      y,
      box: { x: x - REQUIREMENT_W / 2, y: y - REQUIREMENT_H / 2, w: REQUIREMENT_W, h: REQUIREMENT_H },
      label: `需求：${requirement.title}，${requirement.priority}`,
      data: requirement,
    });
  }
  if (graph.requirements_more) {
    const place = placeRequirement("outer");
    if (place !== null) {
      const x = REQUIREMENT_SLOTS_X[place.slot];
      const y = REQUIREMENT_ROW_Y[place.ring];
      add({
        id: "r:more",
        kind: "requirement_more",
        direction: "top",
        ring: place.ring,
        x,
        y,
        box: { x: x - REQUIREMENT_W / 2, y: y - REQUIREMENT_H / 2, w: REQUIREMENT_W, h: REQUIREMENT_H },
        label: `其余 ${graph.requirements_more.count} 个需求`,
        data: graph.requirements_more,
      });
    }
  }

  // 材料：项目文件夹和卡片文件夹靠内，需求文件夹在中圈，散放文件和「其余」在外圈
  const materialY = centerOut(MATERIAL_SLOTS, MEETING_GAP);
  const materialCount: Record<Ring, number> = { inner: 0, middle: 0, outer: 0 };
  const placeMaterial = (ring: Ring) => {
    for (const candidate of RING_ORDER.slice(RING_ORDER.indexOf(ring))) {
      if (materialCount[candidate] < MATERIAL_SLOTS) {
        const y = materialY[materialCount[candidate]];
        materialCount[candidate] += 1;
        return { ring: candidate, y, x: arcX(candidate, y, 1) };
      }
    }
    return null;
  };
  for (const folder of graph.folders) {
    const place = placeMaterial(folder.ring);
    if (place === null) continue;
    const text = fitText(folder.kind === "cards" ? folder.name : `${folder.name}/`, MEETING_TITLE_MAX);
    add({
      id: folder.id,
      kind: "folder",
      direction: "right",
      ring: place.ring,
      x: place.x,
      y: place.y,
      box: rightLabelBox(place.x, place.y, textWidth(text) + (folder.kind === "cards" ? 70 : 0)),
      label: `文件夹：${folder.name}`,
      data: folder,
    });
  }
  const outerMaterials: Array<() => void> = [];
  if (graph.loose) {
    const loose = graph.loose;
    outerMaterials.push(() => {
      const place = placeMaterial("outer");
      if (place === null) return;
      add({
        id: loose.id,
        kind: "loose",
        direction: "right",
        ring: place.ring,
        x: place.x,
        y: place.y,
        box: rightLabelBox(place.x, place.y, textWidth("散放 000 个")),
        label: "根目录里散放的文件",
        data: loose,
      });
    });
  }
  if (graph.folders_more) {
    const more = graph.folders_more;
    outerMaterials.push(() => {
      const place = placeMaterial("outer");
      if (place === null) return;
      add({
        id: more.id,
        kind: "folder_more",
        direction: "right",
        ring: place.ring,
        x: place.x,
        y: place.y,
        box: rightLabelBox(place.x, place.y, textWidth(`其余 ${more.count} 个文件夹`)),
        label: `其余 ${more.count} 个文件夹`,
        data: more,
      });
    });
  }
  outerMaterials.forEach((place) => place());

  // 线索词：下方一排，次数多的在前，字号三档
  const sizes = graph.cues.map((cue) => cueFontSize(cue.total));
  const widths = graph.cues.map((cue, index) => textWidth(cue.text, sizes[index]) + 12);
  const total = widths.reduce((sum, width) => sum + width, 0) + Math.max(0, graph.cues.length - 1) * CUE_GAP;
  let cursor = Math.max(CUE_MIN_X, -total / 2);
  graph.cues.forEach((cue, index) => {
    const width = widths[index];
    const x = cursor + width / 2;
    cursor += width + CUE_GAP;
    add({
      id: cue.id,
      kind: "cue",
      direction: "bottom",
      ring: "inner",
      x,
      y: CUE_Y,
      box: { x: x - width / 2, y: CUE_Y - 11, w: width, h: 22 },
      label: `线索词：${cue.text}，${cue.total} 次`,
      data: cue,
      fontSize: sizes[index],
    });
  });

  // 跨项目信标：贴在下边
  graph.beacons.forEach((beacon, index) => {
    const x = BEACON.x0 + index * BEACON.dx;
    add({
      id: beacon.id,
      kind: "beacon",
      direction: "bottom",
      ring: "outer",
      x,
      y: BEACON.y,
      box: { x: x - BEACON.w / 2, y: BEACON.y - BEACON.h / 2, w: BEACON.w, h: BEACON.h },
      label: beacon.label,
      data: beacon,
    });
  });

  const byId = new Map(nodes.map((node) => [node.id, node]));
  const focus = nodes.filter(
    (node) =>
      node.kind === "project" ||
      node.kind === "cue" ||
      node.ring === "door" ||
      ((node.ring === "inner" || node.ring === "middle") && node.kind !== "beacon"),
  );
  return {
    nodes,
    byId,
    rings: RING_GUIDES,
    bounds: unionBox(nodes.map((node) => node.box)),
    focusBounds: unionBox(focus.map((node) => node.box)),
  };
}

/**
 * N 键的顺序：当前画布里要你处理的节点，门口的会、待复核的会、有待确认任务的会、卡片停了的会、
 * 有待确认任务的需求，各自从上往下（需求从左往右）。不跨项目，不排成队列，只是依次跳。
 */
export function attentionOrder(layout: StarLayout): string[] {
  const byY = (a: LaidNode, b: LaidNode) => a.y - b.y || a.x - b.x;
  const doorstep = layout.nodes.filter((node) => node.kind === "doorstep").sort(byY);
  const meetings = layout.nodes
    .filter(
      (node): node is Extract<LaidNode, { kind: "meeting" }> =>
        node.kind === "meeting" &&
        (node.data.state === "needs_review" || node.data.pending_tasks > 0 || node.data.card === "stopped"),
    )
    .sort((a, b) => a.x - b.x || a.y - b.y);
  const requirements = layout.nodes
    .filter((node) => node.kind === "requirement" && node.data.pending_tasks > 0)
    .sort((a, b) => a.y - b.y || a.x - b.x);
  return [...doorstep, ...meetings, ...requirements].map((node) => node.id);
}

/** 两个框是否相交（贴边不算）。 */
export function overlaps(a: Box, b: Box): boolean {
  return a.x < b.x + b.w && b.x < a.x + a.w && a.y < b.y + b.h && b.y < a.y + a.h;
}

/** 按屏幕方向找最近的节点：方向键用。 */
export function nearestInDirection(
  nodes: LaidNode[],
  from: LaidNode,
  key: "ArrowUp" | "ArrowDown" | "ArrowLeft" | "ArrowRight",
): LaidNode | null {
  let best: LaidNode | null = null;
  let bestScore = Number.POSITIVE_INFINITY;
  for (const node of nodes) {
    if (node.id === from.id) continue;
    const dx = node.x - from.x;
    const dy = node.y - from.y;
    const along = key === "ArrowUp" ? -dy : key === "ArrowDown" ? dy : key === "ArrowLeft" ? -dx : dx;
    if (along <= 1) continue;
    const across = key === "ArrowUp" || key === "ArrowDown" ? Math.abs(dx) : Math.abs(dy);
    const score = along + across * 2;
    if (score < bestScore) {
      bestScore = score;
      best = node;
    }
  }
  return best;
}

export const DIRECTION_ORDER: Direction[] = ["center", "left", "top", "right", "bottom"];
export const DIRECTION_NAMES: Record<Direction, string> = {
  center: "项目",
  left: "会议",
  top: "需求",
  right: "材料",
  bottom: "线索词",
};
