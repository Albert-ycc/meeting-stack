import { useEffect, useRef, useState, type ReactNode } from "react";

import type { ApiClient } from "../../api";
import type { RequirementDetail, RequirementStatus, Task, TaskStatus } from "../../types";
import { REQUIREMENT_STATUS_LABELS } from "../RequirementBadges";
import type { ToastOptions } from "../Toast";
import { CloseTasksDialog } from "./CloseTasksDialog";

/** 没做完的待办：和后端 OPEN_TASK_STATUSES 同一组 */
export const OPEN_TASK_STATUSES: readonly TaskStatus[] = ["pending_confirm", "confirmed", "in_progress"];

export function openTasksOf(tasks: Task[]): Task[] {
  return tasks.filter((task) => OPEN_TASK_STATUSES.includes(task.status));
}

/** 改完状态的提示（D9）；一起关掉了待办时多说一句关了几条 */
export function statusChangedMessage(status: RequirementStatus, title: string, closedTaskCount = 0): string {
  if (status === "active") return `已重新打开「${title}」`;
  const head = `${status === "done" ? "已完成" : "已搁置"}「${title}」`;
  return closedTaskCount > 0 ? `${head}，一起关掉 ${closedTaskCount} 条待办` : head;
}

/** 撤销改状态以后（D11） */
export function statusUndoneMessage(requirement: Pick<RequirementDetail, "title" | "status">): string {
  return `「${requirement.title}」回到${REQUIREMENT_STATUS_LABELS[requirement.status]}了`;
}

export interface StatusTarget {
  id: string;
  title: string;
  open_task_count: number;
  /** 详情页、修改页手里已经有待办清单就传；海报上没有，有没做完的待办时现取一次详情 */
  tasks?: Task[];
}

interface Asking {
  target: StatusTarget;
  status: RequirementStatus;
  tasks: Task[];
}

/**
 * 海报「⋯」和详情页头部共用的改状态（D9～D11）：
 * 标记完成、搁置时没有没做完的待办就直接改；有就先弹 CloseTasksDialog 问一起关掉还是留着，
 * 两个按钮都会改状态，只是 close_open_tasks 不同；✕、Esc 什么都不改。重新打开直接改。
 * 改成了提示一句带［撤销］，撤销调 status-undo（详情页的常驻撤销入口也用 undo）。成没成都调 onChanged，让页面换成最新的：
 * 旧标签页里对已经并走的需求操作，后端回的原因直接提示，刷新后海报消失、详情页换成「已并入」。
 */
export function useRequirementStatusChange(
  apiClient: ApiClient,
  showToast: (message: string, options?: ToastOptions) => void,
  onChanged: () => void | Promise<void>,
): {
  change: (target: StatusTarget, status: RequirementStatus) => Promise<void>;
  undo: (requirementId: string) => Promise<void>;
  dialog: ReactNode;
  busyId: string | null;
} {
  const [asking, setAsking] = useState<Asking | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);
  // 提示上的［撤销］停 10 秒，期间页面的筛选、打开的需求可能已经换了：回调里用最新的刷新
  const changedRef = useRef(onChanged);
  useEffect(() => {
    changedRef.current = onChanged;
  });

  const undo = async (requirementId: string) => {
    setBusyId(requirementId);
    try {
      showToast(statusUndoneMessage(await apiClient.undoRequirementStatus(requirementId)));
    } catch (error) {
      showToast(error instanceof Error ? error.message : "撤销失败，请稍后重试", { tone: "error" });
    } finally {
      setBusyId(null);
    }
    await changedRef.current();
  };

  const apply = async (target: StatusTarget, status: RequirementStatus, closeOpenTasks?: boolean) => {
    setBusyId(target.id);
    try {
      const updated = await apiClient.updateRequirement(
        target.id,
        closeOpenTasks === undefined ? { status } : { status, close_open_tasks: closeOpenTasks },
      );
      showToast(statusChangedMessage(status, updated.title, updated.closed_task_count ?? 0), {
        onUndo: () => undo(target.id),
      });
    } catch (error) {
      showToast(error instanceof Error ? error.message : "没改成，请稍后重试", { tone: "error" });
    } finally {
      setBusyId(null);
    }
    await changedRef.current();
  };

  const change = async (target: StatusTarget, status: RequirementStatus) => {
    if (status === "active") return apply(target, status);
    let tasks = target.tasks;
    if (!tasks) {
      if (target.open_task_count === 0) return apply(target, status);
      setBusyId(target.id);
      try {
        tasks = (await apiClient.requirement(target.id)).tasks;
      } catch (error) {
        showToast(error instanceof Error ? error.message : "读取待办失败，请稍后重试", { tone: "error" });
        setBusyId(null);
        await changedRef.current();
        return;
      }
      setBusyId(null);
    }
    const open = openTasksOf(tasks);
    // 海报上的数是取墙那一刻的：待办在别处已经做完了，就照没有待办直接改
    if (open.length === 0) return apply(target, status);
    setAsking({ target, status, tasks: open });
  };

  const dialog = asking ? (
    <CloseTasksDialog
      onCancel={() => setAsking(null)}
      onDecide={(closeOpenTasks) => {
        setAsking(null);
        void apply(asking.target, asking.status, closeOpenTasks);
      }}
      tasks={asking.tasks}
    />
  ) : null;

  return { change, undo, dialog, busyId };
}
