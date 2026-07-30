export function formatTime(milliseconds: number | null | undefined, withHours = false): string {
  const totalSeconds = Math.max(0, Math.floor((milliseconds ?? 0) / 1000));
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const seconds = totalSeconds % 60;
  if (withHours || hours > 0) {
    return [hours, minutes, seconds].map((value) => String(value).padStart(2, "0")).join(":");
  }
  return [minutes, seconds].map((value) => String(value).padStart(2, "0")).join(":");
}

export function formatDate(value: string | null | undefined): string {
  if (!value) return "日期待补";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value.slice(0, 10);
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).format(date);
}

/** 完成态在库里分三种，对使用者是同一件事：转写好了、能听能搜。 */
export const DONE_STATUSES = ["completed_unreviewed", "draft_modified", "published"];

export function isDoneStatus(status: string): boolean {
  return DONE_STATUSES.includes(status);
}

/** 展示用状态：把三种完成态收敛成“已完成”。 */
export function statusTone(status: string): string {
  return isDoneStatus(status) ? "done" : status;
}

export function statusLabel(status: string): string {
  const labels: Record<string, string> = {
    discovered: "已发现",
    stabilizing: "等待稳定",
    queued: "排队中",
    transcribing: "转写中",
    transcript_ready: "逐字稿就绪",
    minutes_generating: "纪要生成中",
    done: "已完成",
    completed_unreviewed: "已完成",
    draft_modified: "已完成",
    published: "已完成",
    failed: "失败",
    cancelled: "已取消",
    interrupted: "已中断",
  };
  return labels[status] ?? status;
}

const dayFormatter = new Intl.DateTimeFormat("zh-CN", {
  month: "long",
  day: "numeric",
});
const weekdayFormatter = new Intl.DateTimeFormat("zh-CN", { weekday: "long" });

export interface DayStamp {
  key: string;
  year: number;
  monthDay: string;
  weekday: string;
  relative: string | null;
}

/** 资料库按天分组用的日期戳；无录音日期的会议归到“日期待补”。 */
export function dayStamp(value: string | null | undefined): DayStamp {
  const date = value ? new Date(value) : null;
  if (!date || Number.isNaN(date.getTime())) {
    return { key: "unknown", year: 0, monthDay: "日期待补", weekday: "", relative: null };
  }
  const key = [
    date.getFullYear(),
    String(date.getMonth() + 1).padStart(2, "0"),
    String(date.getDate()).padStart(2, "0"),
  ].join("-");
  const today = new Date();
  const startOfToday = new Date(today.getFullYear(), today.getMonth(), today.getDate());
  const startOfDay = new Date(date.getFullYear(), date.getMonth(), date.getDate());
  const dayDelta = Math.round((startOfToday.getTime() - startOfDay.getTime()) / 86_400_000);
  return {
    key,
    year: date.getFullYear(),
    monthDay: dayFormatter.format(date),
    weekday: weekdayFormatter.format(date),
    relative: dayDelta === 0 ? "今天" : dayDelta === 1 ? "昨天" : dayDelta === 2 ? "前天" : null,
  };
}

/** 同一天里多场会靠开始时间区分。 */
export function formatClock(value: string | null | undefined): string {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  return new Intl.DateTimeFormat("zh-CN", {
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(date);
}

export function formatDurationText(milliseconds: number | null | undefined): string {
  const totalMinutes = Math.round((milliseconds ?? 0) / 60_000);
  if (totalMinutes < 1) return "不足 1 分钟";
  if (totalMinutes < 60) return `${totalMinutes} 分钟`;
  const hours = Math.floor(totalMinutes / 60);
  const minutes = totalMinutes % 60;
  return minutes ? `${hours} 小时 ${minutes} 分钟` : `${hours} 小时`;
}

/** 纪要没生成时标题会退成占位；这类会议需要在界面上标出来。 */
export function isUntitled(title: string, meetingId: string): boolean {
  const trimmed = (title ?? "").trim();
  if (!trimmed) return true;
  if (trimmed.toLowerCase() === meetingId.toLowerCase()) return true;
  return trimmed.endsWith("未命名录音");
}
