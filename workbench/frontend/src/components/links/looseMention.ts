/*
 * 4b 放宽的提到（会上换了叫法的文件也连上）：界面上的几句话和回答。
 * 放宽行带 relation_id（字面行、旧后台没有），回答走 answerRelation；没有就照第二期走 reject/restore/pick。
 * 关系图里回答后的提示带［撤销］，进画布的撤销栈（{kind: "relation"}，until 用服务端的 undo_until）。
 */
import { ApiError, isOldBackend, type ApiClient, type RelationAnswer } from "../../api";
import type { NoticeFn } from "../graph/panelParts";
import { OLD_BACKEND_TEXT } from "./useRelationAnswer";

/** 放宽行的小字：代替「会上说『报价单』3 次」「3 次」「N 次」 */
export function looseSaid(phrase: string): string {
  return `说的是『${phrase}』`;
}

/** 放宽行上的［不是这份文件］之后的提示 */
export function looseRejectedNotice(phrase: string): string {
  return `已记下：『${phrase}』不是这份文件`;
}

/** 放宽行的 relation_id；字面行和旧后台回 null */
export function looseId(row: { relation_id?: number | null }): number | null {
  return typeof row.relation_id === "number" ? row.relation_id : null;
}

/** 放宽行的说法：没有 phrase 时退回 needle（后端的 needle 本来就是那句说法） */
export function loosePhrase(row: { phrase?: string | null; needle: string }): string {
  return row.phrase || row.needle;
}

/**
 * 回答一条放宽行。label 是提示那一句；withUndo 为真时提示带［撤销］（进画布的撤销栈），
 * ［撤销］那一行发 restore 时不再带。
 */
export async function answerLoose(
  apiClient: Pick<ApiClient, "answerRelation">,
  onNotice: NoticeFn,
  relationId: number,
  body: { answer: RelationAnswer; file_id?: number },
  label: string,
  withUndo = true,
) {
  const result = await apiClient.answerRelation(relationId, body);
  onNotice(label, withUndo ? { kind: "relation", relationId, label, until: result.undo_until } : undefined);
  return result;
}

/** 回答出错：旧后台写「后台还是旧版本，重启声档后再试」；404、409、422 原样显示服务器那句话 */
export function looseFailure(onNotice: NoticeFn, reason: unknown, fallback: string) {
  if (isOldBackend(reason)) {
    onNotice(OLD_BACKEND_TEXT, undefined, "warning");
    return;
  }
  if (reason instanceof ApiError && [404, 409, 422].includes(reason.status)) {
    onNotice(reason.message, undefined, "warning");
    return;
  }
  onNotice(reason instanceof Error && reason.message ? reason.message : fallback, undefined, "error");
}
