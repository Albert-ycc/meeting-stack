/*
 * 展开一场会：中间一条录音条，决议在上、任务在下，按时间点对齐到录音条上。
 * 和星图一样是纯函数：同样的数据每次得到同样的坐标。
 */
import { formatTime } from "../../format";
import type { FocusTask, MeetingFocus } from "./graphTypes";
import { fitText, textWidth, type Box } from "./layout";

/** 画在条上的决议、任务最多这么多，其余收进「+N」 */
export const FOCUS_DECISIONS_MAX = 4;
export const FOCUS_TASKS_MAX = 6;

/** 录音条默认长度；画布上按视口宽度给，字始终按原大显示 */
export const BAR_W = 960;
export const BAR_H = 10;
const CARD_TEXT_W = 190;
/** 左右内边距 20、左边色条 3、边框，再留一点余量 */
const CARD_PAD = 28;
export const CARD_H = 40;
/** 第一排卡片中心离录音条的距离、排与排之间的距离 */
const FIRST_LANE = 66;
const LANE_GAP = 54;
const H_GAP = 12;
/** 卡片可以比录音条两端多伸出这么多 */
const OVERHANG = 60;
const UNTIMED_LABEL_W = 84;
const OPEN_TASK = new Set(["pending_confirm", "confirmed", "in_progress"]);
const TICK_STEPS_MS = [30, 60, 120, 300, 600, 900, 1200, 1800, 3600].map((seconds) => seconds * 1000);
const MAX_TICKS = 8;
/** 交付物小签（3g）：纸张图标加文件名，最长 80 像素；和任务卡之间留一段画实线箭头 */
export const TAG_MAX_W = 80;
export const TAG_H = 22;
const TAG_GAP = 18;
const TAG_ICON_W = 16;
const TAG_FONT = 11;

export type FocusSide = -1 | 1;

export interface FocusItem {
  /** dec:<决议 id>（台账落后时是 dec:<在 decisions 里的下标>）或 task:<任务 id> */
  id: string;
  kind: "decision" | "task";
  side: FocusSide;
  text: string;
  atMs: number | null;
  /** 时间点在录音条上的横坐标；没有时间点时为 null */
  anchorX: number | null;
  x: number;
  y: number;
  box: Box;
  status?: string;
  /** 任务的交付物文件小签：每条任务最多画 1 个，多的写「+N」 */
  tag?: FocusTag;
}

export interface FocusTag {
  fileId: number;
  name: string;
  ext: string;
  /** 截短后的文件名 */
  label: string;
  /** 另外还有几个文件交付物 */
  more: number;
  box: Box;
}

export interface FocusMore {
  id: "more:decisions" | "more:tasks";
  kind: "decision" | "task";
  side: FocusSide;
  count: number;
  text: string;
  x: number;
  y: number;
  box: Box;
}

export interface FocusLayout {
  barW: number;
  durationMs: number;
  /** 录音长度是从库里读到的；否则是按最晚的时间点估的 */
  durationKnown: boolean;
  /** 有没有带时间点的决议或任务；长度和时间点都没有时条上不画刻度 */
  hasAnchors: boolean;
  ticks: Array<{ x: number; ms: number; label: string }>;
  items: FocusItem[];
  more: FocusMore[];
  /** 「没有时间点」那一排的说明文字 */
  untimedLabels: Array<{ side: FocusSide; x: number; y: number }>;
  bounds: Box;
}

export function msToX(ms: number, durationMs: number, barW = BAR_W): number {
  if (durationMs <= 0) return 0;
  return Math.min(1, Math.max(0, ms / durationMs)) * barW;
}

export function xToMs(x: number, durationMs: number, barW = BAR_W): number {
  return Math.round(Math.min(1, Math.max(0, x / barW)) * durationMs);
}

function cardOf(text: string) {
  const label = fitText(text, CARD_TEXT_W, 12);
  return { label, w: Math.min(CARD_TEXT_W, textWidth(label, 12)) + CARD_PAD };
}

/** 刻度不超过 8 个，也不挤：两个刻度之间至少留 60px */
function tickStep(durationMs: number, barW: number) {
  const most = Math.max(2, Math.min(MAX_TICKS, Math.floor(barW / 60)));
  return TICK_STEPS_MS.find((step) => durationMs / step <= most) ?? TICK_STEPS_MS[TICK_STEPS_MS.length - 1];
}

interface Pending {
  id: string;
  kind: "decision" | "task";
  text: string;
  atMs: number | null;
  status?: string;
  tag?: Omit<FocusTag, "box" | "label">;
}

function extOf(name: string) {
  const dot = name.lastIndexOf(".");
  return dot > 0 ? name.slice(dot + 1).toLowerCase() : "";
}

/** 任务的交付物里能打开的文件（有 file_id、没找不到）：画最近标的那一个，其余记个数 */
export function deliverableTag(task: FocusTask): Omit<FocusTag, "box" | "label"> | undefined {
  const files = (task.deliverables ?? []).filter(
    (item) => item.kind === "file" && typeof item.file_id === "number" && !item.gone,
  );
  const last = files[files.length - 1];
  if (!last || typeof last.file_id !== "number") return undefined;
  const name = last.name || last.url.split("/").pop() || last.title || "文件";
  return { fileId: last.file_id, name, ext: extOf(name), more: files.length - 1 };
}

/** 小签的字和宽度：图标、截短的文件名、「+N」，合起来不超过 80 像素 */
function tagMetrics(tag: Omit<FocusTag, "box" | "label">) {
  const moreW = tag.more > 0 ? textWidth(`+${tag.more}`, TAG_FONT) + 4 : 0;
  const label = fitText(tag.name, Math.max(12, TAG_MAX_W - TAG_ICON_W - 8 - moreW), TAG_FONT);
  const w = Math.min(TAG_MAX_W, TAG_ICON_W + 8 + textWidth(label, TAG_FONT) + moreW);
  return { label, w };
}

/** 卡片连同右边的小签一共占多宽（排的时候按这个算，同一排里不重叠） */
function extraOf(entry: Pending) {
  return entry.tag ? TAG_GAP + tagMetrics(entry.tag).w : 0;
}

function withTag(entry: Pending, card: Box, y: number): FocusTag | undefined {
  if (!entry.tag) return undefined;
  const { label, w } = tagMetrics(entry.tag);
  return { ...entry.tag, label, box: { x: card.x + card.w + TAG_GAP, y: y - TAG_H / 2, w, h: TAG_H } };
}

/** 有时间点的按时间排，一排放不下就往外挪一排；返回用到的排数 */
function placeTimed(entries: Pending[], side: FocusSide, durationMs: number, barW: number, out: FocusItem[]): number {
  const lanes: Array<Array<[number, number]>> = [];
  const timed = entries
    .filter((entry) => entry.atMs !== null)
    .sort((a, b) => (a.atMs as number) - (b.atMs as number) || a.id.localeCompare(b.id));
  for (const entry of timed) {
    const anchorX = msToX(entry.atMs as number, durationMs, barW);
    const { label, w } = cardOf(entry.text);
    const extra = extraOf(entry);
    const x = Math.min(barW + OVERHANG - w / 2 - extra, Math.max(-OVERHANG + w / 2, anchorX));
    const span: [number, number] = [x - w / 2 - H_GAP / 2, x + w / 2 + extra + H_GAP / 2];
    let lane = lanes.findIndex((taken) => taken.every(([left, right]) => span[1] <= left || span[0] >= right));
    if (lane === -1) {
      lanes.push([]);
      lane = lanes.length - 1;
    }
    lanes[lane].push(span);
    const y = side * (FIRST_LANE + lane * LANE_GAP);
    const box = { x: x - w / 2, y: y - CARD_H / 2, w, h: CARD_H };
    out.push({
      id: entry.id,
      kind: entry.kind,
      side,
      text: label,
      atMs: entry.atMs,
      anchorX,
      x,
      y,
      box,
      status: entry.status,
      tag: withTag(entry, box, y),
    });
  }
  return lanes.length;
}

/** 没时间点的和「+N」从左往右排在最外面，排满换一排 */
function placeOuter(
  entries: Pending[],
  more: { id: FocusMore["id"]; kind: FocusMore["kind"]; count: number } | null,
  side: FocusSide,
  firstLane: number,
  barW: number,
  items: FocusItem[],
  mores: FocusMore[],
  labels: FocusLayout["untimedLabels"],
) {
  const untimed = entries.filter((entry) => entry.atMs === null);
  if (!untimed.length && !more) return;
  let lane = firstLane;
  const laneY = () => side * (FIRST_LANE + lane * LANE_GAP + 10);
  let left = untimed.length ? UNTIMED_LABEL_W : 0;
  if (untimed.length) labels.push({ side, x: 0, y: laneY() });
  /** 占一段宽 w 加 extra（小签）的位置，返回卡片中心 */
  const next = (w: number, extra = 0) => {
    if (left > 0 && left + w + extra > barW + OVERHANG) {
      lane += 1;
      left = untimed.length ? UNTIMED_LABEL_W : 0;
    }
    const x = left + w / 2;
    left += w + extra + H_GAP;
    return { x, y: laneY() };
  };
  for (const entry of untimed) {
    const { label, w } = cardOf(entry.text);
    const { x, y } = next(w, extraOf(entry));
    const box = { x: x - w / 2, y: y - CARD_H / 2, w, h: CARD_H };
    items.push({
      id: entry.id,
      kind: entry.kind,
      side,
      text: label,
      atMs: null,
      anchorX: null,
      x,
      y,
      box,
      status: entry.status,
      tag: withTag(entry, box, y),
    });
  }
  if (more) {
    const text = `+${more.count} 条${more.kind === "decision" ? "决议" : "任务"}`;
    const w = textWidth(text, 12) + CARD_PAD;
    const { x, y } = next(w);
    mores.push({ ...more, side, text, x, y, box: { x: x - w / 2, y: y - 14, w, h: 28 } });
  }
}

/** 决议节点的 id：有台账 id 时用它，改稿调顺序也选得回同一条；没有时用下标 */
export function decisionNodeId(decision: { id?: string | null }, index: number): string {
  return `dec:${decision.id ?? index}`;
}

/** 决议挑时间最早的 4 个（有时间点的在前）；任务先挑没做完的，再挑有时间点的，最多 6 个 */
export function layoutMeetingFocus(focus: MeetingFocus, barW = BAR_W): FocusLayout {
  const decisions: Pending[] = focus.decisions.map((item, index) => ({
    id: decisionNodeId(item, index),
    kind: "decision",
    text: item.text,
    atMs: item.start_ms,
  }));
  const tasks: Pending[] = focus.tasks.map((task) => ({
    id: `task:${task.id}`,
    kind: "task",
    text: task.title,
    atMs: task.anchor_ms,
    status: task.status,
    tag: deliverableTag(task),
  }));
  const byTime = (a: Pending, b: Pending) =>
    (a.atMs === null ? 1 : 0) - (b.atMs === null ? 1 : 0) || (a.atMs ?? 0) - (b.atMs ?? 0);
  const shownDecisions = [...decisions].sort(byTime).slice(0, FOCUS_DECISIONS_MAX);
  const shownTasks = [...tasks]
    .sort(
      (a, b) =>
        (OPEN_TASK.has(a.status ?? "") ? 0 : 1) - (OPEN_TASK.has(b.status ?? "") ? 0 : 1) || byTime(a, b),
    )
    .slice(0, FOCUS_TASKS_MAX);

  const anchors = [...decisions, ...tasks].map((item) => item.atMs).filter((value): value is number => value !== null);
  const known = Boolean(focus.meeting.duration_ms && focus.meeting.duration_ms > 0);
  const durationMs = known
    ? (focus.meeting.duration_ms as number)
    : Math.max(60_000, Math.ceil((Math.max(0, ...anchors) * 1.1) / 60_000) * 60_000);

  const items: FocusItem[] = [];
  const more: FocusMore[] = [];
  const untimedLabels: FocusLayout["untimedLabels"] = [];
  const decisionLanes = placeTimed(shownDecisions, -1, durationMs, barW, items);
  const taskLanes = placeTimed(shownTasks, 1, durationMs, barW, items);
  const moreDecisions = decisions.length - shownDecisions.length;
  const moreTasks = tasks.length - shownTasks.length + focus.tasks_more;
  placeOuter(
    shownDecisions,
    moreDecisions > 0 ? { id: "more:decisions", kind: "decision", count: moreDecisions } : null,
    -1,
    decisionLanes,
    barW,
    items,
    more,
    untimedLabels,
  );
  placeOuter(
    shownTasks,
    moreTasks > 0 ? { id: "more:tasks", kind: "task", count: moreTasks } : null,
    1,
    taskLanes,
    barW,
    items,
    more,
    untimedLabels,
  );

  const step = tickStep(durationMs, barW);
  const ticks: FocusLayout["ticks"] = [];
  const scaled = known || anchors.length > 0;
  for (let ms = 0; scaled && ms <= durationMs; ms += step) {
    ticks.push({ x: msToX(ms, durationMs, barW), ms, label: formatTime(ms) });
  }
  if (known && ticks.length && durationMs - ticks[ticks.length - 1].ms > step / 3) {
    ticks.push({ x: barW, ms: durationMs, label: formatTime(durationMs) });
  }

  const boxes = [
    ...items.map((item) => item.box),
    ...items.flatMap((item) => (item.tag ? [item.tag.box] : [])),
    ...more.map((item) => item.box),
  ];
  const minX = Math.min(-20, ...boxes.map((box) => box.x));
  const maxX = Math.max(barW + 20, ...boxes.map((box) => box.x + box.w));
  const minY = Math.min(-FIRST_LANE, ...boxes.map((box) => box.y));
  const maxY = Math.max(FIRST_LANE, ...boxes.map((box) => box.y + box.h));
  return {
    barW,
    durationMs,
    durationKnown: known,
    hasAnchors: anchors.length > 0,
    ticks,
    items,
    more,
    untimedLabels,
    bounds: { x: minX, y: minY, w: maxX - minX, h: maxY - minY },
  };
}
