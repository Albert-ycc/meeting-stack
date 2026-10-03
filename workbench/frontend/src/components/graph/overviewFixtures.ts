// 全部项目概览的测试夹具（layoutOverview.test.ts、overviewLabels.test.ts、OverviewGraph.test.tsx、App.hash.test.tsx 共用）

import type { UnclaimedFolder } from "../../types";
import type {
  GraphOverview,
  HarbourMeeting,
  OverviewFolders,
  OverviewIsland,
  SuggestedProject,
} from "./overviewTypes";

export const TODAY = "2026-09-27";

/** TODAY 往前数 n 天的日期 */
export function daysAgo(n: number): string {
  const [year, month, day] = TODAY.split("-").map(Number);
  return new Date(Date.UTC(year, month - 1, day - n)).toISOString().slice(0, 10);
}

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

// 30 个名字长短、中西文混排都照生产库的样子；每个都按「最近一次会在几天前、28 天里几场、在等你几件」给
const CROWD_REAL: Array<[string, number | null, number, number]> = [
  ["医米科研用药", 5, 11, 1],
  ["CVM 云讲堂", 4, 1, 0],
  ["恒瑞健康", 12, 9, 0],
  ["黑卡小程序", 11, 4, 0],
  ["供应商录音质检账号开通", 11, 1, 0],
  ["安心四季", 13, 2, 2],
  ["项目复制与名单一键转移", 14, 1, 0],
  ["吉士医", 17, 1, 0],
  ["口服药到店领取配置方案", 17, 1, 0],
  ["北京西苑患者管理项目", 18, 2, 1],
  ["礼邦RWS项目迁移", 18, 1, 0],
  ["EDC医生结算口径", 18, 1, 0],
  ["华夏基金会科普同行", 19, 3, 0],
  ["Keep", 19, 1, 0],
  ["随访项目上线配置", 19, 1, 0],
  ["生物信号小程序二类证", 19, 1, 0],
  ["数字疗法APP资产交接", 19, 1, 0],
  ["医朵云SFE方案整合", 25, 1, 0],
  ["会议预约需求", 24, 1, 0],
  ["浩博患者平台", 30, 0, 0],
  ["CACA", 30, 0, 0],
  ["样品项目", 33, 0, 0],
  ["代表医生绑定方案", 38, 0, 0],
  ["MDT", 51, 0, 0],
  ["数字营销线", 72, 0, 0],
  ["OBU免疫线", 88, 0, 0],
  ["万物生对接", 88, 0, 0],
  ["寻呼随访项目", null, 0, 0],
  ["回顾性问卷项目", null, 0, 0],
  ["医米EDC新项目配置", null, 0, 0],
];
const CROWD_COLORS = ["#2c8d83", "#2c8d83", "#2c8d83", "#3f51b5", "#5090ff", "#7c3aed", "#831fa8", "#8c772c", "#3ecf8e"];

/**
 * 150 个项目：上面 30 个，再加 120 个「模拟项目 NNN」——15 个 28 天内开过会，105 个更早或没开过会（约两成没开过）。
 * 项目一多，涨的几乎全是最外圈，所以最外圈 116 个，成小行星带。固定种子，每次一样。
 */
export function crowdedOverview(): GraphOverview {
  // Park–Miller：乘积在 2^53 以内，每台机器算出来都一样
  let seed = 150;
  const random = () => {
    seed = (seed * 16807) % 2147483647;
    return seed / 2147483647;
  };
  const islands = CROWD_REAL.map(([name, age, meetings, waiting], index) =>
    island(`real${index}`, name, {
      color: CROWD_COLORS[index % CROWD_COLORS.length],
      last_day: age === null ? null : daysAgo(age),
      meetings,
      meetings_total: meetings + 3,
      waiting: { review: waiting, doorstep: 0, tasks: 0 },
    }),
  );
  for (let index = 1; index <= 120; index += 1) {
    const recent = index <= 15;
    const age = recent ? 7 + Math.floor(random() * 21) : random() < 0.18 ? null : 28 + Math.floor(random() ** 1.6 * 260);
    const meetings = recent ? 1 + Math.floor(random() * 4) : 0;
    islands.push(
      island(`sim${index}`, `模拟项目 ${String(index).padStart(3, "0")}`, {
        color: CROWD_COLORS[Math.floor(random() * CROWD_COLORS.length)],
        last_day: age === null ? null : daysAgo(age),
        meetings,
        meetings_total: meetings + Math.floor(random() * 6),
        waiting: { review: 0, doorstep: 0, tasks: 0 },
      }),
    );
  }
  return overviewPayload({ islands, suggested_projects: [], bridges: [] });
}
