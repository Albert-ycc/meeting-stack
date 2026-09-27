// 全部项目概览的测试夹具（layoutOverview.test.ts、OverviewGraph.test.tsx、App.hash.test.tsx 共用）

import type { UnclaimedFolder } from "../../types";
import type {
  GraphOverview,
  HarbourMeeting,
  OverviewFolders,
  OverviewIsland,
  SuggestedProject,
} from "./overviewTypes";

export const TODAY = "2026-09-27";

export function island(id: string, name: string, overrides: Partial<OverviewIsland> = {}): OverviewIsland {
  return {
    id,
    name,
    color: "#2c8d83",
    meetings: 3,
    meetings_total: 10,
    last_day: "2026-09-25",
    requirements_active: 2,
    waiting: { review: 0, doorstep: 0, tasks: 0 },
    stopped_cards: 0,
    roots: [],
    pending_folder: null,
    ...overrides,
  };
}

export function suggested(index: number, overrides: Partial<SuggestedProject> = {}): SuggestedProject {
  return {
    key: `name${index}`,
    name: `新名字${index}`,
    meeting_count: 2,
    meeting_ids: [`nm${index}`],
    last_at: "2026-09-20T10:00:00",
    ...overrides,
  };
}

export function harbourMeeting(id: string, overrides: Partial<HarbourMeeting> = {}): HarbourMeeting {
  return {
    id,
    title: `没归项目的会 ${id}`,
    day: "2026-09-24",
    state: "none",
    candidates: [],
    name_hint: null,
    ...overrides,
  };
}

export function overviewPayload(overrides: Partial<GraphOverview> = {}): GraphOverview {
  return {
    window: { effective: "28d", days: 28 },
    today: TODAY,
    islands: [
      island("a", "云图AI", { meetings: 7, waiting: { review: 0, doorstep: 1, tasks: 1 } }),
      island("b", "数据中台", { color: "#7a5af8", meetings: 2 }),
      island("c", "北辰仓", { color: "#c2410c", meetings: 0, stopped_cards: 2 }),
    ],
    islands_more: null,
    harbour: {
      total: 9,
      counts: { ai_pending: 2, needs_review: 3, none: 2, new_project: 2 },
      recent: [
        harbourMeeting("h1", {
          state: "needs_review",
          candidates: [
            { project_id: "a", project_name: "云图AI", project_color: "#2c8d83", count: 3, llm: true, current: false },
            { project_id: "b", project_name: "数据中台", project_color: "#7a5af8", count: 1, llm: false, current: false },
          ],
        }),
        harbourMeeting("h2"),
      ],
      ai_configured: true,
    },
    suggested_projects: [suggested(1, { name: "云图看板", meeting_ids: ["nm1", "nm1b"] })],
    bridges: [{ a: "a", b: "b", count: 2 }],
    ...overrides,
  };
}

export function unclaimed(name: string, overrides: Partial<UnclaimedFolder> = {}): UnclaimedFolder {
  return {
    path: `/Volumes/资料盘/项目/${name}`,
    name,
    modified_at: "2026-09-20T10:00:00",
    kind: "new",
    action: "create",
    project_id: null,
    project_name: null,
    project_roots: [],
    checked: true,
    ...overrides,
  };
}

export function foldersPayload(overrides: Partial<OverviewFolders> = {}): OverviewFolders {
  return {
    state: "ready",
    parent: { path: "/Volumes/资料盘/项目", state: "online", reason: null },
    suggested: null,
    folders: [unclaimed("云图看板"), unclaimed("旧资料")],
    more: 0,
    ...overrides,
  };
}
