import type { RelatedItem, RelatedWindow } from "../../api";
import { formatTime } from "../../format";

/*
 * 相关材料栏的纯函数（4d）：窗 90 秒宽、每 45 秒一个，每个时刻落在两个窗里。
 * 栏里取覆盖这个时刻的两个窗，合并、按内容去重、按排序最多 3 条；位置在同一个 45 秒里移动时结果不变。
 */

export const STEP_MS = 45_000;
export const ITEMS_AT_ONCE = 3;
export const OTHER_TIMES = 8;

/** 覆盖这个时刻的窗（最多两个），按开始时间 */
export function windowsAt(windows: RelatedWindow[], ms: number): RelatedWindow[] {
  return windows.filter((window) => window.start_ms <= ms && ms < window.end_ms);
}

/** 这个时刻所在的 45 秒一格的开头：栏标题「12:00 前后」和「位置在同一个窗里不重画」都按它 */
export function slotStart(ms: number): number {
  return Math.floor(Math.max(0, ms) / STEP_MS) * STEP_MS;
}

/** 覆盖这个时刻的两个窗合并：先各自的第一条、再各自的第二条，按内容去重，最多 3 条 */
export function itemsAt(windows: RelatedWindow[], ms: number, limit = ITEMS_AT_ONCE): RelatedItem[] {
  const covering = windowsAt(windows, ms);
  const result: RelatedItem[] = [];
  const seen = new Set<string>();
  const longest = Math.max(0, ...covering.map((window) => window.items.length));
  for (let rank = 0; rank < longest; rank += 1) {
    for (const window of covering) {
      const item = window.items[rank];
      if (!item || seen.has(item.content_key)) continue;
      seen.add(item.content_key);
      result.push(item);
      if (result.length >= limit) return result;
    }
  }
  return result;
}

/** 当前这一段没有时，「别的时间有：」离这个时刻最近的最多 8 个窗的开头，按时间排 */
export function nearestTimes(windows: RelatedWindow[], ms: number, limit = OTHER_TIMES): number[] {
  return windows
    .filter((window) => window.items.length > 0 && !(window.start_ms <= ms && ms < window.end_ms))
    .map((window) => window.start_ms)
    .sort((a, b) => Math.abs(a - ms) - Math.abs(b - ms) || a - b)
    .slice(0, limit)
    .sort((a, b) => a - b);
}

/** 「12:00」这类时间；一小时以上带小时 */
export function clock(ms: number): string {
  return formatTime(ms);
}

/** 标题行：「相关材料（12:00 前后）2 份」；这一段没有时「这一段没有」 */
export function summaryLine(ms: number, count: number): string {
  return `相关材料（${clock(slotStart(ms))} 前后）${count ? `${count} 份` : "这一段没有"}`;
}
