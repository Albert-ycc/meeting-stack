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

/** 原始标签 SPEAKER_00 从 0 计数，界面上按人的习惯从 1 计数；切块会议的标签带 C{n}_ 前缀，保留片段维度。不认得的写法原样显示。 */
export function formatSpeakerLabel(label: string | null | undefined): string {
  if (!label) return "";
  const chunked = label.match(/^C(\d+)_SPEAKER_(\d+)$/);
  if (chunked) {
    const [, chunk, ordinal] = chunked;
    return `片段${chunk}·说话人${Number(ordinal) + 1}`;
  }
  const plain = label.match(/^SPEAKER_(\d+)$/);
  if (plain) {
    return `说话人 ${Number(plain[1]) + 1}`;
  }
  return label;
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

const TASK_STATUS_TEXT: Record<string, string> = {
  pending_confirm: "待确认",
  confirmed: "已确认",
  in_progress: "进行中",
  done: "已完成",
  cancelled: "已取消",
  expired: "已过期",
};

/** 任务事件正文里的状态流转（如「confirmed → done」）翻成中文。历史事件已经落库，只能在展示时翻。 */
export function formatTaskEventBody(body: string): string {
  return body.replace(/\b([a-z_]+) → ([a-z_]+)\b/g, (match, from: string, to: string) =>
    TASK_STATUS_TEXT[from] && TASK_STATUS_TEXT[to] ? `${TASK_STATUS_TEXT[from]} → ${TASK_STATUS_TEXT[to]}` : match,
  );
}

const SERVICE_STATE_TEXT: Record<string, string> = {
  healthy: "正常",
  ok: "正常",
  ready: "就绪",
  enabled: "已开启",
  disabled: "已关闭",
  degraded: "部分降级",
  failed: "故障",
  unavailable: "连不上",
  unknown: "状态未知",
  paused: "已暂停",
  rebuilding: "重建中",
};

export function serviceStateLabel(state: string): string {
  return SERVICE_STATE_TEXT[state] ?? state;
}

const VERSION_KIND_TEXT: Record<string, string> = {
  funasr: "FunASR 主稿",
  whisper: "Whisper 对照稿",
  whisper_reference: "Whisper 对照稿",
  qwen_reference: "Qwen 对照稿",
  qwen: "Qwen 对照稿",
  legacy_txt: "历史文本",
  draft: "人工修改",
  generated: "AI 生成",
  stale_generated: "AI 生成（逐字稿后来改过）",
  imported: "历史导入",
};

export function versionKindLabel(kind: string): string {
  return VERSION_KIND_TEXT[kind] ?? kind;
}

const FAILURE_STAGE_TEXT: Record<string, string> = {
  discovered: "发现录音",
  queued: "排队",
  stabilizing: "等待稳定",
  transcribing: "转写",
  transcript_ready: "纪要计划",
  minutes_generating: "纪要生成",
  codex_callback: "纪要生成",
  archive_validation: "归档校验",
  pending_archive: "放进会议文件夹",
};

/** 转写任务卡片上「在哪一步出的问题」，不直接露 pending_archive 这类阶段码。 */
export function failureStageLabel(stage: string): string {
  return FAILURE_STAGE_TEXT[stage] ?? "处理";
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

function pad2(value: number): string {
  return String(value).padStart(2, "0");
}

/** 表格里的短日期 MM-DD，按浏览器所在时区显示（与资料库按天分组同一口径）；没有日期显示「—」。 */
export function formatMonthDay(value: string | null | undefined): string {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value.slice(5, 10);
  return `${pad2(date.getMonth() + 1)}-${pad2(date.getDate())}`;
}

/** 同上，带开始时间 MM-DD HH:mm，用于同一需求下多场会的区分。 */
export function formatMonthDayClock(value: string | null | undefined): string {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value.slice(5, 10);
  return `${formatMonthDay(value)} ${pad2(date.getHours())}:${pad2(date.getMinutes())}`;
}

/** 纪要没生成时标题会退成占位；这类会议需要在界面上标出来。 */
export function isUntitled(title: string, meetingId: string): boolean {
  const trimmed = (title ?? "").trim();
  if (!trimmed) return true;
  if (trimmed.toLowerCase() === meetingId.toLowerCase()) return true;
  return trimmed.endsWith("未命名录音");
}

/** 文件大小：B / KB / MB */
export function formatBytes(size: number): string {
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / (1024 * 1024)).toFixed(1)} MB`;
}
