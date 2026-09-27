// 全部项目概览（2c）接口的数据形状，和后端 overview.py 一一对应。

import type { AttributionCandidate, NameHint, UnclaimedFolder } from "../../types";
import type { GraphWindow } from "./graphTypes";

/** 一个项目岛：窗口内的会、在等你的数（琥珀点）、停了的卡片（灰色 ⊘）、挂的文件夹 */
export interface OverviewIsland {
  id: string;
  name: string;
  color: string;
  /** 窗口内的会 */
  meetings: number;
  meetings_total: number;
  last_day: string | null;
  requirements_active: number;
  /** review：已归这个项目、待你复核的会；doorstep：没归项目、候选里有它的会；tasks：待确认任务。琥珀点是三者之和 */
  waiting: { review: number; doorstep: number; tasks: number };
  stopped_cards: number;
  roots: Array<{ id: number; name: string }>;
  pending_folder: null | { path: string; parent: string; state: "waiting" | "stopped"; reason: string | null };
}

/** 等 AI 判断 / 待你选 / AI 没认出 / 像新项目 */
export type HarbourState = "ai_pending" | "needs_review" | "none" | "new_project";

export interface HarbourMeeting {
  id: string;
  title: string;
  day: string;
  state: HarbourState;
  /** 资料库那一行同一个形状，只在待你选时有 */
  candidates: AttributionCandidate[];
  name_hint: NameHint | null;
}

export interface OverviewHarbour {
  total: number;
  counts: Record<HarbourState, number>;
  /** 最近 8 场（窗口内、没归项目、不含你标了「不归项目」的） */
  recent: HarbourMeeting[];
  /** false 时港湾写「没配置 AI：N 场会要你自己选项目」 */
  ai_configured: boolean;
}

/** 像新项目的名字（幽灵岛）；meeting_ids[0] 是最近的一场 */
export interface SuggestedProject {
  key: string;
  name: string;
  meeting_count: number;
  meeting_ids: string[];
  last_at: string;
}

/** 两个项目之间的跨项目关联，按项目对合计 */
export interface OverviewBridge {
  a: string;
  b: string;
  count: number;
}

/** GET /api/graph/overview?window= */
export interface GraphOverview {
  window: { effective: GraphWindow; days: number | null };
  today: string;
  /** 按项目创建先后排好：layoutOverview 的格子顺序 */
  islands: OverviewIsland[];
  /** 超过 40 个项目时，窗口内没会的折起来 */
  islands_more: null | { count: number; project_ids: string[] };
  harbour: OverviewHarbour;
  suggested_projects: SuggestedProject[];
  bridges: OverviewBridge[];
}

/** getGraphOverview 的结果：overview 为 null 表示和上次一样（304） */
export interface GraphOverviewFetch {
  overview: GraphOverview | null;
  etag: string | null;
}

export type OverviewFoldersState = "unset" | "checking" | "ready" | "offline" | "conflict";

/** GET /api/graph/overview/folders：项目总文件夹下还没挂的文件夹（读缓存） */
export interface OverviewFolders {
  /** checking 时 2 秒后再取 */
  state: OverviewFoldersState;
  parent: null | { path: string; state: string | null; reason: string | null };
  /** 没设总文件夹时的推荐位置（「用这个」） */
  suggested: null | { path: string; count: number; total: number };
  /** 最多 12 个，按修改时间倒序 */
  folders: UnclaimedFolder[];
  /** 「还有 N 个」 */
  more: number;
}
