// 全部项目概览的布局：纯函数，同样的数据每次得到完全相同的坐标，和窗口大小无关，不存任何坐标。
//
// 左手是港湾（没归项目的会，和项目图「左手录音」一致），幽灵岛（像新项目的名字）贴在港湾下面；
// 中间是项目岛，按项目创建先后排在固定格子里，一行 4 个，新项目只占末尾的空格，不挤动别的岛；
// 右手是项目总文件夹下还没挂的文件夹（灰色岛）。文件夹走单独的接口、晚一步到，
// 它只影响自己那一列，别的岛不动。坐标单位是缩放为 1 时的像素，(x, y) 是节点中心。

import type { UnclaimedFolder } from "../../types";
import { overlaps, textWidth, type Box } from "./layout";
import type {
  GraphOverview,
  OverviewFolders,
  OverviewHarbour,
  OverviewIsland,
  SuggestedProject,
} from "./overviewTypes";

export type OverviewRegion = "harbour" | "islands" | "folders";

interface BaseNode {
  id: string;
  region: OverviewRegion;
  x: number;
  y: number;
  /** 节点连同外伸的字、琥珀点、按钮占的地方 */
  box: Box;
  /** 键盘和读屏用的一句话 */
  label: string;
}

export type OverviewNode =
  | (BaseNode & { kind: "island"; data: OverviewIsland; size: IslandSize; diameter: number; slot: number })
  | (BaseNode & { kind: "island_more"; data: NonNullable<GraphOverview["islands_more"]>; diameter: number })
  | (BaseNode & { kind: "empty" })
  | (BaseNode & { kind: "harbour"; data: OverviewHarbour })
  | (BaseNode & { kind: "ghost"; data: SuggestedProject; diameter: number })
  | (BaseNode & { kind: "ghost_more"; data: { count: number } })
  | (BaseNode & { kind: "folder"; data: UnclaimedFolder })
  | (BaseNode & { kind: "folder_more"; data: { count: number } })
  | (BaseNode & { kind: "folder_hint"; data: NonNullable<OverviewFolders["suggested"]> | null })
  | (BaseNode & { kind: "folder_status"; data: { text: string } });

export type OverviewNodeKind = OverviewNode["kind"];

/** 两个岛之间的短虚线，写「×2」；两头从圆边起 */
export interface OverviewBridgeLine {
  id: string;
  a: string;
  b: string;
  count: number;
  x1: number;
  y1: number;
  x2: number;
  y2: number;
}

export interface OverviewLayout {
  nodes: OverviewNode[];
  byId: Map<string, OverviewNode>;
  bridges: OverviewBridgeLine[];
  bounds: Box;
  /** 打开时要放进可见区的范围：港湾、项目岛、幽灵岛和文件夹 */
  focusBounds: Box;
}

/** 大小三档：窗口内 0 场、1–5 场、6 场以上 */
export type IslandSize = 0 | 1 | 2;

// ------------------------------------------------------------------ 几何常数

export const ISLAND_COLS = 4;
const CELL_W = 180;
const CELL_H = 150;
const ISLAND_D: Record<IslandSize, number> = { 0: 56, 1: 76, 2: 96 };
const MORE_D = 56;
/** 岛上的名字最宽这么多，超过就折行（最多两行） */
export const ISLAND_TEXT_MAX = 164;
const ISLAND_BOX_MAX_W = CELL_W - 12;
const EMPTY = { w: 300, h: 96 };

const HARBOUR = { x: -300, y: 0, w: 240, h: 116 };
export const GHOST_MAX = 6;
const GHOST_COLS = 3;
const GHOST = { dx: 124, dy: 156, w: 118, h: 144, d: 84, y0: 140 };
const GHOST_MORE = { w: 240, h: 26 };

export const FOLDER_MAX = 12;
const FOLDER_COLS = 2;
const FOLDER_GAP = 80;
const FOLDER = { dx: 128, dy: 100, w: 120, h: 86, y0: -37 };
const FOLDER_NOTE = { w: 250 };
const FOLDER_HINT_H = 116;
const FOLDER_STATUS_H = 56;

// ------------------------------------------------------------------ 小工具

export function islandSize(meetings: number): IslandSize {
  if (meetings <= 0) return 0;
  if (meetings <= 5) return 1;
  return 2;
}

/** 窗口内的会写成「28 天 7 场」，全部时写「全部 7 场」 */
export function islandCountText(days: number | null, meetings: number): string {
  return `${days ? `${days} 天` : "全部"} ${meetings} 场`;
}

/** 幽灵岛上的一句：「像是新项目『云图看板』· 2 场会」 */
export function ghostText(item: SuggestedProject): string {
  return `像是新项目『${item.name}』· ${item.meeting_count} 场会`;
}

/** 在等你的数：待复核的会＋门口的会＋待确认任务 */
export function waitingTotal(island: OverviewIsland): number {
  return island.waiting.review + island.waiting.doorstep + island.waiting.tasks;
}

/**
 * 文件夹节点的 id 用路径的短 hash，不把中文路径写进地址栏。FNV-1a 32 位，转成 36 进制。
 */
export function folderNodeId(path: string): string {
  let hash = 0x811c9dc5;
  for (let index = 0; index < path.length; index += 1) {
    hash ^= path.charCodeAt(index);
    hash = Math.imul(hash, 0x01000193) >>> 0;
  }
  return `fd:${hash.toString(36)}`;
}

export const ISLANDS_MORE_ID = "islands:more";
export const GHOSTS_MORE_ID = "ghosts:more";
export const FOLDERS_MORE_ID = "folders:more";
export const FOLDERS_HINT_ID = "folders:hint";
export const FOLDERS_STATUS_ID = "folders:status";
export const HARBOUR_ID = "harbour";
export const EMPTY_ID = "empty";

function centered(x: number, y: number, w: number, h: number): Box {
  return { x: x - w / 2, y: y - h / 2, w, h };
}

function unionBox(boxes: Box[]): Box {
  if (boxes.length === 0) return { x: 0, y: 0, w: 0, h: 0 };
  const left = Math.min(...boxes.map((box) => box.x));
  const top = Math.min(...boxes.map((box) => box.y));
  const right = Math.max(...boxes.map((box) => box.x + box.w));
  const bottom = Math.max(...boxes.map((box) => box.y + box.h));
  return { x: left, y: top, w: right - left, h: bottom - top };
}

/** 格子里第 slot 个的中心：一行 ISLAND_COLS 个，从左往右、从上往下 */
function cellCenter(slot: number) {
  return { x: (slot % ISLAND_COLS) * CELL_W, y: Math.floor(slot / ISLAND_COLS) * CELL_H };
}

/** 名字最多折成两行时占的宽和行数 */
function nameLines(name: string) {
  const width = textWidth(name, 13);
  return { width: Math.min(width, ISLAND_TEXT_MAX), lines: width > ISLAND_TEXT_MAX ? 2 : 1 };
}

function islandBox(x: number, y: number, diameter: number, name: string, sub: string): Box {
  const text = nameLines(name);
  const textW = Math.max(text.width, textWidth(sub, 11.5));
  const textH = text.lines * 17 + 16;
  // 右上角的琥珀点伸出圆外一点
  const w = Math.min(ISLAND_BOX_MAX_W, Math.max(diameter + 16, textW + 16));
  const h = Math.max(diameter + 16, textH + 12);
  return centered(x, y, w, h);
}

// ------------------------------------------------------------------ 主函数

/**
 * folders 为 null 表示没认领的文件夹还没取到：右手那一列先空着，到了只加节点，别的岛不动。
 */
export function layoutOverview(overview: GraphOverview, folders: OverviewFolders | null): OverviewLayout {
  const nodes: OverviewNode[] = [];
  const add = (node: OverviewNode) => nodes.push(node);

  // 港湾：左侧固定位置
  const harbour = overview.harbour;
  add({
    id: HARBOUR_ID,
    kind: "harbour",
    region: "harbour",
    x: HARBOUR.x,
    y: HARBOUR.y,
    box: centered(HARBOUR.x, HARBOUR.y, HARBOUR.w, HARBOUR.h),
    label: harbour.ai_configured
      ? `港湾：${harbour.total} 场没归项目的会`
      : `港湾：没配置 AI，${harbour.total} 场会要你自己选项目`,
    data: harbour,
  });

  // 幽灵岛：贴着港湾，最多 6 个，一行 3 个
  const ghosts = overview.suggested_projects.slice(0, GHOST_MAX);
  ghosts.forEach((item, index) => {
    const x = HARBOUR.x + ((index % GHOST_COLS) - (GHOST_COLS - 1) / 2) * GHOST.dx;
    const y = GHOST.y0 + Math.floor(index / GHOST_COLS) * GHOST.dy;
    add({
      id: `np:${item.key}`,
      kind: "ghost",
      region: "harbour",
      x,
      y,
      box: centered(x, y, GHOST.w, GHOST.h),
      label: ghostText(item),
      data: item,
      diameter: GHOST.d,
    });
  });
  const ghostRest = overview.suggested_projects.length - ghosts.length;
  if (ghostRest > 0) {
    const rows = Math.ceil(ghosts.length / GHOST_COLS);
    const y = GHOST.y0 + (rows - 1) * GHOST.dy + GHOST.h / 2 + 8 + GHOST_MORE.h / 2;
    add({
      id: GHOSTS_MORE_ID,
      kind: "ghost_more",
      region: "harbour",
      x: HARBOUR.x,
      y,
      box: centered(HARBOUR.x, y, GHOST_MORE.w, GHOST_MORE.h),
      label: `还有 ${ghostRest} 个像新项目的名字`,
      data: { count: ghostRest },
    });
  }

  // 项目岛：按创建先后落在固定格子里
  const days = overview.window.days;
  overview.islands.forEach((island, slot) => {
    const { x, y } = cellCenter(slot);
    const size = islandSize(island.meetings);
    const diameter = ISLAND_D[size];
    const sub = islandCountText(days, island.meetings);
    const waiting = waitingTotal(island);
    add({
      id: `p:${island.id}`,
      kind: "island",
      region: "islands",
      x,
      y,
      box: islandBox(x, y, diameter, island.name, sub),
      label: [
        `项目：${island.name}，${sub}`,
        waiting ? `${waiting} 件在等你` : "",
        island.stopped_cards ? `${island.stopped_cards} 张卡片停了` : "",
      ]
        .filter(Boolean)
        .join("，"),
      data: island,
      size,
      diameter,
      slot,
    });
  });
  if (overview.islands_more) {
    const { x, y } = cellCenter(overview.islands.length);
    const text = `其余 ${overview.islands_more.count} 个项目`;
    add({
      id: ISLANDS_MORE_ID,
      kind: "island_more",
      region: "islands",
      x,
      y,
      box: islandBox(x, y, MORE_D, text, ""),
      label: `${text}（窗口内没有会）`,
      data: overview.islands_more,
      diameter: MORE_D,
    });
  }
  if (overview.islands.length === 0 && !overview.islands_more) {
    const x = EMPTY.w / 2 - ISLAND_BOX_MAX_W / 2;
    add({
      id: EMPTY_ID,
      kind: "empty",
      region: "islands",
      x,
      y: 0,
      box: centered(x, 0, EMPTY.w, EMPTY.h),
      label: "还没有项目",
    });
  }

  // 没认领的文件夹：项目岛右边一列，只看项目岛占到哪儿
  const islandRight = Math.max(
    ...nodes.filter((node) => node.region === "islands").map((node) => node.box.x + node.box.w),
  );
  const folderX0 = islandRight + FOLDER_GAP;
  if (folders) {
    const shown = folders.folders.slice(0, FOLDER_MAX);
    const folderAt = (slot: number) => ({
      x: folderX0 + FOLDER.w / 2 + (slot % FOLDER_COLS) * FOLDER.dx,
      y: FOLDER.y0 + Math.floor(slot / FOLDER_COLS) * FOLDER.dy,
    });
    const seen = new Set<string>();
    shown.forEach((folder, slot) => {
      const { x, y } = folderAt(slot);
      let id = folderNodeId(folder.path);
      // hash 撞了（几乎不会）：后一个加序号，保证 id 唯一
      if (seen.has(id)) id = `${id}-${slot}`;
      seen.add(id);
      add({
        id,
        kind: "folder",
        region: "folders",
        x,
        y,
        box: centered(x, y, FOLDER.w, FOLDER.h),
        label: `没挂到项目的文件夹：${folder.name}`,
        data: folder,
      });
    });
    const rest = folders.more + Math.max(0, folders.folders.length - shown.length);
    if (rest > 0) {
      const { x, y } = folderAt(shown.length);
      add({
        id: FOLDERS_MORE_ID,
        kind: "folder_more",
        region: "folders",
        x,
        y,
        box: centered(x, y, FOLDER.w, 28),
        label: `还有 ${rest} 个文件夹没挂到项目`,
        data: { count: rest },
      });
    }
    // 一个文件夹都没列出来时，这一列写一句话：没设总文件夹是提示岛，别的是状态
    const noteX = folderX0 + FOLDER_NOTE.w / 2;
    const status = shown.length === 0 && folders.state !== "unset" ? folderStatusText(folders) : "";
    if (shown.length === 0 && folders.state === "unset") {
      add({
        id: FOLDERS_HINT_ID,
        kind: "folder_hint",
        region: "folders",
        x: noteX,
        y: 0,
        box: centered(noteX, 0, FOLDER_NOTE.w, FOLDER_HINT_H),
        label: "设项目总文件夹后，这里会列出还没挂的文件夹",
        data: folders.suggested,
      });
    } else if (status) {
      add({
        id: FOLDERS_STATUS_ID,
        kind: "folder_status",
        region: "folders",
        x: noteX,
        y: 0,
        box: centered(noteX, 0, FOLDER_NOTE.w, FOLDER_STATUS_H),
        label: status,
        data: { text: status },
      });
    }
  }

  // 跨项目的线：两头的岛都画出来时才画，从圆边起
  const byId = new Map(nodes.map((node) => [node.id, node]));
  const bridges: OverviewBridgeLine[] = [];
  for (const bridge of overview.bridges) {
    const a = byId.get(`p:${bridge.a}`);
    const b = byId.get(`p:${bridge.b}`);
    if (a?.kind !== "island" || b?.kind !== "island") continue;
    const dx = b.x - a.x;
    const dy = b.y - a.y;
    const length = Math.hypot(dx, dy) || 1;
    const ra = a.diameter / 2 + 4;
    const rb = b.diameter / 2 + 4;
    bridges.push({
      id: `br:${bridge.a}:${bridge.b}`,
      a: a.id,
      b: b.id,
      count: bridge.count,
      x1: a.x + (dx / length) * ra,
      y1: a.y + (dy / length) * ra,
      x2: b.x - (dx / length) * rb,
      y2: b.y - (dy / length) * rb,
    });
  }

  return {
    nodes,
    byId,
    bridges,
    bounds: unionBox(nodes.map((node) => node.box)),
    focusBounds: unionBox(nodes.map((node) => node.box)),
  };
}

/** 文件夹那一列没有文件夹时的一句话；有文件夹或已经列完时不说 */
export function folderStatusText(folders: OverviewFolders): string {
  switch (folders.state) {
    case "checking":
      return "正在看项目总文件夹下面有哪些文件夹…";
    case "offline":
      if (folders.parent?.state === "missing") return "找不到项目总文件夹了";
      if (folders.parent?.state === "unreadable") return "读不了项目总文件夹";
      return "资料盘未连接，插上后再列下面的文件夹";
    case "conflict":
      return folders.parent?.reason ?? "项目总文件夹现在用不了";
    default:
      return "";
  }
}

/**
 * N 键的顺序：有在等你的或卡片停了的项目岛（按格子顺序），港湾里有待你选、像新项目的会时是港湾，再是幽灵岛。
 */
export function overviewAttention(layout: OverviewLayout): string[] {
  const islands = layout.nodes.filter(
    (node): node is Extract<OverviewNode, { kind: "island" }> =>
      node.kind === "island" && (waitingTotal(node.data) > 0 || node.data.stopped_cards > 0),
  );
  const harbour = layout.nodes.filter(
    (node) => node.kind === "harbour" && node.data.counts.needs_review + node.data.counts.new_project > 0,
  );
  const ghosts = layout.nodes.filter((node) => node.kind === "ghost");
  return [...islands, ...harbour, ...ghosts].map((node) => node.id);
}

/** 按屏幕方向找最近的节点：方向键用（和项目图同一套打分）。 */
export function nearestOverviewNode(
  nodes: OverviewNode[],
  from: OverviewNode,
  key: "ArrowUp" | "ArrowDown" | "ArrowLeft" | "ArrowRight",
): OverviewNode | null {
  let best: OverviewNode | null = null;
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

/** 测试和调试用：有没有两个节点的框相交 */
export function overlappingPairs(layout: OverviewLayout): Array<[string, string]> {
  const pairs: Array<[string, string]> = [];
  layout.nodes.forEach((node, index) => {
    for (const other of layout.nodes.slice(index + 1)) {
      if (overlaps(node.box, other.box)) pairs.push([node.id, other.id]);
    }
  });
  return pairs;
}

export const OVERVIEW_REGION_NAMES: Record<OverviewRegion, string> = {
  harbour: "港湾",
  islands: "项目",
  folders: "没挂到项目的文件夹",
};

export const OVERVIEW_REGION_ORDER: OverviewRegion[] = ["harbour", "islands", "folders"];
