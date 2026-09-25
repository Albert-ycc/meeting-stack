import { useEffect, useRef, useState } from "react";

import type { ApiClient } from "../api";
import type { RequirementDetail, Task, TaskStatus } from "../types";
import "./LinkTasksModal.css";

interface LinkTasksModalProps {
  apiClient: ApiClient;
  requirementId: string;
  projectId: string;
  onCancel: () => void;
  onSaved: (requirement: RequirementDetail) => void;
}

const STATUS_LABEL: Record<TaskStatus, string> = {
  pending_confirm: "待确认",
  confirmed: "已确认",
  in_progress: "进行中",
  done: "已完成",
  cancelled: "已取消",
  expired: "已过期",
};

const STATUS_ORDER: TaskStatus[] = [
  "pending_confirm",
  "confirmed",
  "in_progress",
  "done",
  "expired",
  "cancelled",
];

const DEFAULT_STATUSES: TaskStatus[] = ["pending_confirm", "confirmed", "in_progress"];

/** 关联已有任务弹窗（A-04-3）：只列该项目下还没挂到任何需求上的任务。 */
export function LinkTasksModal({ apiClient, requirementId, projectId, onCancel, onSaved }: LinkTasksModalProps) {
  const [search, setSearch] = useState("");
  const [statuses, setStatuses] = useState<Set<TaskStatus>>(new Set(DEFAULT_STATUSES));
  const [statusMenuOpen, setStatusMenuOpen] = useState(false);
  const [tasks, setTasks] = useState<Task[] | null>(null);
  const [loadError, setLoadError] = useState(false);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const statusFieldRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    let active = true;
    setTasks(null);
    void apiClient
      .tasks({
        project_id: projectId,
        requirement_id: "none",
        status: [...statuses].join(","),
        q: search.trim() || undefined,
        limit: 200,
      })
      .then((payload) => {
        if (active) setTasks(payload.items);
      })
      .catch(() => {
        if (active) setLoadError(true);
      });
    return () => {
      active = false;
    };
  }, [apiClient, projectId, search, statuses]);

  useEffect(() => {
    if (!statusMenuOpen) return;
    const onPointerDown = (event: MouseEvent) => {
      if (statusFieldRef.current && !statusFieldRef.current.contains(event.target as Node)) {
        setStatusMenuOpen(false);
      }
    };
    document.addEventListener("mousedown", onPointerDown);
    return () => document.removeEventListener("mousedown", onPointerDown);
  }, [statusMenuOpen]);

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !saving) onCancel();
    };
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [onCancel, saving]);

  const toggleStatus = (status: TaskStatus) =>
    setStatuses((current) => {
      const next = new Set(current);
      if (next.has(status)) next.delete(status);
      else next.add(status);
      return next;
    });

  const toggleTask = (taskId: string) =>
    setSelected((current) => {
      const next = new Set(current);
      if (next.has(taskId)) next.delete(taskId);
      else next.add(taskId);
      return next;
    });

  const confirm = async () => {
    setSaving(true);
    setError("");
    try {
      const updated = await apiClient.attachRequirementTasks(requirementId, [...selected]);
      onSaved(updated);
    } catch (err) {
      setError(err instanceof Error ? err.message : "保存失败，请稍后重试");
      setSaving(false);
    }
  };

  const statusLabel = STATUS_ORDER.filter((status) => statuses.has(status))
    .map((status) => STATUS_LABEL[status])
    .join("、") || "全部状态";

  return (
    <div className="link-tasks-modal__overlay" onClick={() => { if (!saving) onCancel(); }}>
      <div
        aria-label="关联已有任务"
        aria-modal="true"
        className="link-tasks-modal__card"
        onClick={(event) => event.stopPropagation()}
        role="dialog"
      >
        <header className="link-tasks-modal__head">
          <h2>关联已有任务</h2>
          <button aria-label="关闭" disabled={saving} onClick={onCancel} type="button">✕</button>
        </header>
        <div className="link-tasks-modal__filters">
          <input
            aria-label="搜索任务"
            className="link-tasks-modal__search"
            onChange={(event) => setSearch(event.target.value)}
            placeholder="搜索任务"
            value={search}
          />
          <div className="link-tasks-modal__status" ref={statusFieldRef}>
            <button
              aria-expanded={statusMenuOpen}
              aria-haspopup="listbox"
              onClick={() => setStatusMenuOpen((value) => !value)}
              type="button"
            >
              {statusLabel} ▾
            </button>
            {statusMenuOpen && (
              <div className="link-tasks-modal__status-menu" role="listbox">
                {STATUS_ORDER.map((status) => (
                  <label key={status}>
                    <input checked={statuses.has(status)} onChange={() => toggleStatus(status)} type="checkbox" />
                    {STATUS_LABEL[status]}
                  </label>
                ))}
              </div>
            )}
          </div>
        </div>
        {tasks === null ? (
          <div className="link-tasks-modal__state">{loadError ? "任务读取失败" : "加载中…"}</div>
        ) : (
          <div className="link-tasks-modal__table">
            <div className="link-tasks-modal__row link-tasks-modal__row--head">
              <span />
              <span>任务</span>
              <span>状态</span>
              <span>来源会议</span>
            </div>
            <div className="link-tasks-modal__body">
              {tasks.length === 0 ? (
                <div className="link-tasks-modal__state">没有匹配的任务</div>
              ) : (
                tasks.map((task) => (
                  <label className="link-tasks-modal__row" key={task.id}>
                    <input checked={selected.has(task.id)} onChange={() => toggleTask(task.id)} type="checkbox" />
                    <span className="link-tasks-modal__title">{task.title}</span>
                    <span>{STATUS_LABEL[task.status]}</span>
                    <span className="link-tasks-modal__meeting">{task.meeting_title || "—"}</span>
                  </label>
                ))
              )}
            </div>
          </div>
        )}
        {error && <p className="link-tasks-modal__error" role="alert">{error}</p>}
        <footer className="link-tasks-modal__footer">
          <span>已选 {selected.size} 条</span>
          <span className="link-tasks-modal__actions">
            <button disabled={saving} onClick={onCancel} type="button">取消</button>
            <button className="link-tasks-modal__confirm" disabled={saving} onClick={() => void confirm()} type="button">
              {saving ? "保存中…" : "确定"}
            </button>
          </span>
        </footer>
      </div>
    </div>
  );
}
