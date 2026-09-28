import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, useSyncExternalStore } from "react";

import {
  ApiError,
  isOldBackend,
  type ApiClient,
  type RelationAnswer,
  type RelationKind,
  type RelationAnswerResult,
  type RelationQuestion,
} from "../../api";
import type { NoticeFn } from "../graph/panelParts";

/** 新页面配没重启的旧后台时的那一句 */
export const OLD_BACKEND_TEXT = "后台还是旧版本，重启声档后再试";
/** 回答 409 是这句时，问题块整块消失 */
export const ALREADY_HANDLED_TEXT = "这条已经处理过了";

/**
 * 回答以后收成的一行：那句提示加［撤销］，留到服务端给的 undo_until。
 * 按文件 id、任务 id、决议 id 记，宿主关掉再打开还能按自己的 id 找回来。
 */
export interface RecentAnswer {
  relationId: number;
  kind: RelationKind;
  text: string;
  until: string;
  fileId: number | null;
  taskId: string | null;
  decisionId: string | null;
}

/** 宿主是谁：文件面板、预览抽屉给 fileId，任务抽屉给 taskId，需求卡给 decisionIds */
export interface RelationScope {
  fileId?: number | null;
  taskId?: string | null;
  decisionIds?: string[];
}

/** App 一层的存储：只在内存里，宿主卸载再挂上时这些行还在 */
export interface RecentAnswerStore {
  get: () => RecentAnswer[];
  put: (entry: RecentAnswer) => void;
  drop: (relationId: number) => void;
  subscribe: (listener: () => void) => () => void;
}

export function createRecentAnswerStore(): RecentAnswerStore {
  let items: RecentAnswer[] = [];
  const listeners = new Set<() => void>();
  const emit = () => listeners.forEach((listener) => listener());
  return {
    get: () => items,
    put: (entry) => {
      const now = Date.now();
      // 过了撤销期的顺手清掉
      items = [...items.filter((item) => item.relationId !== entry.relationId && Date.parse(item.until) > now), entry];
      emit();
    },
    drop: (relationId) => {
      if (!items.some((item) => item.relationId === relationId)) return;
      items = items.filter((item) => item.relationId !== relationId);
      emit();
    },
    subscribe: (listener) => {
      listeners.add(listener);
      return () => {
        listeners.delete(listener);
      };
    },
  };
}

export const RecentAnswersContext = createContext<RecentAnswerStore | null>(null);

/** 每种回答一句提示（第 12 节「提示和撤销」）；4b、4d 的说法由各自的宿主传 label */
export function answerNotice(question: RelationQuestion, answer: RelationAnswer): string {
  if (question.kind === "produced") {
    if (answer === "yes") {
      return question.task?.title ? `已登记为『${question.task.title}』的交付物` : "已登记为交付物";
    }
    if (answer === "no") return "已记下：不是这条任务的交付物";
  }
  if (question.kind === "affects") {
    if (answer === "updated") return "已标为更新过";
    if (answer === "no") return "已记下：和这条决议不相关";
  }
  if (question.kind === "later_changed" && answer === "no") return "已去掉这条『后来改了』";
  if (question.kind === "restated" && answer === "no") return "已分开，两条各列各的";
  if (question.kind === "related" && answer === "no") return `已记下：『${question.file.name}』和这场会不相关`;
  return "已记下";
}

/** 收成的这一行归不归这个宿主（按文件 id、任务 id、决议 id） */
export function inScope(entry: RecentAnswer, scope: RelationScope) {
  if (scope.fileId !== undefined && scope.fileId !== null && entry.fileId === scope.fileId) return true;
  if (scope.taskId && entry.taskId === scope.taskId) return true;
  return Boolean(entry.decisionId && scope.decisionIds?.includes(entry.decisionId));
}

function errorText(reason: unknown, fallback: string) {
  return reason instanceof Error && reason.message ? reason.message : fallback;
}

interface UseRelationAnswerOptions {
  apiClient: Partial<Pick<ApiClient, "answerRelation" | "undoRelation">>;
  scope: RelationScope;
  /** 关系图里是 showNotice，撤销进画布的撤销栈；别处由宿主把 relation 撤销接到 undo 上 */
  onNotice: NoticeFn;
  /** 回答、撤销以后宿主重取（关系图的 ETag 变了） */
  onChanged?: () => void | Promise<void>;
  /** 4f：回答成了以后（重取之前）告诉宿主，关系图拿它把选中挪到新的交付物线或文件上 */
  onAnswered?: (question: RelationQuestion, answer: RelationAnswer, result: RelationAnswerResult) => void;
}

/**
 * 发回答、撤销，记下收成的那一行。409、422 和中文的 404 原样显示服务器那句话（warning），
 * 旧后台写「后台还是旧版本，重启声档后再试」，这之后问题块不再画。
 */
export function useRelationAnswer({ apiClient, scope, onNotice, onChanged, onAnswered }: UseRelationAnswerOptions) {
  const shared = useContext(RecentAnswersContext);
  // 没有 App 那一层时（单独挂的测试）退回组件自己的一份
  const [ownStore] = useState(createRecentAnswerStore);
  const store = shared ?? ownStore;
  const all = useSyncExternalStore(store.subscribe, store.get, store.get);
  const [now, setNow] = useState(() => Date.now());
  const [sending, setSending] = useState<number | null>(null);
  const sendingRef = useRef(false);
  const [gone, setGone] = useState<ReadonlySet<number>>(() => new Set());
  const [oldBackend, setOldBackend] = useState(false);

  const scopeKey = `${scope.fileId ?? ""}|${scope.taskId ?? ""}|${(scope.decisionIds ?? []).join(",")}`;
  // scope 每次渲染都是新对象，按 scopeKey 比
  // eslint-disable-next-line react-hooks/exhaustive-deps
  const scoped = useMemo(() => all.filter((entry) => inScope(entry, scope)), [all, scopeKey]);
  const recent = useMemo(() => scoped.filter((entry) => Date.parse(entry.until) > now), [scoped, now]);

  // 最早到期的那一行到期时重画，过期就收起
  const nextDeadline = recent.length ? Math.min(...recent.map((entry) => Date.parse(entry.until))) : null;
  useEffect(() => {
    if (nextDeadline === null) return;
    const wait = Math.max(0, nextDeadline - Date.now()) + 50;
    const timer = window.setTimeout(() => setNow(Date.now()), Math.min(wait, 2_147_000_000));
    return () => window.clearTimeout(timer);
  }, [nextDeadline]);

  const showFailure = useCallback(
    (reason: unknown, fallback: string) => {
      if (isOldBackend(reason)) {
        setOldBackend(true);
        onNotice(OLD_BACKEND_TEXT, undefined, "warning");
        return;
      }
      if (reason instanceof ApiError && [404, 409, 422].includes(reason.status)) {
        onNotice(reason.message, undefined, "warning");
        return;
      }
      onNotice(errorText(reason, fallback), undefined, "error");
    },
    [onNotice],
  );

  // 宿主重取失败时由宿主自己说，不算这次回答没成
  const refetch = useCallback(async () => {
    try {
      await onChanged?.();
    } catch {
      // 忽略
    }
  }, [onChanged]);

  /** 回答一条；pick 要带 fileId；label 不给时按第 12 节的表 */
  const answer = useCallback(
    async (question: RelationQuestion, value: RelationAnswer, extra: { fileId?: number; label?: string } = {}) => {
      if (sendingRef.current || typeof apiClient.answerRelation !== "function") return false;
      sendingRef.current = true;
      setSending(question.relation_id);
      try {
        const body = value === "pick" && extra.fileId !== undefined ? { answer: value, file_id: extra.fileId } : { answer: value };
        const result = await apiClient.answerRelation(question.relation_id, body);
        const text = extra.label ?? answerNotice(question, value);
        store.put({
          relationId: question.relation_id,
          kind: question.kind,
          text,
          until: result.undo_until,
          fileId: question.file?.id ?? null,
          taskId: question.task?.id ?? null,
          decisionId: question.decision?.id ?? null,
        });
        setNow(Date.now());
        onNotice(text, { kind: "relation", relationId: question.relation_id, label: text, until: result.undo_until });
        onAnswered?.(question, value, result);
        await refetch();
        return true;
      } catch (reason) {
        const handled =
          reason instanceof ApiError &&
          !isOldBackend(reason) &&
          ((reason.status === 409 && reason.message === ALREADY_HANDLED_TEXT) || reason.status === 404);
        if (handled) setGone((current) => new Set(current).add(question.relation_id));
        showFailure(reason, "操作失败");
        return false;
      } finally {
        sendingRef.current = false;
        setSending(null);
      }
    },
    [apiClient, onAnswered, onNotice, refetch, showFailure, store],
  );

  /** 收成的那一行上的［撤销］；关系图里的 ⌘Z 走画布自己的撤销栈 */
  const undo = useCallback(
    async (relationId: number) => {
      if (sendingRef.current || typeof apiClient.undoRelation !== "function") return false;
      sendingRef.current = true;
      setSending(relationId);
      try {
        await apiClient.undoRelation(relationId);
        store.drop(relationId);
        onNotice("已撤销");
        await refetch();
        return true;
      } catch (reason) {
        // 过了撤销期、已经撤销过、行没了：这一行再留着也撤不了
        if (reason instanceof ApiError && !isOldBackend(reason) && (reason.status === 409 || reason.status === 404)) {
          store.drop(relationId);
        }
        showFailure(reason, "撤销失败");
        return false;
      } finally {
        sendingRef.current = false;
        setSending(null);
      }
    },
    [apiClient, onNotice, refetch, showFailure, store],
  );

  return { answer, undo, sending, gone, recent, oldBackend };
}

export type RelationAnswering = ReturnType<typeof useRelationAnswer>;
