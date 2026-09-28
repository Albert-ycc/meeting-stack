/* 需求页「决议」卡、项目时间线、展开一场会共用的字（第 5、12 节）。日期写「9月28日」，不写路径。 */
import type { DecisionDismissed, DecisionLinkRef, DecisionRestatedRef } from "../../api";

const WEEKDAYS = "日一二三四五六";

function parseDay(iso: string): { year: number; month: number; day: number } | null {
  const match = /^(\d{4})-(\d{2})-(\d{2})/.exec(iso || "");
  if (!match) return null;
  return { year: Number(match[1]), month: Number(match[2]), day: Number(match[3]) };
}

/** 「9月28日」 */
export function monthDay(iso: string): string {
  const parts = parseDay(iso);
  return parts ? `${parts.month}月${parts.day}日` : iso;
}

/** 分组标题的日子：「9月24日 周三」，不是今年的「2025年12月30日 周二」 */
export function dayWithWeekday(iso: string, today: Date = new Date()): string {
  const parts = parseDay(iso);
  if (!parts) return iso;
  const weekday = WEEKDAYS[new Date(parts.year, parts.month - 1, parts.day).getDay()];
  const head = parts.year === today.getFullYear() ? `${parts.month}月${parts.day}日` : `${parts.year}年${parts.month}月${parts.day}日`;
  return `${head} 周${weekday}`;
}

/** 早的那条：「后来改了：9月28日 周会『阈值改成 0.7』」 */
export function laterMark(ref: DecisionLinkRef): string {
  return `后来改了：${monthDay(ref.meeting.date)} ${ref.meeting.title}『${ref.text}』`;
}

/** 晚的那条：「这次改了 9月20日 周会定的『阈值先按 0.8 执行』」 */
export function earlierMark(ref: DecisionLinkRef): string {
  return `这次改了 ${monthDay(ref.meeting.date)} ${ref.meeting.title}定的『${ref.text}』`;
}

/** 「后来又提到：9月30日 周会」 */
export function restatedMark(ref: DecisionRestatedRef): string {
  return `后来又提到：${monthDay(ref.meeting.date)} ${ref.meeting.title}`;
}

/** 标过［不是一回事］的：「你标过和 9月28日 周会那条不是一回事」 */
export function dismissedMark(item: DecisionDismissed): string {
  return `你标过和 ${monthDay(item.other.date)} ${item.other.meeting_title}那条不是一回事`;
}

/** 简报、时间线决议后面的小尾巴：「· 9月28日后来改了」 */
export function laterTail(date: string): string {
  return `· ${monthDay(date)}后来改了`;
}

/** 回答以后的提示（第 12 节「提示和撤销」） */
export const DISMISS_CHANGED_NOTICE = "已去掉这条『后来改了』";
export const DISMISS_RESTATED_NOTICE = "已分开，两条各列各的";
export const PLACED_NONE_NOTICE = "已从这个需求里拿掉，项目时间线的『决议』里还能看到";
export const PLACED_HERE_NOTICE = "已放到这个需求";
export const UNDONE_NOTICE = "已撤销";

export function placedIntoNotice(title: string): string {
  return `已放到『${title}』`;
}
