import type { AskJob, AskPlan } from "../../types";

/*
 * 问答的几轮（4g）：模块里一个 Map<项目 id, 轮[]>（和 graphCache 一样），每个项目最多 5 轮，新的在前。
 * 只在内存里：不写 sessionStorage、localStorage（回答是材料原文的转述）。在清单和关系图之间切换、
 * 打开一场会再回来都还在，刷新就没有。setDraft、takeDraft 给搜索页把问题交过来（不自动发）。
 */

export const TURNS_PER_PROJECT = 5;

export type AskPhase =
  | "preparing"
  | "confirm"
  | "sending"
  | "waiting"
  | "done"
  | "stopped"
  | "empty"
  | "unavailable"
  | "failed";

export interface AskTurn {
  id: number;
  question: string;
  phase: AskPhase;
  plan?: AskPlan;
  jobId?: string;
  job?: AskJob;
  /** 发的时候带没带材料（［再问一次］照这个重发） */
  withMaterials?: boolean;
  /** failed 时的一句话；retry 为 plan 时［再问一次］重新找一遍；list 为真时照样列出找到的原话（429、503） */
  error?: { text: string; retry: boolean; list?: boolean };
}

const turns = new Map<string, AskTurn[]>();
const drafts = new Map<string, string>();
let nextId = 1;

export function readTurns(projectId: string): AskTurn[] {
  return turns.get(projectId) ?? [];
}

export function writeTurns(projectId: string, next: AskTurn[]): void {
  turns.set(projectId, next.slice(0, TURNS_PER_PROJECT));
}

export function newTurnId(): number {
  nextId += 1;
  return nextId;
}

/** 搜索页交过来的问题：打开项目时填进输入框，不自动发 */
export function setDraft(projectId: string, question: string): void {
  drafts.set(projectId, question);
}

/** 只看不取：关系图据此在打开时展开问答面板，由 ProjectAsk 的 takeDraft 填进输入框 */
export function hasDraft(projectId: string): boolean {
  return Boolean(drafts.get(projectId));
}

export function takeDraft(projectId: string): string | undefined {
  const draft = drafts.get(projectId);
  drafts.delete(projectId);
  return draft;
}

/** 测试用：清掉所有轮和草稿 */
export function forgetAskStore(): void {
  turns.clear();
  drafts.clear();
}
