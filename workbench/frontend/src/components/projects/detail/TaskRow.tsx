import type { DragEvent } from "react";

import type { ApiClient } from "../../../api";
import { formatMonthDay, formatTime } from "../../../format";
import type { LinkOption, Task } from "../../../types";
import { LinkPicker } from "../../todo/LinkPicker";
import { RowMenu, type RowMenuItem } from "../../todo/RowMenu";
import { dueText } from "./workModel";

export type RowPopover = "link" | "menu" | null;

export interface TaskActions {
  onComplete: (task: Task) => void;
  onStart: (task: Task) => void;
  onBack: (task: Task) => void;
  onEdit: (task: Task) => void;
  onCancel: (task: Task) => void;
  onLink: (task: Task, option: LinkOption | null) => void;
  onOpenTask: (taskId: string) => void;
  onOpenMeeting: (meetingId: string, seekMs?: number) => void;
}

interface TaskRowProps extends TaskActions {
  apiClient: ApiClient;
  task: Task;
  /** panel：某条需求的任务面板；unlinked：没挂需求的任务（带状态列、拖动把手、挂到需求按钮） */
  variant: "panel" | "unlinked";
  canWrite: boolean;
  busy: boolean;
  popover: RowPopover;
  onPopover: (next: RowPopover) => void;
  /** unlinked：本项目没有进行中需求时为 false，不显示「挂到需求」、也不能拖 */
  canLink?: boolean;
  /** unlinked：任务挂着的候选还在本项目待认领名单里才标出候选名；已搬去别的项目或处理掉的不标 */
  candidateVisible?: boolean;
  onDragStart?: (task: Task) => void;
  onDragEnd?: () => void;
}

function LinkIcon() {
  return (
    <svg aria-hidden="true" fill="none" height="12" stroke="currentColor" strokeLinecap="round" strokeLinejoin="round" strokeWidth="1.5" viewBox="0 0 16 16" width="12">
      <path d="M6.8 9.2a2.5 2.5 0 0 0 3.5 0l2.4-2.4a2.5 2.5 0 0 0-3.5-3.5l-.7.7" />
      <path d="M9.2 6.8a2.5 2.5 0 0 0-3.5 0L3.3 9.2a2.5 2.5 0 0 0 3.5 3.5l.7-.7" />
    </svg>
  );
}

function DoneMark() {
  return (
    <svg aria-hidden="true" fill="none" height="12" stroke="currentColor" strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" viewBox="0 0 16 16" width="12">
      <path d="M3.2 8.6l3 3 6.6-7.2" />
    </svg>
  );
}

const STATUS_TEXT: Record<string, string> = { confirmed: "已确认", in_progress: "进行中" };

export function TaskRow({
  apiClient,
  task,
  variant,
  canWrite,
  busy,
  popover,
  onPopover,
  canLink = true,
  candidateVisible = false,
  onDragStart,
  onDragEnd,
  ...actions
}: TaskRowProps) {
  const done = task.status === "done";
  const unlinked = variant === "unlinked";
  const draggable = unlinked && canWrite && canLink && !busy;
  const picking = popover === "link";

  const menu: RowMenuItem[] = [];
  if (task.status === "confirmed") menu.push({ label: "开始处理", act: () => actions.onStart(task) });
  if (task.status === "in_progress") menu.push({ label: "回退", act: () => actions.onBack(task) });
  menu.push({ label: "修改", act: () => actions.onEdit(task) });
  if (!unlinked) menu.push({ label: "改挂需求", act: () => onPopover("link") });
  if (!done) menu.push({ label: "取消任务", danger: true, act: () => actions.onCancel(task) });

  const onDrag = (event: DragEvent) => {
    // Firefox 不带数据就不开始拖动
    event.dataTransfer?.setData("text/plain", task.id);
    if (event.dataTransfer) event.dataTransfer.effectAllowed = "move";
    onDragStart?.(task);
  };

  return (
    <li
      className={`work-row work-row--${variant} ${done ? "is-done" : ""}`}
      draggable={draggable}
      onDragEnd={draggable ? () => onDragEnd?.() : undefined}
      onDragStart={draggable ? onDrag : undefined}
    >
      {unlinked && (
        <span aria-hidden="true" className={`work-row__grip ${draggable ? "" : "is-off"}`} title={draggable ? "拖到某条需求上挂上" : undefined}>
          ⠿
        </span>
      )}
      <label className="work-row__cell work-row__check-cell">
        <input
          aria-label={done ? `已完成「${task.title}」` : `完成「${task.title}」`}
          checked={done}
          className="work-row__check"
          disabled={!canWrite || busy || done}
          onChange={() => actions.onComplete(task)}
          type="checkbox"
        />
      </label>
      <span className="work-row__cell work-row__task">
        <button className="work-row__title" onClick={() => actions.onOpenTask(task.id)} title={task.title} type="button">
          {task.title}
        </button>
        {unlinked && candidateVisible && task.candidate_title && (
          <span className="work-row__candidate" title={task.candidate_title}>
            候选：{task.candidate_title}
          </span>
        )}
      </span>
      {unlinked && (
        <span className="work-row__cell">
          <span className={`work-row__status work-row__status--${task.status}`}>{STATUS_TEXT[task.status] ?? task.status}</span>
        </span>
      )}
      <span className="work-row__cell">
        <span className="work-row__assignee">{task.assignee === "ai" ? "AI" : "我"}</span>
      </span>
      <span className="work-row__cell">
        <span className={`work-row__due ${task.due_date ? "" : "is-undated"}`}>{dueText(task.due_date)}</span>
      </span>
      <span className="work-row__cell work-row__source">
        {task.meeting_title && (
          <>
            <span className="work-row__meeting" title={task.meeting_title}>
              {task.meeting_title}
            </span>
            {task.meeting_recording_date && (
              <span className="work-row__meeting-date"> · {formatMonthDay(task.meeting_recording_date)}</span>
            )}
          </>
        )}
        {task.meeting_id && task.anchor_ms != null && (
          <button
            className="work-row__anchor"
            onClick={() => actions.onOpenMeeting(task.meeting_id!, task.anchor_ms ?? undefined)}
            title="回到录音里这句话"
            type="button"
          >
            ▶ 原话 {formatTime(task.anchor_ms, true)}
          </button>
        )}
      </span>
      <span className="work-row__cell work-row__actions">
        {done ? (
          <span className="work-row__finished">
            <DoneMark />
            已完成
          </span>
        ) : (
          canWrite && (
            <button
              aria-label={`完成「${task.title}」`}
              className="work-row__done"
              disabled={busy}
              onClick={() => actions.onComplete(task)}
              type="button"
            >
              完成
            </button>
          )
        )}
        {unlinked && canWrite && canLink && (
          <span className="work-row__link">
            <button
              aria-expanded={picking}
              aria-haspopup="dialog"
              aria-label={`挂到需求：${task.title}`}
              className="work-row__link-btn"
              disabled={busy}
              // 选择器「点外面收起」听 document 的 mousedown：触发钮自己按下时拦住，否则先收起又被 click 打开
              onClick={() => onPopover(picking ? null : "link")}
              onMouseDown={(event) => event.stopPropagation()}
              type="button"
            >
              <LinkIcon />
              挂到需求
            </button>
            {picking && (
              <LinkPicker
                apiClient={apiClient}
                onClose={() => onPopover(null)}
                onPick={(option) => {
                  onPopover(null);
                  actions.onLink(task, option);
                }}
                projectName={task.project_name}
                selectedId={null}
                taskId={task.id}
              />
            )}
          </span>
        )}
        {canWrite && (
          <span className="work-row__menu">
            <RowMenu
              disabled={busy}
              items={menu}
              label={`更多操作：${task.title}`}
              onOpenChange={(open) => onPopover(open ? "menu" : null)}
              open={popover === "menu"}
            />
            {!unlinked && picking && (
              <LinkPicker
                apiClient={apiClient}
                onClose={() => onPopover(null)}
                onPick={(option) => {
                  onPopover(null);
                  actions.onLink(task, option);
                }}
                projectName={task.project_name}
                selectedId={task.requirement_id ?? task.candidate_id ?? null}
                taskId={task.id}
              />
            )}
          </span>
        )}
      </span>
    </li>
  );
}
