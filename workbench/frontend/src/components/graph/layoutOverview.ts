// 全部项目概览（星图）的分圈与排位：纯函数，同样的数据每次得到完全相同的结果，和舞台大小、视角无关。
//
// 太阳在正中，项目按最近一次会离今天多久落在三圈轨道上（0–6 天、7–27 天、28 天以上或没开过会，和项目图的环、
// 时间窗同一个口径）。同一圈里最近开过会的放在正前方（离镜头最近），往两侧交替、逐个变旧，正后方留一个空档写圈名。
// 角度只看最近一次会，不看时间窗：换时间窗只换大小，行星不挪位置。
// 最外圈多于 12 个（连同服务器折起来的 islands_more；只要折了就算）时变成小行星带：没有在等你的项目变成带子里的粒子，
// 沿轨道带状散开；有在等你的仍画成行星、带名字。坐标单位是缩放为 1 时的像素，投影见 overviewProjection.ts。
//
// 港湾（没归项目的会）、像新项目的名字、还没挂的文件夹不在盘面上，是舞台左上、右上两个角上的读数块，这里只给节点。

import type { UnclaimedFolder } from "../../types";
import { DEG, TAU } from "./overviewProjection";
import type {
  GraphOverview,
  OverviewFolders,
  OverviewHarbour,
  OverviewIsland,
  SuggestedProject,
} from "./overviewTypes";

export type OverviewRegion = "harbour" | "islands" | "folders";
export type RingIndex = 0 | 1 | 2;

interface BaseNode {
  id: string;
  region: OverviewRegion;
  /** 键盘和读屏用的一句话 */
  label: string;
}

export interface IslandNode extends BaseNode {
  kind: "island";
  data: OverviewIsland;
  ring: RingIndex;
  /** 最近一次会是几天前，没开过会为 null */
  age: number | null;
  /** 在这一圈里的名次：0 是最近开过会的那个 */
  rank: number;
  /** 轨道角（弧度）：π/2 是正前方 */
  theta: number;
  /** 小行星带里的径向抖动（世界单位），行星为 0 */
  rj: number;
  /** 是不是小行星带里的粒子 */
  belt: boolean;
  waiting: number;
}

/** 小行星带：最外圈全部项目（按最近一次会排）+ 服务器折起来的项目 id */
export interface BeltNode extends BaseNode {
  kind: "belt";
  data: { projects: OverviewIsland[]; folded: string[]; count: number };
}

export type OverviewNode =
  | IslandNode
  | BeltNode
  | (BaseNode & { kind: "harbour"; data: OverviewHarbour })
  | (BaseNode & { kind: "ghost"; data: SuggestedProject })
  | (BaseNode & { kind: "ghost_more"; data: { count: number } })
  | (BaseNode & { kind: "folder"; data: UnclaimedFolder })
  | (BaseNode & { kind: "folder_more"; data: { count: number } })
  | (BaseNode & { kind: "folder_hint"; data: NonNullable<OverviewFolders["suggested"]> | null })
  | (BaseNode & { kind: "folder_status"; data: { text: string } });

/** 一圈轨道：R 是半径（世界单位），head 是圈头（最新的那颗）所在的角，chipAt 是圈名相对圈头的角 */
export interface OverviewRing {
  index: RingIndex;
  name: string;
  R: number;
  head: number;
  chipAt: number;
  /** 这一圈的项目数（小行星带时含折起来的） */
  count: number;
  belt: boolean;
}

/** 两个项目之间的跨项目关联，两头都在图上才有 */
export interface OverviewBridgeLine {
  id: string;
  a: string;
  b: string;
  count: number;
}

export interface OverviewLayout {
  nodes: OverviewNode[];
  byId: Map<string, OverviewNode>;
  /** 由里圈往外、同一圈按名次 */
  islands: IslandNode[];
  rings: OverviewRing[];
  belt: BeltNode | null;
  bridges: OverviewBridgeLine[];
  /** 窗口内项目上的会（太阳上写的数） */
  meetings: number;
  /** 最近一次会是几天前（全部项目里最近的那个），一个都没开过时为 null */
  latestAge: number | null;
  /** 项目数，含服务器折起来的 */
  projectCount: number;
}

// ------------------------------------------------------------------ 几何常数

export const RING_NAMES = ["7 天内", "28 天内", "更早或没开过会"] as const;

/**
 * 三圈的半径与排法：第一圈项目少，每颗之间最多隔 52°，圈头偏右前一点，名字不压太阳；
 * 外两圈在正后方留空档写圈名，其余均分。圈名在圈头对面（第一圈后半段被太阳挡着，圈名放到右后方）。
 */
const RING_GEOMETRY = [
  { R: 126, head: (90 - 18) * DEG, maxStep: 52 * DEG, gap: 0, chipAt: (180 + 62) * DEG },
  { R: 290, head: 90 * DEG, maxStep: 0, gap: 32 * DEG, chipAt: 180 * DEG },
  { R: 452, head: (90 + 4) * DEG, maxStep: 0, gap: 42 * DEG, chipAt: 180 * DEG },
] as const;

/** 最外圈超过这么多个项目时变成小行星带 */
export const BELT_MIN = 12;
const BELT_GAP = 16 * DEG;
/** 带子沿半径方向散开的幅度（世界单位） */
const BELT_SPREAD = 24;
const BELT_SEED = 79;

export const FAR_R = RING_GEOMETRY[2].R;
/** 盘面（粒子场和盘沿）比最外圈再大一圈 */
export const PLATE_R = FAR_R + 86;
export const SUN_R = 44;
/** 太阳的光点浮在盘面上方一点 */
export const SUN_Y = SUN_R * 0.82;

/** 行星的大小（世界单位）：窗口内没会的最小，有会的按场次的平方根长 */
export function planetRadius(meetings: number): number {
  return meetings > 0 ? 5.5 + 3.9 * Math.sqrt(meetings) : 5.2;
}

// ------------------------------------------------------------------ 小工具

/** 窗口内的会写成「28 天 7 场」，全部时写「全部 7 场」 */
export function islandCountText(days: number | null, meetings: number): string {
  return `${days ? `${days} 天` : "全部"} ${meetings} 场`;
}

/** 太阳上的一句：「28 天 51 场会」 */
export function sunCountText(days: number | null, meetings: number): string {
  return `${days ? `${days} 天` : "全部"} ${meetings} 场会`;
}

/** 像新项目的名字：「像是新项目『云图看板』· 2 场会」 */
export function ghostText(item: SuggestedProject): string {
  return `像是新项目『${item.name}』· ${item.meeting_count} 场会`;
}

/** 「2026-09-03」写成「9月3日」（悬停卡片、小行星带列表） */
export function monthDay(day: string): string {
  const [, month, date] = day.split("-").map(Number);
  return `${month}月${date}日`;
}

/** 在等你的数：待复核的会＋门口的会＋待确认任务 */
function waitingTotal(island: OverviewIsland): number {
  return island.waiting.review + island.waiting.doorstep + island.waiting.tasks;
}

function dayNumber(day: string): number | null {
  const match = /^(\d{4})-(\d{2})-(\d{2})/.exec(day);
  if (!match) return null;
  return Date.UTC(Number(match[1]), Number(match[2]) - 1, Number(match[3])) / 86_400_000;
}

/** 最近一次会是几天前（按日历日数）；没开过会或日期读不懂时为 null */
export function islandAge(island: Pick<OverviewIsland, "last_day">, today: string): number | null {
  if (!island.last_day) return null;
  const now = dayNumber(today);
  const last = dayNumber(island.last_day);
  if (now === null || last === null) return null;
  return Math.max(0, now - last);
}

/** 落在第几圈：0–6 天前、7–27 天前、更早或没开过会 */
export function ringIndex(age: number | null): RingIndex {
  if (age === null || age >= 28) return 2;
  return age >= 7 ? 1 : 0;
}

/** 同一圈里的先后：最近开过会的在前，一样近的场次总数多的在前，再按名字 */
function compareRecency(
  a: { age: number | null; island: OverviewIsland },
  b: { age: number | null; island: OverviewIsland },
): number {
  return (
    (a.age ?? Number.POSITIVE_INFINITY) - (b.age ?? Number.POSITIVE_INFINITY) ||
    b.island.meetings_total - a.island.meetings_total ||
    a.island.name.localeCompare(b.island.name, "zh-CN")
  );
}

/** 确定性随机（mulberry32）：小行星带的抖动、盘面粒子每次打开都一样 */
export function seededRandom(seed: number): () => number {
  let s = seed | 0;
  return () => {
    s = (s + 0x6d2b79f5) | 0;
    let t = Math.imul(s ^ (s >>> 15), 1 | s);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
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

export const BELT_ID = "belt";
export const GHOSTS_MORE_ID = "ghosts:more";
export const FOLDERS_MORE_ID = "folders:more";
export const FOLDERS_HINT_ID = "folders:hint";
export const FOLDERS_STATUS_ID = "folders:status";
export const HARBOUR_ID = "harbour";

/** 左上角读数块里最多列几个像新项目的名字、右上角最多列几个没挂的文件夹，其余写「还有 N 个」 */
export const DOCK_GHOST_MAX = 3;
export const DOCK_FOLDER_MAX = 4;

// ------------------------------------------------------------------ 两个角上的读数块

function harbourNodes(overview: GraphOverview): OverviewNode[] {
  const harbour = overview.harbour;
  const nodes: OverviewNode[] = [
    {
      id: HARBOUR_ID,
      kind: "harbour",
      region: "harbour",
      label: harbour.ai_configured
        ? `港湾：${harbour.total} 场没归项目的会`
        : `港湾：没配置 AI，${harbour.total} 场会要你自己选项目`,
      data: harbour,
    },
  ];
  const ghosts = overview.suggested_projects.slice(0, DOCK_GHOST_MAX);
  for (const item of ghosts) {
    nodes.push({ id: `np:${item.key}`, kind: "ghost", region: "harbour", label: ghostText(item), data: item });
  }
  const rest = overview.suggested_projects.length - ghosts.length;
  if (rest > 0) {
    nodes.push({
      id: GHOSTS_MORE_ID,
      kind: "ghost_more",
      region: "harbour",
      label: `还有 ${rest} 个像新项目的名字`,
      data: { count: rest },
    });
  }
  return nodes;
}

function folderNodes(folders: OverviewFolders): OverviewNode[] {
  const nodes: OverviewNode[] = [];
  const shown = folders.folders.slice(0, DOCK_FOLDER_MAX);
  const seen = new Set<string>();
  shown.forEach((folder, slot) => {
    let id = folderNodeId(folder.path);
    // hash 撞了（几乎不会）：后一个加序号，保证 id 唯一
    if (seen.has(id)) id = `${id}-${slot}`;
    seen.add(id);
    nodes.push({ id, kind: "folder", region: "folders", label: `没挂到项目的文件夹：${folder.name}`, data: folder });
  });
  const rest = folders.more + Math.max(0, folders.folders.length - shown.length);
  if (rest > 0) {
    nodes.push({
      id: FOLDERS_MORE_ID,
      kind: "folder_more",
      region: "folders",
      label: `还有 ${rest} 个文件夹没挂到项目`,
      data: { count: rest },
    });
  }
  // 一个文件夹都没列出来时写一句话：没设总文件夹是提示（能开面板），别的是状态
  if (shown.length === 0 && folders.state === "unset") {
    nodes.push({
      id: FOLDERS_HINT_ID,
      kind: "folder_hint",
      region: "folders",
      label: "设项目总文件夹后，这里会列出还没挂的文件夹",
      data: folders.suggested,
    });
  } else if (shown.length === 0) {
    const status = folderStatusText(folders);
    if (status) {
      nodes.push({ id: FOLDERS_STATUS_ID, kind: "folder_status", region: "folders", label: status, data: { text: status } });
    }
  }
  return nodes;
}

/** 文件夹那一角没有文件夹时的一句话；有文件夹或已经列完时不说 */
function folderStatusText(folders: OverviewFolders): string {
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

// ------------------------------------------------------------------ 主函数

/**
 * folders 为 null 表示没挂的文件夹还没取到：右上角先空着，到了只多出那几个节点，盘面上什么都不动。
 */
export function layoutOverview(overview: GraphOverview, folders: OverviewFolders | null): OverviewLayout {
  const days = overview.window.days;
  const groups: Array<Array<{ island: OverviewIsland; age: number | null }>> = [[], [], []];
  for (const island of overview.islands) {
    const age = islandAge(island, overview.today);
    groups[ringIndex(age)].push({ island, age });
  }
  for (const group of groups) group.sort(compareRecency);

  const folded = overview.islands_more?.project_ids ?? [];
  const farCount = groups[2].length + (overview.islands_more?.count ?? 0);
  // 服务器折起来的项目没有位置，只能待在带子的列表里：折了就一定有带子
  const beltOn = farCount > BELT_MIN || folded.length > 0;

  const islands: IslandNode[] = [];
  const rings: OverviewRing[] = RING_GEOMETRY.map((geometry, index) => {
    const ring = index as RingIndex;
    const group = groups[ring];
    const n = group.length;
    const belt = ring === 2 && beltOn;
    const gap = belt ? BELT_GAP : geometry.gap;
    const step = geometry.maxStep ? Math.min(TAU / Math.max(1, n), geometry.maxStep) : (TAU - gap) / Math.max(1, n - 1);
    const jitter = seededRandom(BELT_SEED);
    group.forEach(({ island, age }, rank) => {
      // 圈头放最新的，往两侧交替：0, -1, +1, -2, +2 …
      const side = Math.ceil(rank / 2) * (rank % 2 ? 1 : -1);
      const waiting = waitingTotal(island);
      const particle = belt && waiting === 0;
      const rj = particle ? (jitter() - 0.5) * 2 * BELT_SPREAD : 0;
      const theta = geometry.head + side * step + (particle ? (jitter() - 0.5) * step * 0.6 : 0);
      islands.push({
        id: `p:${island.id}`,
        kind: "island",
        region: "islands",
        label: [
          `项目：${island.name}，${islandCountText(days, island.meetings)}`,
          waiting ? `${waiting} 件在等你` : "",
          island.stopped_cards ? `${island.stopped_cards} 张卡片停了` : "",
        ]
          .filter(Boolean)
          .join("，"),
        data: island,
        ring,
        age,
        rank,
        theta,
        rj,
        belt: particle,
        waiting,
      });
    });
    return {
      index: ring,
      name: RING_NAMES[ring],
      R: geometry.R,
      head: geometry.head,
      chipAt: geometry.chipAt,
      count: ring === 2 ? farCount : n,
      belt,
    };
  });

  const belt: BeltNode | null = beltOn
    ? {
        id: BELT_ID,
        kind: "belt",
        region: "islands",
        label: `更早或没开过会的 ${farCount} 个项目，打开列表`,
        data: { projects: groups[2].map((item) => item.island), folded, count: farCount },
      }
    : null;

  const nodes: OverviewNode[] = [...harbourNodes(overview), ...islands];
  if (belt) nodes.push(belt);
  if (folders) nodes.push(...folderNodes(folders));
  const byId = new Map(nodes.map((node) => [node.id, node]));

  const bridges: OverviewBridgeLine[] = [];
  for (const bridge of overview.bridges) {
    if (!byId.has(`p:${bridge.a}`) || !byId.has(`p:${bridge.b}`)) continue;
    bridges.push({ id: `br:${bridge.a}:${bridge.b}`, a: `p:${bridge.a}`, b: `p:${bridge.b}`, count: bridge.count });
  }

  const ages = islands.map((node) => node.age).filter((age): age is number => age !== null);
  return {
    nodes,
    byId,
    islands,
    rings,
    belt,
    bridges,
    meetings: overview.islands.reduce((sum, island) => sum + island.meetings, 0),
    latestAge: ages.length ? Math.min(...ages) : null,
    projectCount: overview.islands.length + (overview.islands_more?.count ?? 0),
  };
}

/**
 * N 键的顺序：有在等你的或卡片停了的项目（由里圈往外、同一圈按名次），
 * 港湾里有待你选、像新项目的会时是港湾，再是像新项目的名字。
 */
export function overviewAttention(layout: OverviewLayout): string[] {
  const islands = layout.islands.filter((node) => node.waiting > 0 || node.data.stopped_cards > 0);
  const harbour = layout.nodes.filter(
    (node) => node.kind === "harbour" && node.data.counts.needs_review + node.data.counts.new_project > 0,
  );
  const ghosts = layout.nodes.filter((node) => node.kind === "ghost");
  return [...islands, ...harbour, ...ghosts].map((node) => node.id);
}

export const OVERVIEW_REGION_NAMES: Record<OverviewRegion, string> = {
  harbour: "港湾",
  islands: "项目",
  folders: "没挂到项目的文件夹",
};
