import { useCallback, useEffect, useState } from "react";

import { ApiError, type ApiClient } from "../api";
import type { Task, TaskStatus, Project } from "../types";
import { AsyncState } from "./AsyncState";
import { TaskEditModal } from "./TaskEditModal";
import { TaskReExtractModal } from "./TaskReExtractModal";
import "./MeetingTasksPanel.css";

interface MeetingTasksPanelProps {
  apiClient: ApiClient;
  meetingId: string;
  meetingTitle: string;
  canWrite: boolean;
  onOpenTasks: () => void;
  /** 任务确认 / 驳回 / 修改 / 重抽后回调，供父级刷新角标 */
  onChanged?: () => void;
  /** 修改任务弹窗里的项目下拉；不传时下拉为空、选不了项目 */
  projects?: Project[];
}

type LoadState = "loading" | "ready" | "error";

/** 只读分组的任务状态文案；待确认行不走标签，用左侧圆点 + 操作按钮 */
const STATUS_LABEL: Record<TaskStatus, string> = {
  pending_confirm: "待确认",
  confirmed: "已确认",
  in_progress: "进行中",
  done: "已完成",
  cancelled: "已取消",
  expired: "已过期",
};

/** 只读行状态标签的色调，只用现有 token：teal 青 / neutral 中性 / muted 灰 */
const STATUS_TONE: Partial<Record<TaskStatus, "teal" | "neutral" | "muted">> = {
  confirmed: "teal",
  done: "teal",
  in_progress: "neutral",
  cancelled: "muted",
  expired: "muted",
};

export function MeetingTasksPanel({
  apiClient,
  meetingId,
  meetingTitle,
  canWrite,
  onOpenTasks,
  onChanged,
  projects = [],
}: MeetingTasksPanelProps) {
  const [tasks, setTasks] = useState<Task[]>([]);
  const [state, setState] = useState<LoadState>("loading");
  const [notice, setNotice] = useState("");
  const [reExtracting, setReExtracting] = useState(false);
  const [editing, setEditing] = useState<Task | null>(null);
  const [reExtractOpen, setReExtractOpen] = useState(false);

  const load = useCallback(async () => {
    try {
      const payload = await apiClient.tasks({ meeting_id: meetingId, limit: 100 });
      setTasks(payload.items);
      setState("ready");
    } catch {
      setState("error");
    }
  }, [apiClient, meetingId]);

  useEffect(() => {
    void load();
  }, [load]);

  // 三类分组：待确认保留操作，已确认/进行中/已完成合并只读，已取消与已过期的草稿整组标灰
  const pending = tasks.filter((task) => task.status === "pending_confirm");
  const active = tasks.filter(
    (task) => task.status === "confirmed" || task.status === "in_progress" || task.status === "done",
  );
  const cancelled = tasks.filter((task) => task.status === "cancelled" || task.status === "expired");

  const renderCopy = (task: Task) => (
    <div className="meeting-tasks-panel__copy">
      <strong>{task.title}</strong>
      {task.anchor_quote && <small>「{task.anchor_quote.slice(0, 32)}…」</small>}
    </div>
  );

  // 同一时间只处理一条，处理中这一组按钮禁用，连点不会发两次请求。
  const [actingId, setActingId] = useState<string | null>(null);
  const act = async (task: Task, action: () => Promise<unknown>, done: string, failed: string) => {
    if (actingId) return;
    setActingId(task.id);
    try {
      await action();
      setNotice(done);
      await load();
      onChanged?.();
    } catch (error) {
      setNotice(error instanceof Error ? error.message : failed);
    } finally {
      setActingId(null);
    }
  };
  const confirmOne = (task: Task) => act(task, () => apiClient.confirmTask(task.id, {}), "任务已确认", "确认失败");
  const rejectOne = (task: Task) => act(task, () => apiClient.rejectTask(task.id), "任务已驳回", "驳回失败");

  if (state === "loading") {
    return (
      <section className="meeting-tasks-panel" aria-label="本场任务">
        <AsyncState state="loading" />
      </section>
    );
  }
  if (state === "error") {
    return (
      <section className="meeting-tasks-panel" aria-label="本场任务">
        <div className="meeting-tasks-panel__error">
          <span>本场任务读取失败</span>
          <button className="ghost-button" onClick={() => void load()} type="button">重试</button>
        </div>
      </section>
    );
  }
  if (tasks.length === 0) {
    return (
      <section className="meeting-tasks-panel" aria-label="本场任务">
        <p className="meeting-tasks-panel__empty">本场会暂无任务。</p>
      </section>
    );
  }

  return (
    <section className="meeting-tasks-panel" aria-label="本场任务">
      <div className="meeting-tasks-panel__head">
        <div className="meeting-tasks-panel__title">
          本场任务
          {pending.length > 0 && <em className="meeting-tasks-panel__count">{pending.length}</em>}
        </div>
        {canWrite && (
          <button
            className="ghost-button"
            disabled={reExtracting}
            onClick={() => setReExtractOpen(true)}
            type="button"
          >
            重新抽取
          </button>
        )}
      </div>
      {pending.length > 0 && (
        <div className="meeting-tasks-panel__group">
          <div className="meeting-tasks-panel__group-title">待确认</div>
          <ul className="meeting-tasks-panel__list">
            {pending.map((task) => (
              <li key={task.id} className="meeting-tasks-panel__row">
                <span className="meeting-tasks-panel__dot" aria-hidden="true" />
                {renderCopy(task)}
                {canWrite ? (
                  <span className="meeting-tasks-panel__actions">
                    <button className="text-button text-button--accent" disabled={actingId !== null} onClick={() => void confirmOne(task)} type="button">
                      {actingId === task.id ? "处理中…" : "确认"}
                    </button>
                    <button className="text-button" disabled={actingId !== null} onClick={() => setEditing(task)} type="button">修改</button>
                    <button className="text-button text-button--muted" disabled={actingId !== null} onClick={() => void rejectOne(task)} type="button">驳回</button>
                  </span>
                ) : null}
              </li>
            ))}
          </ul>
        </div>
      )}

      {active.length > 0 && (
        <div className="meeting-tasks-panel__group">
          <div className="meeting-tasks-panel__group-title">已确认 · 进行中</div>
          <ul className="meeting-tasks-panel__list">
            {active.map((task) => (
              <li key={task.id} className="meeting-tasks-panel__row">
                <span className={`meeting-tasks-panel__status meeting-tasks-panel__status--${STATUS_TONE[task.status] ?? "neutral"}`}>
                  {STATUS_LABEL[task.status]}
                </span>
                {renderCopy(task)}
              </li>
            ))}
          </ul>
        </div>
      )}

      {cancelled.length > 0 && (
        <div className="meeting-tasks-panel__group">
          <div className="meeting-tasks-panel__group-title">已取消 · 已过期</div>
          <ul className="meeting-tasks-panel__list">
            {cancelled.map((task) => (
              <li key={task.id} className="meeting-tasks-panel__row meeting-tasks-panel__row--muted">
                <span className={`meeting-tasks-panel__status meeting-tasks-panel__status--${STATUS_TONE[task.status] ?? "muted"}`}>
                  {STATUS_LABEL[task.status]}
                </span>
                {renderCopy(task)}
              </li>
            ))}
          </ul>
        </div>
      )}
      {tasks.length > 0 && (
        <button className="meeting-tasks-panel__more" onClick={onOpenTasks} type="button">
          在任务页查看全部 →
        </button>
      )}
      {notice && (
        <div className="action-banner" role="status">{notice}</div>
      )}
      {editing && (
        <TaskEditModal
          apiClient={apiClient}
          canWrite={canWrite}
          onClose={() => setEditing(null)}
          onSaved={() => {
            setEditing(null);
            void load();
            onChanged?.();
          }}
          projects={projects}
          task={editing}
        />
      )}
      {reExtractOpen && (
        <TaskReExtractModal
          apiClient={apiClient}
          meetingId={meetingId}
          meetingTitle={meetingTitle}
          onClose={() => setReExtractOpen(false)}
          onReExtracted={() => {
            setReExtractOpen(false);
            void load();
            onChanged?.();
          }}
          taskCount={pending.length}
        />
      )}
    </section>
  );
}
