import type { GraphWindow } from "./graphTypes";

/*
 * 关系图的个人偏好，存在本机 localStorage，关掉再开还在：
 * - 项目详情页上次看的是关系图还是清单（按项目）；
 * - 手动选过的时间窗（按项目，没选过就交给后端按会数自动定）；
 * - 每周打开关系图的次数，上线 4 周后拿来判断默认视图要不要改回清单（只在本机统计）。
 * 存储不可用时一律当成没存过。
 */
const PREFIX = "meeting-workbench:graph:";
const WINDOWS: GraphWindow[] = ["7d", "28d", "90d", "all"];

export type ProjectViewMode = "graph" | "list";

function read(key: string): string | null {
  try {
    return window.localStorage.getItem(PREFIX + key);
  } catch {
    return null;
  }
}

function write(key: string, value: string) {
  try {
    window.localStorage.setItem(PREFIX + key, value);
  } catch {
    // 存不下只是下次回到默认
  }
}

/** 没选过时默认关系图；是否改回清单由用户决定，系统不自动切。 */
export function readProjectMode(projectId: string): ProjectViewMode {
  return read(`mode.${projectId}`) === "list" ? "list" : "graph";
}

export function writeProjectMode(projectId: string, mode: ProjectViewMode) {
  write(`mode.${projectId}`, mode);
}

export function readGraphWindow(projectId: string): GraphWindow | null {
  const value = read(`window.${projectId}`);
  return WINDOWS.includes(value as GraphWindow) ? (value as GraphWindow) : null;
}

export function writeGraphWindow(projectId: string, window: GraphWindow) {
  write(`window.${projectId}`, window);
}

function isoWeek(date: Date): string {
  const day = new Date(Date.UTC(date.getFullYear(), date.getMonth(), date.getDate()));
  const weekday = day.getUTCDay() || 7;
  day.setUTCDate(day.getUTCDate() + 4 - weekday);
  const yearStart = new Date(Date.UTC(day.getUTCFullYear(), 0, 1));
  const week = Math.ceil(((day.getTime() - yearStart.getTime()) / 86_400_000 + 1) / 7);
  return `${day.getUTCFullYear()}-W${String(week).padStart(2, "0")}`;
}

/** 记一次打开；只留最近 12 周。 */
export function recordGraphOpen(now = new Date()) {
  let counts: Record<string, number> = {};
  try {
    counts = JSON.parse(read("opens") ?? "{}") as Record<string, number>;
  } catch {
    counts = {};
  }
  const week = isoWeek(now);
  counts[week] = (counts[week] ?? 0) + 1;
  const kept = Object.keys(counts).sort().slice(-12);
  write("opens", JSON.stringify(Object.fromEntries(kept.map((key) => [key, counts[key]]))));
}
