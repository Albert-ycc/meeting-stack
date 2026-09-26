import { useCallback, useEffect, useRef, useState } from "react";

import { type ApiClient } from "../api";
import { formatClock, formatTime, formatTaskEventBody } from "../format";
import type { TaskDetail, TaskStatus } from "../types";
import { AsyncState } from "./AsyncState";
import { DeliverableModal } from "./DeliverableModal";
import { PriorityBadge } from "./RequirementBadges";
import { isComposingKeydown } from "../keyboard";
import "./TaskDrawer.css";
import { useDialogFocus } from "./useDialog";
import { NoticeBanner, useNotice } from "./Notice";

interface TaskDrawerProps {
  apiClient: ApiClient;
  taskId: string;
  canWrite: boolean;
  /** 父级（任务页）正在发起写操作时为 true，避免页面与抽屉对同一任务并发双写 */
  parentBusy?: boolean;
  onClose: () => void;
  onChanged: () => void;
  onOpenMeeting: (meetingId: string, seekMs?: number) => void;
  onOpenRequirement?: (id: string) => void;
}

type LoadState = "loading" | "ready" | "error";

const STATUS_META: Record<TaskStatus, { label: string; tone: string }> = {
  pending_confirm: { label: "待确认", tone: "pending" },
  confirmed: { label: "已确认", tone: "confirmed" },
  in_progress: { label: "进行中", tone: "in-progress" },
  done: { label: "已完成", tone: "done" },
  cancelled: { label: "已取消", tone: "cancelled" },
  expired: { label: "已过期", tone: "cancelled" },
};

export function TaskDrawer({
  apiClient,
  taskId,
  canWrite,
  parentBusy = false,
  onClose,
  onChanged,
  onOpenMeeting,
  onOpenRequirement,
}: TaskDrawerProps) {
  const drawerRef = useRef<HTMLDivElement>(null);
  useDialogFocus(drawerRef);
  const [task, setTask] = useState<TaskDetail | null>(null);
  const [state, setState] = useState<LoadState>("loading");
  const { notice, setNotice, dismissNotice } = useNotice();
  const [busy, setBusy] = useState(false);
  const [comment, setComment] = useState("");
  const [sending, setSending] = useState(false);
  const [deliverableOpen, setDeliverableOpen] = useState(false);
  const busyRef = useRef(false);

  const load = useCallback(async () => {
    try {
      const detail = await apiClient.task(taskId);
      setTask(detail);
      setState("ready");
    } catch {
      setState("error");
    }
  }, [apiClient, taskId]);

  useEffect(() => {
    void load();
  }, [load]);

  // 关闭交互：点遮罩、按 Esc。背景滚动锁定与焦点进出由 useDialogFocus 负责。
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !event.isComposing && !deliverableOpen) onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose, deliverableOpen]);

  const act = async (run: () => Promise<TaskDetail>, doneMessage: string) => {
    if (busyRef.current) return; // ref 级互斥：双击不会连发两个状态请求
    busyRef.current = true;
    setBusy(true);
    setNotice("");
    try {
      const updated = await run();
      setTask(updated);
      setNotice(doneMessage);
      onChanged();
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "操作失败", "error");
    } finally {
      busyRef.current = false;
      setBusy(false);
    }
  };

  const sendComment = async () => {
    const text = comment.trim();
    if (!text || sending) return;
    setSending(true);
    setNotice("");
    try {
      const updated = await apiClient.addTaskComment(taskId, text);
      setTask(updated);
      setComment("");
      setNotice("备注已记下");
      onChanged();
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "备注发送失败", "error");
    } finally {
      setSending(false);
    }
  };

  const statusMeta = task ? STATUS_META[task.status] : null;
  const closed = task?.status === "done" || task?.status === "cancelled";
  const assignedToAi = task?.assignee === "ai";

  return (
    <div className="task-drawer" role="dialog" aria-modal="true" aria-label="任务详情" ref={drawerRef}>
      <div className="task-drawer__scrim" onClick={onClose} aria-hidden="true" />
      <aside className="task-drawer__panel">
        <header className="task-drawer__head">
          {statusMeta && (
            <span
              className={`task-drawer__chip task-drawer__chip--status task-drawer__chip--${statusMeta.tone}`}
            >
              {statusMeta.label}
            </span>
          )}
          <button
            className="task-drawer__close"
            onClick={onClose}
            type="button"
            aria-label="关闭"
          >
            ✕
          </button>
        </header>

        <div className="task-drawer__body">
          {state === "loading" && <AsyncState state="loading" />}
          {state === "error" && (
            <div className="task-drawer__error">
              <AsyncState message="任务详情读取失败" state="error" />
              <button className="task-drawer__retry" onClick={() => void load()} type="button">
                重试
              </button>
            </div>
          )}
          {state === "ready" && task && (
            <>
              <h2 className="task-drawer__title">{task.title}</h2>
              {task.detail && <p className="task-drawer__detail">{task.detail}</p>}

              <div className="task-drawer__meta">
                {task.project_name && (
                  <span className="task-drawer__chip task-drawer__chip--project">
                    <i
                      className="task-drawer__chip-dot"
                      style={{ background: task.project_color || "var(--teal)" }}
                    />
                    {task.project_name}
                  </span>
                )}
                <span className="task-drawer__chip task-drawer__chip--assignee">
                  {task.assignee === "ai" ? "交给 AI" : "我来做"}
                </span>
                {task.stalled && (
                  <span className="task-drawer__chip task-drawer__chip--stall">
                    停滞 {Math.floor(task.stall_days)} 天
                  </span>
                )}
              </div>

              {/* 所属需求：优先级只挂在需求上，任务本身不设优先级；没挂需求时不显示这一行 */}
              {task.requirement_id && task.requirement_title && (
                <div className="task-drawer__requirement">
                  <PriorityBadge priority={task.requirement_priority} />
                  {onOpenRequirement ? (
                    <button
                      className="task-drawer__requirement-link"
                      onClick={() => onOpenRequirement(task.requirement_id!)}
                      type="button"
                    >
                      {task.requirement_title}
                    </button>
                  ) : (
                    <span className="task-drawer__requirement-link">{task.requirement_title}</span>
                  )}
                </div>
              )}

              {task.meeting_id && (
                <section className="task-drawer__section">
                  <h3 className="task-drawer__section-label">来源会议</h3>
                  <div className="task-drawer__source-card">
                    <div className="task-drawer__source-head">
                      <strong>{task.meeting_title || "未命名会议"}</strong>
                      {task.anchor_ms != null && (
                        <span className="task-drawer__anchor">{formatTime(task.anchor_ms)}</span>
                      )}
                    </div>
                    {task.anchor_quote && (
                      <p className="task-drawer__quote">「{task.anchor_quote}」</p>
                    )}
                    <button
                      className="task-drawer__open-meeting"
                      onClick={() => onOpenMeeting(task.meeting_id!, task.anchor_ms ?? 0)}
                      type="button"
                    >
                      打开纪要原文 →
                    </button>
                  </div>
                </section>
              )}

              <section className="task-drawer__section">
                <div className="task-drawer__section-head">
                  <h3 className="task-drawer__section-label">交付物</h3>
                  {canWrite && (
                    <button
                      className="task-drawer__add-deliverable"
                      onClick={() => setDeliverableOpen(true)}
                      type="button"
                    >
                      ＋ 登记交付物
                    </button>
                  )}
                </div>
                {task.deliverables.length > 0 ? (
                  <ul className="task-drawer__deliverables">
                    {task.deliverables.map((deliverable) => (
                      <li key={deliverable.id} className="task-drawer__deliverable">
                        <a
                          className="task-drawer__deliverable-title"
                          href={deliverable.url}
                          target="_blank"
                          rel="noreferrer"
                        >
                          {deliverable.title || deliverable.url}
                        </a>
                        {deliverable.note && (
                          <span className="task-drawer__deliverable-note">{deliverable.note}</span>
                        )}
                      </li>
                    ))}
                  </ul>
                ) : (
                  <p className="task-drawer__empty">还没有登记交付物</p>
                )}
              </section>

              <section className="task-drawer__section">
                <h3 className="task-drawer__section-label">讨论轨迹</h3>
                {task.events.length > 0 ? (
                  <ol className="task-drawer__timeline">
                    {task.events.map((event) => (
                      <li key={event.id} className="task-drawer__timeline-item">
                        <span className="task-drawer__timeline-rail" aria-hidden="true">
                          <i className="task-drawer__timeline-dot" />
                        </span>
                        <div className="task-drawer__timeline-body">
                          <time className="task-drawer__timeline-time">
                            {formatClock(event.created_at)}
                          </time>
                          <p className="task-drawer__timeline-text">{formatTaskEventBody(event.body)}</p>
                        </div>
                      </li>
                    ))}
                  </ol>
                ) : (
                  <p className="task-drawer__empty">还没有讨论记录</p>
                )}
                <div className="task-drawer__comment">
                  <input
                    value={comment}
                    onChange={(event) => setComment(event.target.value)}
                    onKeyDown={(event) => {
                      if (event.key === "Enter" && !isComposingKeydown(event) && !sending && comment.trim()) {
                        void sendComment();
                      }
                    }}
                    placeholder="补一句备注或新要求，AI 会跟进…"
                    disabled={!canWrite || sending}
                    aria-label="补充备注"
                  />
                  <button
                    className="task-drawer__comment-send"
                    onClick={() => void sendComment()}
                    disabled={!canWrite || sending || !comment.trim()}
                    type="button"
                  >
                    发送
                  </button>
                </div>
              </section>
            </>
          )}
        </div>

        {state === "ready" && task && (
          <footer className="task-drawer__foot">
            <div className="task-drawer__foot-actions">
              {task.status === "pending_confirm" || task.status === "expired" ? (
                <button
                  className="task-drawer__primary"
                  onClick={() => void act(() => apiClient.confirmTask(taskId, {}), "任务已确认")}
                  disabled={!canWrite || busy || parentBusy}
                  type="button"
                >
                  确认
                </button>
              ) : (
                <button
                  className="task-drawer__primary"
                  onClick={() => void act(() => apiClient.setTaskStatus(taskId, "done"), "已标记完成")}
                  disabled={!canWrite || busy || parentBusy || closed}
                  type="button"
                >
                  标记完成
                </button>
              )}
              <button
                className="task-drawer__secondary"
                onClick={() =>
                  void act(() => apiClient.updateTask(taskId, { assignee: "ai" }), "已转给 AI 执行")
                }
                disabled={!canWrite || busy || parentBusy || closed || assignedToAi}
                type="button"
              >
                转给 AI 执行
              </button>
            </div>
            <button
              className="task-drawer__cancel"
              onClick={() =>
                void act(() => apiClient.setTaskStatus(taskId, "cancelled"), "已取消任务")
              }
              disabled={!canWrite || busy || parentBusy || closed}
              type="button"
            >
              取消任务
            </button>
          </footer>
        )}

        <NoticeBanner className="task-drawer__notice" notice={notice} onDismiss={dismissNotice} />

        {deliverableOpen && (
          <DeliverableModal
            apiClient={apiClient}
            taskId={taskId}
            taskTitle={task?.title ?? ""}
            onClose={() => setDeliverableOpen(false)}
            onSaved={() => {
              setDeliverableOpen(false);
              void load();
              onChanged();
            }}
          />
        )}
      </aside>
    </div>
  );
}
