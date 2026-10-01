import { useState } from "react";


import type { ApiClient } from "../../api";
import { formatMonthDay, formatTime } from "../../format";
import { usePersistentState } from "../../viewState";
import type { LinkOption, Task, TodoGroup, TodoGroupKey } from "../../types";
import { LinkPicker } from "./LinkPicker";
import { RowMenu, type RowMenuItem } from "./RowMenu";
import "./TodoGroups.css";

/** 未定截止组默认露出的条数，其余折成「还有 N 条」 */
export const UNDATED_VISIBLE = 8;
/** 停滞满几天在行内标出来 */
const STALL_BADGE_DAYS = 3;

const EMPTY_TEXT: Record<TodoGroupKey, string> = {
  overdue: "没有逾期的任务",
  today: "今天没有到期的任务",
  week: "本周没有到期的任务",
  later: "之后没有到期的任务",
  undated: "没有未定截止的任务",
};

/** 接口给的是 YYYY-MM-DD 的日历日：直接截字符串，不过浏览器时区 */
function monthDay(value: string | null | undefined): string {
  if (!value) return "";
  return /^\d{4}-\d{2}-\d{2}/.test(value) ? value.slice(5, 10) : formatMonthDay(value);
}

/** 逾期天数按接口的「今天」（北京日期）算，不用浏览器时钟 */
export function overdueDays(due: string, today: string): number {
  const toDay = (value: string) => {
    const [year, month, day] = value.slice(0, 10).split("-").map(Number);
    return Date.UTC(year, month - 1, day) / 86_400_000;
  };
  return Math.max(0, toDay(today) - toDay(due));
}

export interface TodoGroupsProps {
  apiClient: ApiClient;
  canWrite: boolean;
  busy: boolean;
  /** 北京日期的今天 */
  today: string;
  groups: TodoGroup[];
  onComplete: (task: Task) => void;
  onStart: (task: Task) => void;
  onBack: (task: Task) => void;
  onEdit: (task: Task) => void;
  onCancel: (task: Task) => void;
  /** 选了需求、候选，或 null（不挂需求） */
  onLink: (task: Task, option: LinkOption | null) => void;
  onOpenTask: (task: Task) => void;
  onOpenMeeting: (meetingId: string, seekMs?: number) => void;
  onOpenProject: (projectId: string) => void;
  onOpenRequirement: (id: string) => void;
}

type RowHandlers = Omit<TodoGroupsProps, "groups" | "today">;

interface TodoRowProps extends RowHandlers {
  task: Task;
  today: string;
  /** 这一行开着哪个弹层：整页同一时刻只开一个（挂到需求选择器和 ⋯ 菜单互斥），由外层管 */
  popover: "link" | "menu" | null;
  onPopover: (next: "link" | "menu" | null) => void;
}

function TodoRow({ task, today, popover, onPopover, ...handlers }: TodoRowProps) {
  const { apiClient, canWrite, busy } = handlers;

  const due = task.due_date ?? null;
  const late = due ? overdueDays(due, today) : 0;
  const picking = popover === "link";
  const linked = Boolean(task.requirement_id || task.candidate_id);
  const stalled = (task.stall_days ?? 0) >= STALL_BADGE_DAYS;

  const menu: RowMenuItem[] = [];
  if (task.status === "confirmed") menu.push({ label: "开始处理", act: () => handlers.onStart(task) });
  if (task.status === "in_progress") menu.push({ label: "回退", act: () => handlers.onBack(task) });
  menu.push({ label: "修改", act: () => handlers.onEdit(task) });
  menu.push({ label: "取消任务", danger: true, act: () => handlers.onCancel(task) });

  return (
    <li className="todo-row">
      {/* 套一层 label：手机上点击区域放大到 32×32 */}
      <label className="todo-row__cell todo-row__check-cell">
        <input
          aria-label={`完成「${task.title}」`}
          checked={false}
          className="todo-row__check"
          disabled={!canWrite || busy}
          onChange={() => handlers.onComplete(task)}
          type="checkbox"
        />
      </label>
      <span className="todo-row__cell todo-row__task">
        <button
          className="todo-row__title"
          onClick={() => handlers.onOpenTask(task)}
          title={task.title}
          type="button"
        >
          {task.title}
        </button>
        {task.status === "in_progress" && <span className="todo-row__state">进行中</span>}
        {stalled && <span className="todo-row__stall">停滞 {Math.floor(task.stall_days)} 天</span>}
      </span>
      {/* 宽屏时 display: contents，五列跟任务、操作同排对齐；窄屏时折成第二行 */}
      <div className="todo-row__meta">
        <span className="todo-row__cell todo-row__req">
          {task.requirement_id && task.requirement_title ? (
            <button
              className="todo-row__requirement"
              onClick={() => handlers.onOpenRequirement(task.requirement_id!)}
              title={task.requirement_title}
              type="button"
            >
              <FlagIcon />
              <span>{task.requirement_title}</span>
            </button>
          ) : task.candidate_id && task.candidate_title ? (
            <span className="todo-row__candidate" title={task.candidate_title}>
              候选：{task.candidate_title}
            </span>
          ) : (
            <span className="todo-row__none-chip">未挂需求</span>
          )}
        </span>
        <span className="todo-row__cell todo-row__project-cell">
          {task.project_id && task.project_name ? (
            <button
              className="todo-row__project"
              onClick={() => handlers.onOpenProject(task.project_id!)}
              title={task.project_name}
              type="button"
            >
              <i aria-hidden="true" style={{ background: task.project_color || "#3ecf8e" }} />
              <span>{task.project_name}</span>
            </button>
          ) : (
            <span className="todo-row__none">未归项目</span>
          )}
        </span>
        <span className="todo-row__cell">
          <span className="todo-row__assignee">{task.assignee === "ai" ? "AI" : "我"}</span>
        </span>
        <span className="todo-row__cell">
          {due ? (
            <span className={`todo-row__due ${late > 0 ? "is-late" : ""}`}>
              {monthDay(due)}
              {late > 0 && ` · 逾期 ${late} 天`}
            </span>
          ) : (
            <span className="todo-row__due is-undated">截止未定</span>
          )}
        </span>
        <span className="todo-row__cell todo-row__source">
          {task.meeting_title && (
            <>
              {/* 只有会议名收缩省略，日期和原话锚不收缩；整段写进 title */}
              <span
                className="todo-row__meeting"
                title={`${task.meeting_title}${task.meeting_recording_date ? ` · ${monthDay(task.meeting_recording_date)}` : ""}`}
              >
                {task.meeting_title}
              </span>
              {task.meeting_recording_date && (
                <span className="todo-row__meeting-date"> · {monthDay(task.meeting_recording_date)}</span>
              )}
            </>
          )}
          {task.meeting_id && task.anchor_ms != null && (
            <button
              className="todo-row__anchor"
              onClick={() => handlers.onOpenMeeting(task.meeting_id!, task.anchor_ms ?? undefined)}
              title="回到录音里这句话"
              type="button"
            >
              ▶ 原话 {formatTime(task.anchor_ms, true)}
            </button>
          )}
        </span>
      </div>
      <span className="todo-row__cell todo-row__actions">
        {canWrite && (
          <>
            <button
              aria-label={`完成「${task.title}」`}
              className="todo-row__done"
              disabled={busy}
              onClick={() => handlers.onComplete(task)}
              type="button"
            >
              完成
            </button>
            <span className="todo-row__link">
              <button
                aria-expanded={picking}
                aria-haspopup="dialog"
                aria-label={`${linked ? "改挂" : "挂到需求"}：${task.title}`}
                className="todo-row__link-btn"
                disabled={busy}
                // 选择器的「点外面收起」听的是 document 的 mousedown：触发钮自己按下时拦住，
                // 否则先被收起、紧接着的 click 又把它打开
                onClick={() => onPopover(picking ? null : "link")}
                onMouseDown={(event) => event.stopPropagation()}
                type="button"
              >
                <LinkIcon />
                {linked ? "改挂" : "挂到需求"}
              </button>
              {picking && (
                <LinkPicker
                  apiClient={apiClient}
                  onClose={() => onPopover(null)}
                  onPick={(option) => {
                    onPopover(null);
                    handlers.onLink(task, option);
                  }}
                  projectName={task.project_name}
                  selectedId={task.requirement_id ?? task.candidate_id ?? null}
                  taskId={task.id}
                />
              )}
            </span>
            <RowMenu
              disabled={busy}
              items={menu}
              label={`更多操作：${task.title}`}
              onOpenChange={(open) => onPopover(open ? "menu" : null)}
              open={popover === "menu"}
            />
          </>
        )}
      </span>
    </li>
  );
}

function FlagIcon() {
  return (
    <svg aria-hidden="true" fill="none" height="11" stroke="currentColor" strokeLinecap="round" strokeWidth="1.5" viewBox="0 0 16 16" width="11">
      <path d="M3.5 14V2.5" />
      <path d="M3.5 2.5h8l-1.6 3 1.6 3h-8" />
    </svg>
  );
}

function LinkIcon() {
  return (
    <svg aria-hidden="true" fill="none" height="12" stroke="currentColor" strokeLinecap="round" strokeLinejoin="round" strokeWidth="1.5" viewBox="0 0 16 16" width="12">
      <path d="M6.8 9.2a2.5 2.5 0 0 0 3.5 0l2.4-2.4a2.5 2.5 0 0 0-3.5-3.5l-.7.7" />
      <path d="M9.2 6.8a2.5 2.5 0 0 0-3.5 0L3.3 9.2a2.5 2.5 0 0 0 3.5 3.5l.7-.7" />
    </svg>
  );
}

/** 「未完成」页签：已确认、进行中的任务按截止分五组（R07-4）。组内顺序后端排好，这里不再排。 */
export function TodoGroups({ groups, today, ...handlers }: TodoGroupsProps) {
  // 展开状态跟着本机记：切页签、去会议页再回来还是展开的
  const [undatedOpen, setUndatedOpen] = usePersistentState("tasks.undatedOpen", false);
  const [popover, setPopover] = useState<{ id: string; kind: "link" | "menu" } | null>(null);

  return (
    <div className="todo-groups">
      {groups.map((group) => {
        const folded = group.key === "undated" && !undatedOpen && group.items.length > UNDATED_VISIBLE;
        const shown = folded ? group.items.slice(0, UNDATED_VISIBLE) : group.items;
        return (
          <section aria-label={group.label} className={`todo-group todo-group--${group.key}`} key={group.key}>
            <h2 className="todo-group__head">
              {group.label}
              <span>{group.count}</span>
            </h2>
            {group.items.length === 0 ? (
              <p className="todo-group__empty">{EMPTY_TEXT[group.key]}</p>
            ) : (
              <div className="todo-group__panel">
                {/* 列头：和每一行同一套列宽（宽屏）；读屏不需要，行内已有完整文字 */}
                <div aria-hidden="true" className="todo-cols">
                  <span />
                  <span>任务</span>
                  <span>需求</span>
                  <span>项目</span>
                  <span>负责人</span>
                  <span>截止</span>
                  <span>来源</span>
                  <span className="todo-cols__ops">操作</span>
                </div>
                <ul className="todo-group__list">
                  {shown.map((task) => (
                    <TodoRow
                      key={task.id}
                      {...handlers}
                      onPopover={(kind) => setPopover(kind ? { id: task.id, kind } : null)}
                      popover={popover?.id === task.id ? popover.kind : null}
                      task={task}
                      today={today}
                    />
                  ))}
                </ul>
              </div>
            )}
            {folded && (
              <button className="todo-group__more" onClick={() => setUndatedOpen(true)} type="button">
                还有 {group.items.length - UNDATED_VISIBLE} 条
              </button>
            )}
            {group.key === "undated" && undatedOpen && group.items.length > UNDATED_VISIBLE && (
              <button className="todo-group__more" onClick={() => setUndatedOpen(false)} type="button">
                收起
              </button>
            )}
          </section>
        );
      })}
    </div>
  );
}
