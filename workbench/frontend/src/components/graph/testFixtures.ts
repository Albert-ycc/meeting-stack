/* 关系图测试共用的夹具：一个小项目图，按需覆盖字段。只给测试用。 */
import type { GraphMeeting, GraphPayload, GraphRequirement } from "./graphTypes";

export const TODAY = "2026-09-26";

export function day(ago: number): string {
  const base = new Date(`${TODAY}T00:00:00Z`);
  base.setUTCDate(base.getUTCDate() - ago);
  return base.toISOString().slice(0, 10);
}

export function meeting(id: string, ago: number, overrides: Partial<GraphMeeting> = {}): GraphMeeting {
  return {
    id: `m:${id}`,
    meeting_id: id,
    title: `初审规则沟通 ${id}`,
    date: day(ago),
    age_days: ago,
    ring: ago < 7 ? "inner" : "middle",
    state: "auto",
    attribution: { label: "自动 · 提到『初审规则』3 次", source: "ai" },
    open_tasks: 0,
    pending_tasks: 0,
    card: null,
    card_text: null,
    has_minutes: true,
    ...overrides,
  };
}

export function requirement(id: string, overrides: Partial<GraphRequirement> = {}): GraphRequirement {
  return {
    id: `r:${id}`,
    requirement_id: id,
    title: `需求 ${id}`,
    priority: "P1",
    open_tasks: 1,
    pending_tasks: 0,
    last_activity: TODAY,
    age_days: 1,
    ring: "inner",
    stale: false,
    stale_text: null,
    ...overrides,
  };
}

export function payload(overrides: Partial<GraphPayload> = {}): GraphPayload {
  return {
    project: { id: "p", name: "云图AI", color: "#2c8d83", meeting_count: 3 },
    window: { requested: null, effective: "28d", days: 28, widened_reason: null },
    today: TODAY,
    meetings: [meeting("a", 0), meeting("b", 2), meeting("c", 10)],
    collapsed: [],
    doorstep: [],
    doorstep_more: 0,
    requirements: [requirement("r1")],
    requirements_more: null,
    folders: [
      { id: "root:1", kind: "root", name: "云图AI", path: "/材料/云图AI", ring: "inner", root_id: 1 },
      { id: "cards", kind: "cards", name: "声档会议记录/", path: "/材料/云图AI/声档会议记录", ring: "inner", written: 3, stopped: 0, waiting: 0, enabled: true },
    ],
    folders_more: null,
    loose: { id: "loose", kind: "loose", name: "散放文件", ring: "outer", count: null },
    cues: [{ id: "cue:1", text: "初审规则", source: "term", term_id: "t1", total: 6, meetings: [] }],
    beacons: [],
    edges: [],
    status: { ok: [], waiting: [], stopped: [], note: null },
    weekly: [],
    moved_out: [],
    ...overrides,
  };
}
