import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import type { KeyboardEvent as ReactKeyboardEvent } from "react";

import { AsyncState } from "./AsyncState";
import { Pagination } from "./Pagination";
import { PriorityBadge } from "./RequirementBadges";
import { TaskDrawer } from "./TaskDrawer";
import { TaskEditModal } from "./TaskEditModal";
import type { ApiClient } from "../api";
import type { Project, RequirementSummary, Task, TaskAssignee, TaskDetail, TaskStatus } from "../types";

import "./TasksPage.css";

interface TasksPageProps {
  apiClient: ApiClient;
  canWrite: boolean;
  projects: Project[];
  onOpenProject: (projectId: string) => void;
  onOpenMeeting: (meetingId: string, seekMs?: number) => void;
  onOpenRequirement: (id: string) => void;
}

type TabKey = "all" | "pending" | "in_progress" | "done" | "expired" | "cancelled";
type LoadState = "loading" | "ready" | "error";

const TABS: Array<{ key: TabKey; label: string }> = [
  { key: "all", label: "全部" },
  { key: "pending", label: "待确认" },
  { key: "in_progress", label: "进行中" },
  { key: "done", label: "已完成" },
  { key: "expired", label: "已过期" },
  { key: "cancelled", label: "已取消" },
];

// 每个页签向服务端要哪些状态；页签数字取服务端 counts，不再拉一大页在前端自己数。
const TAB_STATUSES: Record<TabKey, TaskStatus[]> = {
  all: [],
  pending: ["pending_confirm"],
  in_progress: ["confirmed", "in_progress"],
  done: ["done"],
  expired: ["expired"],
  cancelled: ["cancelled"],
};

const EMPTY_MESSAGE: Record<TabKey, string> = {
  all: "这里还没有任务。",
  pending: "这里还没有任务。会议纪要生成后，AI 会自动把拍板事项整理到这里等你确认。",
  in_progress: "这里还没有任务。",
  done: "这里还没有任务。",
  expired: "没有已过期的草稿。待确认草稿放太久没处理会自动归到这里，随时可以恢复。",
  cancelled: "这里还没有任务。",
};

const PAGE_SIZE = 10;
const REQUIREMENT_OPTIONS_LIMIT = 200;
const UNDO_VISIBLE_MS = 15_000;

const STATUS_LABEL: Record<TaskStatus, string> = {
  pending_confirm: "待确认",
  confirmed: "已确认",
  in_progress: "进行中",
  done: "已完成",
  cancelled: "已取消",
  expired: "已过期",
};

const STATUS_TONE: Record<TaskStatus, string> = {
  pending_confirm: "pending",
  confirmed: "progress",
  in_progress: "progress",
  done: "done",
  cancelled: "muted",
  expired: "muted",
};

const ASSIGNEE_SHORT: Record<TaskAssignee, string> = { ai: "AI", me: "我" };

// 页签和所属项目下拉共用一套求和：statuses 为空（全部页签）时把所有状态加起来。
function sumCounts(counts: Partial<Record<TaskStatus, number>>, statuses: TaskStatus[]): number {
  return (statuses.length ? statuses : (Object.keys(counts) as TaskStatus[])).reduce(
    (sum, status) => sum + (counts[status] ?? 0),
    0,
  );
}

export function TasksPage({
  apiClient,
  canWrite,
  projects,
  onOpenProject,
  onOpenMeeting,
  onOpenRequirement,
}: TasksPageProps) {
  const [tasks, setTasks] = useState<Task[]>([]);
  const [total, setTotal] = useState(0);
  const [statusCounts, setStatusCounts] = useState<Partial<Record<TaskStatus, number>>>({});
  const [projectCounts, setProjectCounts] = useState<Record<string, Partial<Record<TaskStatus, number>>> | null>(null);
  const [state, setState] = useState<LoadState>("loading");
  const [notice, setNotice] = useState("");
  const [undoIds, setUndoIds] = useState<string[]>([]);
  const [activeTab, setActiveTab] = useState<TabKey>("all");
  const [page, setPage] = useState(0);
  const [selected, setSelected] = useState<Set<string>>(() => new Set());
  const [busy, setBusy] = useState(false);
  const [menuTaskId, setMenuTaskId] = useState<string | null>(null);
  // 更多操作菜单的键盘操作：Esc 收起并把焦点还给「⋯」，上下键在菜单项间移动。
  const onMenuKeyDown = (event: ReactKeyboardEvent<HTMLDivElement>) => {
    const items = Array.from(event.currentTarget.querySelectorAll<HTMLElement>('[role="menuitem"]'));
    const index = items.indexOf(document.activeElement as HTMLElement);
    if (event.key === "Escape") {
      event.stopPropagation();
      const trigger = event.currentTarget.parentElement?.querySelector<HTMLElement>(".task-menu__trigger");
      setMenuTaskId(null);
      trigger?.focus();
    } else if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      const step = event.key === "ArrowDown" ? 1 : -1;
      items[(index + step + items.length) % items.length]?.focus();
    }
  };
  const [drawerTaskId, setDrawerTaskId] = useState<string | null>(null);
  const [editingTask, setEditingTask] = useState<Task | null>(null);
  const [creating, setCreating] = useState(false);
  const busyRef = useRef(false);
  const loadSeqRef = useRef(0);

  // 查询区：草稿态（输入中）与已应用态（点「查询」才生效）分开，和需求池同一套模式
  const [projectDraft, setProjectDraft] = useState("");
  const [requirementDraft, setRequirementDraft] = useState("");
  const [assigneeDraft, setAssigneeDraft] = useState<"" | TaskAssignee>("");
  const [dateFromDraft, setDateFromDraft] = useState("");
  const [dateToDraft, setDateToDraft] = useState("");
  const [nameDraft, setNameDraft] = useState("");

  const [appliedProjectId, setAppliedProjectId] = useState("");
  const [appliedRequirementId, setAppliedRequirementId] = useState("");
  const [appliedAssignee, setAppliedAssignee] = useState<"" | TaskAssignee>("");
  const [appliedDateFrom, setAppliedDateFrom] = useState("");
  const [appliedDateTo, setAppliedDateTo] = useState("");
  const [appliedName, setAppliedName] = useState("");

  const [requirementOptions, setRequirementOptions] = useState<RequirementSummary[]>([]);

  const load = useCallback(async () => {
    // 请求序号守卫：轮询与写操作尾随的 load 可能并发，慢的旧响应不许覆盖新响应。
    const seq = ++loadSeqRef.current;
    try {
      const statuses = TAB_STATUSES[activeTab];
      const payload = await apiClient.tasks({
        ...(statuses.length ? { status: statuses.join(",") } : {}),
        ...(appliedProjectId ? { project_id: appliedProjectId } : {}),
        ...(appliedRequirementId ? { requirement_id: appliedRequirementId } : {}),
        ...(appliedAssignee ? { assignee: appliedAssignee } : {}),
        ...(appliedDateFrom ? { meeting_date_from: appliedDateFrom } : {}),
        ...(appliedDateTo ? { meeting_date_to: appliedDateTo } : {}),
        ...(appliedName ? { q: appliedName } : {}),
        limit: PAGE_SIZE,
        offset: page * PAGE_SIZE,
      });
      if (seq !== loadSeqRef.current) return;
      // 当前页被操作空了（比如确认掉第 2 页最后一条），退到最后一个有内容的页，而不是停在空页上。
      if (payload.items.length === 0 && page > 0 && payload.total > 0) {
        setPage(Math.max(0, Math.ceil(payload.total / PAGE_SIZE) - 1));
        return;
      }
      setTasks(payload.items);
      setTotal(payload.total);
      setStatusCounts(payload.counts ?? {});
      setProjectCounts(payload.project_counts ?? null);
      // 刷新后已不在列表里的（被别处确认、过期、删除）从勾选里剔掉，批量操作只作用于看得见的行。
      setSelected((current) => {
        if (current.size === 0) return current;
        const visible = new Set(payload.items.map((task) => task.id));
        const next = new Set([...current].filter((id) => visible.has(id)));
        return next.size === current.size ? current : next;
      });
      setState("ready");
      setMenuTaskId(null); // 列表刷新后收起更多菜单，避免弹层挂在已卸载行上
    } catch {
      if (seq !== loadSeqRef.current) return;
      setState("error");
    }
  }, [
    apiClient,
    activeTab,
    appliedProjectId,
    appliedRequirementId,
    appliedAssignee,
    appliedDateFrom,
    appliedDateTo,
    appliedName,
    page,
  ]);

  useEffect(() => {
    void load();
  }, [load]);

  // 轻量轮询：后台扫描刚给某场会抽出的新草稿，停留本页时也能出现。
  useEffect(() => {
    const interval = window.setInterval(() => {
      if (!document.hidden) void load();
    }, 20_000);
    return () => window.clearInterval(interval);
  }, [load]);

  // 撤销入口只留一小会儿，过了窗口自动收起。
  useEffect(() => {
    if (undoIds.length === 0) return;
    const timer = window.setTimeout(() => setUndoIds([]), UNDO_VISIBLE_MS);
    return () => window.clearTimeout(timer);
  }, [undoIds]);

  // 所属需求下拉：跟随所属项目草稿收窄范围，只列进行中的需求（未提交查询前就能看到，属于表单自身的联动）
  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const payload = await apiClient.requirements({
          status: "active",
          ...(projectDraft ? { project_id: projectDraft } : {}),
          limit: REQUIREMENT_OPTIONS_LIMIT,
        });
        if (!cancelled) setRequirementOptions(payload.items);
      } catch {
        if (!cancelled) setRequirementOptions([]);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [apiClient, projectDraft]);

  // 换了所属项目草稿，之前选的所属需求可能不属于新项目了，清空重选
  useEffect(() => {
    setRequirementDraft("");
  }, [projectDraft]);

  const tabStatuses = TAB_STATUSES[activeTab];
  const tabCounts: Record<TabKey, number> = {
    all: sumCounts(statusCounts, TAB_STATUSES.all),
    pending: sumCounts(statusCounts, TAB_STATUSES.pending),
    in_progress: sumCounts(statusCounts, TAB_STATUSES.in_progress),
    done: sumCounts(statusCounts, TAB_STATUSES.done),
    expired: sumCounts(statusCounts, TAB_STATUSES.expired),
    cancelled: sumCounts(statusCounts, TAB_STATUSES.cancelled),
  };

  // 所属项目下拉的数字跟随当前页签：切到「进行中」就是各项目还剩多少待办。服务端没给 project_counts 时只显示名字。
  const projectOptions: Array<{ key: string; label: string; count: number | null }> = [
    {
      key: "",
      label: "全部项目",
      count: projectCounts
        ? Object.values(projectCounts).reduce((sum, counts) => sum + sumCounts(counts, tabStatuses), 0)
        : null,
    },
    ...projects.map((project) => ({
      key: project.id,
      label: project.name,
      count: projectCounts ? sumCounts(projectCounts[project.id] ?? {}, tabStatuses) : null,
    })),
    {
      key: "none",
      label: "未归属",
      count: projectCounts ? sumCounts(projectCounts.none ?? {}, tabStatuses) : null,
    },
  ];

  const run = async (action: () => Promise<void>) => {
    if (busyRef.current) return; // ref 级互斥：双击同帧不会连发两个写请求
    busyRef.current = true;
    setBusy(true);
    setUndoIds([]); // 新的写操作开始，上一步的撤销入口作废
    try {
      await action();
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "操作失败，请稍后重试");
    } finally {
      busyRef.current = false;
      setBusy(false);
    }
  };

  // 确认/驳回之后给一个撤销入口：误点不再是一锤子买卖。
  const offerUndo = (ids: string[], message: string) => {
    setNotice(message);
    setUndoIds(ids);
  };

  const confirmOne = (task: Task) =>
    run(async () => {
      await apiClient.confirmTask(task.id, {});
      offerUndo([task.id], "任务已确认");
      await load();
    });

  const rejectOne = (task: Task) =>
    run(async () => {
      await apiClient.rejectTask(task.id);
      offerUndo([task.id], "任务已驳回");
      await load();
    });

  const restoreExpired = (task: Task) =>
    run(async () => {
      await apiClient.setTaskStatus(task.id, "pending_confirm");
      setNotice("已恢复到待确认");
      await load();
    });

  const markDone = (task: Task) =>
    run(async () => {
      await apiClient.setTaskStatus(task.id, "done");
      setNotice("已标记完成");
      await load();
    });

  const startOne = (task: Task) =>
    run(async () => {
      await apiClient.setTaskStatus(task.id, "in_progress");
      setNotice("已开始处理");
      await load();
    });

  const cancelOne = (task: Task) =>
    run(async () => {
      await apiClient.setTaskStatus(task.id, "cancelled");
      setNotice("任务已取消");
      await load();
    });

  const restoreOne = (task: Task) =>
    run(async () => {
      await apiClient.setTaskStatus(task.id, "confirmed");
      setNotice("任务已恢复");
      await load();
    });

  const backOne = (task: Task) =>
    run(async () => {
      await apiClient.setTaskStatus(task.id, "confirmed");
      setNotice("已回退到已确认");
      await load();
    });

  const reviewSelected = (kind: "confirm" | "reject") => {
    const ids = [...selected];
    if (ids.length === 0) return;
    void run(async () => {
      if (kind === "confirm") {
        const result = await apiClient.batchConfirmTasks(ids);
        offerUndo(
          result.confirmed,
          result.failed.length ? `已确认 ${result.confirmed.length} 项，${result.failed.length} 项失败` : `已确认 ${result.confirmed.length} 项`,
        );
      } else {
        const result = await apiClient.batchRejectTasks(ids);
        offerUndo(
          result.rejected,
          result.failed.length ? `已驳回 ${result.rejected.length} 项，${result.failed.length} 项失败` : `已驳回 ${result.rejected.length} 项`,
        );
      }
      setSelected(new Set());
      await load();
    });
  };

  const undoReview = () => {
    const ids = undoIds;
    if (ids.length === 0) return;
    void run(async () => {
      const result = await apiClient.undoTaskReview(ids);
      setNotice(
        result.failed.length
          ? `已撤销 ${result.reverted.length} 项，${result.failed.length} 项已无法撤销`
          : `已撤销 ${result.reverted.length} 项，恢复为待确认`,
      );
      await load();
    });
  };

  const toggleSelected = (taskId: string) =>
    setSelected((current) => {
      const next = new Set(current);
      if (next.has(taskId)) next.delete(taskId);
      else next.add(taskId);
      return next;
    });

  const allVisibleSelected = tasks.length > 0 && tasks.every((task) => selected.has(task.id));
  const toggleSelectAllVisible = () =>
    setSelected((current) => {
      if (allVisibleSelected) {
        const next = new Set(current);
        tasks.forEach((task) => next.delete(task.id));
        return next;
      }
      const next = new Set(current);
      tasks.forEach((task) => next.add(task.id));
      return next;
    });

  const resetListing = () => {
    setMenuTaskId(null); // 切页签清掉更多菜单，避免残留透明遮罩拦截点击
    setSelected(new Set());
    setPage(0);
    setTasks([]);
    setState("loading");
  };

  const switchTab = (key: TabKey) => {
    if (key === activeTab) return;
    resetListing();
    setActiveTab(key);
  };

  const applyQuery = (event: FormEvent) => {
    event.preventDefault();
    setAppliedProjectId(projectDraft);
    setAppliedRequirementId(requirementDraft);
    setAppliedAssignee(assigneeDraft);
    setAppliedDateFrom(dateFromDraft);
    setAppliedDateTo(dateToDraft);
    setAppliedName(nameDraft);
    setPage(0);
    setSelected(new Set());
  };

  const resetQuery = () => {
    setProjectDraft("");
    setRequirementDraft("");
    setAssigneeDraft("");
    setDateFromDraft("");
    setDateToDraft("");
    setNameDraft("");
    setAppliedProjectId("");
    setAppliedRequirementId("");
    setAppliedAssignee("");
    setAppliedDateFrom("");
    setAppliedDateTo("");
    setAppliedName("");
    setPage(0);
    setSelected(new Set());
  };

  const openDrawer = (task: Task) => {
    setMenuTaskId(null);
    setDrawerTaskId(task.id);
  };

  // 行内操作只出后端状态机允许的动作：pending→确认/修改/驳回（平铺）、confirmed→开始处理/完成/取消、
  // in_progress→完成/取消、cancelled→恢复、expired→恢复/直接确认/驳回；done 是终态，不给状态按钮。
  const renderRowActions = (task: Task) => {
    if (!canWrite || task.status === "done") return null;

    const primary: Array<{ label: string; accent?: boolean; act: () => void }> = [];
    const menu: Array<{ label: string; act: () => void }> = [];

    if (task.status === "pending_confirm") {
      // 待确认给任务挂需求要走「修改」，藏进 ⋯ 多一步，所以三个操作都平铺显示
      primary.push({ label: "确认", accent: true, act: () => void confirmOne(task) });
      primary.push({ label: "修改", act: () => setEditingTask(task) });
      primary.push({ label: "驳回", act: () => void rejectOne(task) });
    } else if (task.status === "confirmed" || task.status === "in_progress") {
      if (task.status === "confirmed") {
        primary.push({ label: "开始处理", act: () => void startOne(task) });
      } else {
        menu.push({ label: "回退", act: () => void backOne(task) });
      }
      primary.push({ label: "标记完成", accent: true, act: () => void markDone(task) });
      menu.push({ label: "修改", act: () => setEditingTask(task) });
      menu.push({ label: "取消任务", act: () => void cancelOne(task) });
    } else if (task.status === "cancelled") {
      primary.push({ label: "恢复", act: () => void restoreOne(task) });
    } else if (task.status === "expired") {
      primary.push({ label: "恢复", act: () => void restoreExpired(task) });
      menu.push({ label: "直接确认", act: () => void confirmOne(task) });
      menu.push({ label: "驳回", act: () => void rejectOne(task) });
    }

    if (primary.length === 0 && menu.length === 0) return null;

    return (
      <span className="task-row__actions">
        {primary.map((action) => (
          <button
            className={action.accent ? "text-button text-button--accent" : "text-button"}
            disabled={busy}
            key={action.label}
            onClick={(event) => {
              event.stopPropagation();
              action.act();
            }}
            type="button"
          >
            {action.label}
          </button>
        ))}
        {menu.length > 0 && (
          <span className="task-menu">
            <button
              aria-expanded={menuTaskId === task.id}
              aria-haspopup="menu"
              aria-label="更多操作"
              className="task-menu__trigger"
              disabled={busy}
              onClick={(event) => {
                event.stopPropagation();
                setMenuTaskId(menuTaskId === task.id ? null : task.id);
              }}
              type="button"
            >
              ⋯
            </button>
            {menuTaskId === task.id && (
              <div
                className="task-menu__pop"
                onClick={(event) => event.stopPropagation()}
                onKeyDown={onMenuKeyDown}
                ref={(element) => {
                  // 菜单一打开焦点就落到第一项，键盘用户可以直接上下选。
                  if (element && !element.contains(document.activeElement)) {
                    element.querySelector<HTMLElement>('[role="menuitem"]')?.focus();
                  }
                }}
                role="menu"
              >
                {menu.map((action) => (
                  <button
                    key={action.label}
                    onClick={(event) => {
                      event.stopPropagation();
                      setMenuTaskId(null);
                      action.act();
                    }}
                    role="menuitem"
                    type="button"
                  >
                    {action.label}
                  </button>
                ))}
              </div>
            )}
          </span>
        )}
      </span>
    );
  };

  const showCheckboxColumn = activeTab === "pending" && canWrite;

  const renderRow = (task: Task) => (
    <tr
      className="tasks-table__row"
      key={task.id}
      onClick={() => openDrawer(task)}
      onKeyDown={(event) => {
        // 只响应行本身聚焦时的回车/空格，避免劫持行内按钮（确认/驳回/勾选/所属项目/所属需求）的原生激活
        if (event.target !== event.currentTarget) return;
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          openDrawer(task);
        }
      }}
      role="button"
      tabIndex={0}
    >
      {showCheckboxColumn && (
        <td className="tasks-table__select-cell" onClick={(event) => event.stopPropagation()}>
          <input
            aria-label={`选择任务：${task.title}`}
            checked={selected.has(task.id)}
            className="task-select"
            disabled={busy}
            onChange={() => toggleSelected(task.id)}
            type="checkbox"
          />
        </td>
      )}
      <td className="tasks-table__title">{task.title}</td>
      <td>
        {task.project_name && task.project_id ? (
          <button
            className="tasks-table__project"
            onClick={(event) => {
              event.stopPropagation();
              onOpenProject(task.project_id!);
            }}
            type="button"
          >
            <i aria-hidden="true" style={{ background: task.project_color || "#3ecf8e" }} />
            {task.project_name}
          </button>
        ) : (
          <span className="tasks-table__muted">—</span>
        )}
      </td>
      <td>
        {task.requirement_id && task.requirement_title ? (
          <button
            className="tasks-table__requirement"
            onClick={(event) => {
              event.stopPropagation();
              onOpenRequirement(task.requirement_id!);
            }}
            type="button"
          >
            {task.requirement_title}
          </button>
        ) : (
          <span className="tasks-table__muted">—</span>
        )}
      </td>
      <td>
        <PriorityBadge priority={task.requirement_priority} />
      </td>
      <td>
        <span className={`status-chip status-chip--${STATUS_TONE[task.status] ?? "unknown"}`}>
          {STATUS_LABEL[task.status] ?? task.status}
        </span>
      </td>
      <td>{ASSIGNEE_SHORT[task.assignee] ?? task.assignee}</td>
      <td className="tasks-table__meeting">{task.meeting_title ?? "—"}</td>
      <td>{task.stalled && task.stall_days > 0 ? `${Math.floor(task.stall_days)} 天` : "—"}</td>
      <td onClick={(event) => event.stopPropagation()}>{renderRowActions(task)}</td>
    </tr>
  );

  return (
    <section className="page-content tasks-page">
      <header className="page-heading tasks-heading">
        <div>
          <span className="eyebrow">TASKS / 任务代办</span>
          <h1>任务</h1>
          <p>会议纪要生成的拍板事项会先在这里等你确认，确认后进入你的待办清单。</p>
        </div>
        {canWrite && (
          <button className="tasks-create" onClick={() => setCreating(true)} type="button">
            ＋ 新建任务
          </button>
        )}
      </header>

      <form className="tasks-query" onSubmit={applyQuery}>
        <label className="tasks-query__field">
          <span>所属项目</span>
          <select onChange={(event) => setProjectDraft(event.target.value)} value={projectDraft}>
            {projectOptions.map((option) => (
              <option key={option.key || "all"} value={option.key}>
                {option.label}
                {option.count != null ? `（${option.count}）` : ""}
              </option>
            ))}
          </select>
        </label>
        <label className="tasks-query__field">
          <span>所属需求</span>
          <select onChange={(event) => setRequirementDraft(event.target.value)} value={requirementDraft}>
            <option value="">全部需求</option>
            <option value="none">未归需求</option>
            {requirementOptions.map((requirement) => (
              <option key={requirement.id} value={requirement.id}>
                {requirement.priority} {requirement.title}
              </option>
            ))}
          </select>
        </label>
        <label className="tasks-query__field">
          <span>执行方</span>
          <select
            onChange={(event) => setAssigneeDraft(event.target.value as "" | TaskAssignee)}
            value={assigneeDraft}
          >
            <option value="">全部</option>
            <option value="me">我</option>
            <option value="ai">AI</option>
          </select>
        </label>
        <label className="tasks-query__field">
          <span>来源会议日期</span>
          <span className="tasks-query__daterange">
            <input
              aria-label="开始日期"
              onChange={(event) => setDateFromDraft(event.target.value)}
              type="date"
              value={dateFromDraft}
            />
            <span aria-hidden="true">–</span>
            <input
              aria-label="结束日期"
              onChange={(event) => setDateToDraft(event.target.value)}
              type="date"
              value={dateToDraft}
            />
          </span>
        </label>
        <label className="tasks-query__field">
          <span>任务名称</span>
          <input
            onChange={(event) => setNameDraft(event.target.value)}
            placeholder="输入任务名称"
            value={nameDraft}
          />
        </label>
        <span className="tasks-query__buttons">
          <button className="tasks-query__search" type="submit">
            查询
          </button>
          <button className="tasks-query__reset" onClick={resetQuery} type="button">
            重置
          </button>
        </span>
      </form>

      <div className="tasks-table-card">
        <div aria-label="任务状态" className="tasks-tabs" role="tablist">
          {TABS.map((tab) => (
            <button
              aria-selected={activeTab === tab.key}
              key={tab.key}
              onClick={() => switchTab(tab.key)}
              role="tab"
              type="button"
            >
              {tab.label}
              <span>{tabCounts[tab.key]}</span>
            </button>
          ))}
        </div>

        {notice && (
          <div className="action-banner" role="status">
            {notice}
            {canWrite && undoIds.length > 0 && (
              <button
                className="text-button text-button--accent action-banner__undo"
                disabled={busy}
                onClick={undoReview}
                type="button"
              >
                撤销
              </button>
            )}
          </div>
        )}

        {activeTab === "pending" && canWrite && selected.size > 0 && (
          <div aria-label="批量操作" className="tasks-batchbar" role="toolbar">
            <span>已选 {selected.size} 项</span>
            <button
              className="text-button text-button--accent"
              disabled={busy}
              onClick={() => reviewSelected("confirm")}
              type="button"
            >
              确认所选
            </button>
            <button
              className="text-button"
              disabled={busy}
              onClick={() => reviewSelected("reject")}
              type="button"
            >
              驳回所选
            </button>
            <button
              className="text-button text-button--muted"
              disabled={busy}
              onClick={() => setSelected(new Set())}
              type="button"
            >
              取消选择
            </button>
          </div>
        )}

        {state === "loading" && <AsyncState state="loading" />}
        {state === "error" && <AsyncState message="任务读取失败" state="error" />}
        {state === "ready" && tasks.length === 0 && (
          <AsyncState message={EMPTY_MESSAGE[activeTab]} state="empty" />
        )}
        {state === "ready" && tasks.length > 0 && (
          <>
            <table className="tasks-table">
              <thead>
                <tr>
                  {showCheckboxColumn && (
                    <th>
                      <input
                        aria-label="全选本页"
                        checked={allVisibleSelected}
                        className="task-select"
                        onChange={toggleSelectAllVisible}
                        type="checkbox"
                      />
                    </th>
                  )}
                  <th>任务名称</th>
                  <th>所属项目</th>
                  <th>所属需求</th>
                  <th>优先级</th>
                  <th>状态</th>
                  <th>执行方</th>
                  <th>来源会议</th>
                  <th>停滞</th>
                  <th>操作</th>
                </tr>
              </thead>
              <tbody>{tasks.map(renderRow)}</tbody>
            </table>
            <div className="tasks-table__pagination">
              <p className="tasks-table__count">共 {total} 条</p>
              <Pagination onChange={setPage} page={page} pageCount={Math.max(1, Math.ceil(total / PAGE_SIZE))} />
            </div>
          </>
        )}
      </div>

      {menuTaskId && <div className="task-menu__scrim" onClick={() => setMenuTaskId(null)} />}

      {drawerTaskId && (
        <TaskDrawer
          apiClient={apiClient}
          canWrite={canWrite}
          onChanged={() => void load()}
          onClose={() => setDrawerTaskId(null)}
          onOpenMeeting={onOpenMeeting}
          onOpenRequirement={onOpenRequirement}
          parentBusy={busy}
          taskId={drawerTaskId}
        />
      )}
      {editingTask && (
        <TaskEditModal
          apiClient={apiClient}
          canWrite={canWrite}
          onClose={() => setEditingTask(null)}
          onSaved={() => {
            setEditingTask(null);
            void load();
          }}
          projects={projects}
          task={editingTask as TaskDetail}
        />
      )}
      {creating && (
        <TaskEditModal
          apiClient={apiClient}
          canWrite={canWrite}
          onClose={() => setCreating(false)}
          onSaved={() => {
            setCreating(false);
            void load();
          }}
          projects={projects}
          task={null}
        />
      )}
    </section>
  );
}
