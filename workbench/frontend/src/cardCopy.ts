import type { MeetingCard, MeetingCardEffect, MeetingCardReason } from "./types";

/** 「云图AI/声档会议记录/260926 初审规则沟通.md」→「云图AI」 */
function folderOf(label: string | null) {
  return (label ?? "").split("/")[0] || "";
}

/** 卡片路径只显示最后三级：项目文件夹/声档会议记录/文件名 */
export function shortCardPath(path: string) {
  return path.split("/").filter(Boolean).slice(-3).join("/");
}

/** 改归属、撤销之后卡片的去向，拼进提示里；没动卡片时是空串 */
export function cardEffectNote(card: MeetingCardEffect | undefined) {
  if (!card) return "";
  if (card.action === "written") {
    return `会议卡片已写进「${folderOf(card.to)}」文件夹`;
  }
  if (card.action === "retired") {
    const head = `会议卡片已从「${folderOf(card.from)}」文件夹撤下`;
    if (card.reason === "no_root") return `${head}，新项目挂上文件夹后带着你的笔记补写`;
    if (card.reason === "root_offline") return `${head}，资料盘插上后带着你的笔记补写`;
    return `${head}，你的笔记一并存进了回收区`;
  }
  if (card.action === "waiting") {
    if (card.reason === "no_root") return "项目还没挂文件夹，会议卡片先存着";
    if (card.reason === "root_offline") return "资料盘没连接，会议卡片插上后补写";
    if (card.reason === "paused") return "这个项目暂停了写会议卡片";
  }
  return "";
}

/** 改归属提示的后半句：「3 条任务和会议卡片一起移过去」；什么都没跟着动时是空串 */
export function reassignNote(tasksMoved: number, tasksLeft: number, card?: MeetingCardEffect) {
  const cardMoved = card?.action === "moved";
  const moved =
    tasksMoved > 0 && cardMoved
      ? `${tasksMoved} 条任务和会议卡片一起移过去`
      : tasksMoved > 0
        ? `${tasksMoved} 条任务一起移过去`
        : cardMoved
          ? "会议卡片一起移过去"
          : "";
  return [
    moved,
    tasksLeft > 0 ? `${tasksLeft} 条任务挂在原项目的需求上，留在原处` : "",
    cardMoved ? "" : cardEffectNote(card),
  ]
    .filter(Boolean)
    .join("；");
}

export type CardStatusAction = "copy" | "rewrite" | "regenerate" | "resume" | "enable" | "open_project";

export interface CardStatusCopy {
  text: string;
  action?: CardStatusAction;
  actionLabel?: string;
}

const WAITING_COPY: Partial<Record<MeetingCardReason, string>> = {
  queued: "下一轮扫描就写进项目文件夹",
  waiting_minutes: "等纪要生成",
  waiting_project: "还没归项目",
  needs_review: "等你选项目",
  root_offline: "资料盘没连接，插上后自动补写",
};

function syncedClock(iso: string | null) {
  if (!iso) return "";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "";
  const pad = (value: number) => String(value).padStart(2, "0");
  const clock = `${pad(date.getHours())}:${pad(date.getMinutes())}`;
  const today = new Date();
  return date.toDateString() === today.toDateString()
    ? clock
    : `${date.getMonth() + 1}/${date.getDate()} ${clock}`;
}

/**
 * 会议页卡片状态条的一句话和（最多）一个按钮。三类：好了 / 在等什么 / 为什么停了。
 * projectName 是这场会当前的项目名，用在「项目『X』还没挂文件夹」。
 */
export function cardStatusCopy(card: MeetingCard, projectName?: string | null): CardStatusCopy {
  if (card.category === "ok" && card.path) {
    const clock = syncedClock(card.synced_at);
    return {
      text: `${shortCardPath(card.path)}${clock ? ` · ${clock} 更新` : ""}`,
      action: "copy",
      actionLabel: "复制卡片路径",
    };
  }
  if (card.state === "user_edited") {
    return { text: "你改过这张卡的纪要部分，已停止自动更新", action: "rewrite", actionLabel: "用最新纪要重写" };
  }
  if (card.state === "missing") {
    return { text: "卡片被移走或删除了，不再自动生成", action: "regenerate", actionLabel: "重新生成" };
  }
  switch (card.reason) {
    case "no_root":
      return {
        text: projectName ? `项目「${projectName}」还没挂文件夹` : "项目还没挂文件夹",
        action: "open_project",
        actionLabel: "去挂文件夹",
      };
    case "paused":
      return { text: "这个项目暂停了写会议卡片", action: "resume", actionLabel: "恢复写入" };
    case "disabled":
      return { text: "会议卡片还没开启", action: "enable", actionLabel: "开启" };
    case "not_backfilled":
      return { text: "上线前的会，还没补写卡片", action: "open_project", actionLabel: "去项目页补写" };
    case "root_missing":
      return { text: "找不到项目文件夹，卡片先存着", action: "open_project", actionLabel: "去项目页" };
    case "root_in_archive":
      return { text: "项目文件夹在归档目录里，声档不往那里写卡片", action: "open_project", actionLabel: "去项目页" };
    case "root_shared":
      return {
        text: "这个文件夹也挂在别的项目下，卡片只写给最早挂上的项目",
        action: "open_project",
        actionLabel: "去项目页",
      };
    default:
      break;
  }
  if (card.error) return { text: `写卡片出错：${card.error}` };
  return { text: (card.reason && WAITING_COPY[card.reason]) || "等待写入" };
}
