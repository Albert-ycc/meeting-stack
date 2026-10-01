import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent } from "react";

import { AsyncState } from "./AsyncState";
import type { NoticeTone } from "./Notice";
import { useToast } from "./Toast";
import { Pagination } from "./Pagination";
import { DirectionBar } from "./pool/DirectionBar";
import { PriorityBadge } from "./RequirementBadges";
import { TaskDrawer } from "./TaskDrawer";
import { TaskEditModal } from "./TaskEditModal";
import { ReviewCardsPanel } from "./todo/ReviewCardsPanel";
import { RowMenu, type RowMenuItem } from "./todo/RowMenu";
import { TodoGroups } from "./todo/TodoGroups";
import type { ApiClient } from "../api";
import type {
  LinkOption,
  PoolProject,
  Project,
  RequirementSummary,
  Task,
  TaskAssignee,
  TaskDetail,
  TaskStatus,
  TodoFilters,
  TodoPayload,
} from "../types";
import { usePersistentState } from "../viewState";

import "./TasksPage.css";

interface TasksPageProps {
  apiClient: ApiClient;
  canWrite: boolean;
  projects: Project[];
  onOpenProject: (projectId: string) => void;
  onOpenMeeting: (meetingId: string, seekMs?: number) => void;
  onOpenRequirement: (id: string) => void;
  /** 待确认页签里候选的「认领」：进需求池的认领二级页 */
  onClaimCandidate: (candidateId: string) => void;
  /** 3g：任务抽屉里的文件交付物点了打开预览抽屉 */
  onOpenPreview?: (fileId: number) => void;
}

type TabKey = "pending" | "open" | "done" | "cancelled" | "expired";

const TABS: Array<{ key: TabKey; label: string }> = [
  { key: "pending", label: "待确认" },
  { key: "open", label: "未完成" },
  { key: "done", label: "已完成" },
  { key: "cancelled", label: "已取消" },
  { key: "expired", label: "已过期" },
];

const TAB_KEYS = TABS.map((tab) => tab.key) as string[];

// 每个页签对应哪些状态：页签数字和「我的方向」条上的数字都按它从服务端的计数里加出来。
const TAB_STATUSES: Record<TabKey, TaskStatus[]> = {
  pending: ["pending_confirm"],
  open: ["confirmed", "in_progress"],
  done: ["done"],
  cancelled: ["cancelled"],
  expired: ["expired"],
};

// 已完成、已取消、已过期三个页签是分页的清单；待确认走审核卡，未完成走分组。
type ListTab = "done" | "cancelled" | "expired";
const LIST_TABS: ListTab[] = ["done", "cancelled", "expired"];
const isListTab = (tab: TabKey): tab is ListTab => (LIST_TABS as string[]).includes(tab);

const EMPTY_MESSAGE: Record<ListTab, string> = {
  done: "这里还没有任务。",
  cancelled: "这里还没有任务。",
  expired: "没有已过期的草稿。待确认草稿放太久没处理会自动归到这里，随时可以恢复。",
};

const PAGE_SIZE = 10;
const REQUIREMENT_OPTIONS_LIMIT = 200;
const NONE_PROJECT = "none";
// 完成后能撤销多久：和后端 UNDO_WINDOW_SECONDS=600 对齐
const UNDO_COMPLETE_WINDOW_MS = 10 * 60 * 1000;
const DATE_RE = /^\d{4}-\d{2}-\d{2}$/;
const isValidDate = (value: string) => value === "" || (DATE_RE.test(value) && !Number.isNaN(Date.parse(value)));

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

function sumCounts(counts: Partial<Record<TaskStatus, number>>, statuses: TaskStatus[]): number {
  return statuses.reduce((sum, status) => sum + (counts[status] ?? 0), 0);
}

function toggled<T>(values: T[], value: T): T[] {
  return values.includes(value) ? values.filter((item) => item !== value) : [...values, value];
}

const isStringList = (value: unknown) => Array.isArray(value) && value.every((item) => typeof item === "string");

export function TasksPage({
  apiClient,
  canWrite,
  projects,
  onOpenProject,
  onOpenMeeting,
  onOpenRequirement,
  onClaimCandidate,
  onOpenPreview,
}: TasksPageProps) {
  const [todo, setTodo] = useState<TodoPayload | null>(null);
  const [listTasks, setListTasks] = useState<Task[]>([]);
  const [listTotal, setListTotal] = useState(0);
  // 切到分页清单的页签后，清单还没取回来之前显示加载中，不露出上一个页签的行
  const [listFresh, setListFresh] = useState(false);
  const [state, setState] = useState<"loading" | "ready" | "error">("loading");
  const { toastNode, showToast, hideToast } = useToast();
  // 旧值（本机存过的 all、in_progress）不在页签里，读回来就当没存，落到默认的「未完成」
  const [activeTab, setActiveTab] = usePersistentState<TabKey>("tasks.activeTab", "open", {
    valid: (value) => typeof value === "string" && TAB_KEYS.includes(value),
  });
  const [page, setPage] = usePersistentState("tasks.page", 0);
  const [projectIds, setProjectIds] = usePersistentState<string[]>("tasks.projectIds", [], { valid: isStringList });
  const [busy, setBusy] = useState(false);
  const [reloadKey, setReloadKey] = useState(0);
  const [drawerTaskId, setDrawerTaskId] = useState<string | null>(null);
  const [editingTask, setEditingTask] = useState<Task | null>(null);
  // 从审核卡打开的修改弹窗只保存、不确认：确认留给卡上的［确认］，卡上选的需求不能被弹窗冲掉
  const [editFromCard, setEditFromCard] = useState(false);
  const [loadError, setLoadError] = useState("");
  const [queryError, setQueryError] = useState("");
  const [creating, setCreating] = useState(false);
  const busyRef = useRef(false);
  const loadSeqRef = useRef(0);

  // 查询区：草稿态（输入中）与已应用态（点「查询」才生效）分开，和需求池同一套模式。
  // 所属项目不在这里：它是「我的方向」条上的点选，点一下立刻生效。
  const [requirementDraft, setRequirementDraft] = usePersistentState("tasks.requirementDraft", "");
  const [assigneeDraft, setAssigneeDraft] = usePersistentState<"" | TaskAssignee>("tasks.assigneeDraft", "");
  const [dateFromDraft, setDateFromDraft] = usePersistentState("tasks.dateFromDraft", "");
  const [dateToDraft, setDateToDraft] = usePersistentState("tasks.dateToDraft", "");
  const [nameDraft, setNameDraft] = usePersistentState("tasks.nameDraft", "");

  const [appliedRequirementId, setAppliedRequirementId] = usePersistentState("tasks.appliedRequirementId", "");
  const [appliedAssignee, setAppliedAssignee] = usePersistentState<"" | TaskAssignee>("tasks.appliedAssignee", "");
  const [appliedDateFrom, setAppliedDateFrom] = usePersistentState("tasks.appliedDateFrom", "");
  const [appliedDateTo, setAppliedDateTo] = usePersistentState("tasks.appliedDateTo", "");
  const [appliedName, setAppliedName] = usePersistentState("tasks.appliedName", "");

  const [requirementOptions, setRequirementOptions] = useState<RequirementSummary[]>([]);

  const projectKey = projectIds.join(",");

  const load = useCallback(async () => {
    // 请求序号守卫：轮询与写操作尾随的 load 可能并发，慢的旧响应不许覆盖新响应。
    const seq = ++loadSeqRef.current;
    try {
      const filters: TodoFilters = {
        ...(projectKey ? { project_id: projectKey } : {}),
        ...(appliedRequirementId ? { requirement_id: appliedRequirementId } : {}),
        ...(appliedAssignee ? { assignee: appliedAssignee } : {}),
        ...(appliedDateFrom ? { meeting_date_from: appliedDateFrom } : {}),
        ...(appliedDateTo ? { meeting_date_to: appliedDateTo } : {}),
        ...(appliedName ? { q: appliedName } : {}),
      };
      // 待办接口不论哪个页签都取：页签数字、「我的方向」条的数字和「未完成」的分组都从它来
      const [todoPayload, listPayload] = await Promise.all([
        apiClient.todo(filters),
        isListTab(activeTab)
          ? apiClient.tasks({
              ...filters,
              status: TAB_STATUSES[activeTab].join(","),
              limit: PAGE_SIZE,
              offset: page * PAGE_SIZE,
            })
          : null,
      ]);
      if (seq !== loadSeqRef.current) return;
      // 当前页被操作空了（比如恢复掉最后一页的最后一条），退到最后一个有内容的页，而不是停在空页上。
      if (listPayload && listPayload.items.length === 0 && page > 0 && listPayload.total > 0) {
        setPage(Math.max(0, Math.ceil(listPayload.total / PAGE_SIZE) - 1));
        return;
      }
      setTodo(todoPayload);
      if (listPayload) {
        setListTasks(listPayload.items);
        setListTotal(listPayload.total);
      }
      setListFresh(true);
      setState("ready");
      setLoadError("");
      // 记着的项目已经删掉或合并掉：条上看不到它被选着，列表却被它筛了，去掉（「未归项目」不在条上，保留）
      const known = new Set(todoPayload.projects.map((project) => project.id));
      if (projectIds.some((id) => id !== NONE_PROJECT && !known.has(id))) {
        setProjectIds((current) => current.filter((id) => id === NONE_PROJECT || known.has(id)));
      }
    } catch (error) {
      if (seq !== loadSeqRef.current) return;
      // 条件存在本机，刷新后还会失败：把后端给的原因带出来（比如日期格式不对的 400），并给［重置筛选］
      setLoadError(error instanceof Error ? error.message : "");
      setState("error");
    }
    // projectIds 只用来剪枝，换条件由 projectKey 触发重取
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [
    apiClient,
    activeTab,
    projectKey,
    appliedRequirementId,
    appliedAssignee,
    appliedDateFrom,
    appliedDateTo,
    appliedName,
    page,
  ]);

  // 提示条上的［撤销］可能在筛选变了以后才点：刷新要用最新的 load，不是创建那一刻的
  const loadRef = useRef(load);
  loadRef.current = load;

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

  // 所属需求下拉：只选了一个项目时收窄到该项目进行中的需求（未提交查询前就能看到，属于表单自身的联动）
  const scopedProjectId = projectIds.length === 1 && projectIds[0] !== NONE_PROJECT ? projectIds[0] : "";
  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const payload = await apiClient.requirements({
          status: "active",
          ...(scopedProjectId ? { project_id: scopedProjectId } : {}),
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
  }, [apiClient, scopedProjectId]);

  const counts = todo?.counts ?? {};
  const tabCounts: Record<TabKey, number> = {
    pending: sumCounts(counts, TAB_STATUSES.pending),
    open: sumCounts(counts, TAB_STATUSES.open),
    done: sumCounts(counts, TAB_STATUSES.done),
    cancelled: sumCounts(counts, TAB_STATUSES.cancelled),
    expired: sumCounts(counts, TAB_STATUSES.expired),
  };

  // 「我的方向」条上每个项目的数字跟随当前页签：切到「未完成」就是各项目还剩多少待办。
  const tabStatuses = TAB_STATUSES[activeTab];
  const directionProjects: PoolProject[] = (todo?.projects ?? []).map((project) => ({
    ...project,
    count: sumCounts(todo?.project_counts[project.id] ?? {}, tabStatuses),
  }));
  const noneCount = sumCounts(todo?.project_counts[NONE_PROJECT] ?? {}, tabStatuses);

  const run = async (action: () => Promise<void>) => {
    if (busyRef.current) return; // ref 级互斥：双击同帧不会连发两个写请求
    busyRef.current = true;
    setBusy(true);
    try {
      await action();
    } catch (error) {
      showToast(error instanceof Error ? error.message : "操作失败，请稍后重试", { tone: "error" });
    } finally {
      busyRef.current = false;
      setBusy(false);
    }
  };

  // 页面别处有写操作后：重取本页数据，并让待确认的审核卡也重取
  const refresh = async () => {
    setReloadKey((key) => key + 1);
    await loadRef.current();
  };

  // 页面所有结果提示和撤销都走这一个入口（审核卡也用它）：共用的底部深色条，带 undo 就出［撤销］、停 10 秒；
  // 没成的（提醒、失败）用错误样式。新的提示顶掉旧的，旧条上的撤销入口跟着作废。
  const notify = (message: string, undo?: () => Promise<void>, tone: NoticeTone = "success") => {
    showToast(message, {
      tone: tone === "success" ? undefined : "error",
      onUndo: undo
        ? () =>
            run(async () => {
              await undo();
              await refresh();
            })
        : undefined,
    });
  };

  const undoReview = async (ids: string[]) => {
    const result = await apiClient.undoTaskReview(ids);
    notify(
      result.failed.length
        ? `已撤销 ${result.reverted.length} 项，${result.failed.length} 项已无法撤销`
        : `已撤销 ${result.reverted.length} 项，恢复为待确认`,
      undefined,
      result.failed.length ? "warning" : "success",
    );
  };

  const confirmOne = (task: Task) =>
    run(async () => {
      await apiClient.confirmTask(task.id, {});
      notify("任务已确认", () => undoReview([task.id]));
      await load();
    });

  const rejectOne = (task: Task) =>
    run(async () => {
      await apiClient.rejectTask(task.id);
      notify("任务已驳回", () => undoReview([task.id]));
      await load();
    });

  const restoreExpired = (task: Task) =>
    run(async () => {
      await apiClient.setTaskStatus(task.id, "pending_confirm");
      notify("已恢复到待确认，重新计 7 天");
      await load();
    });

  const completeOne = (task: Task) =>
    run(async () => {
      await apiClient.setTaskStatus(task.id, "done");
      notify(`已完成「${task.title}」`, async () => {
        const result = await apiClient.undoTaskComplete([task.id]);
        if (result.failed.length > 0) {
          notify(result.failed[0].error || "已经过了 10 分钟，没法撤销了", undefined, "warning");
        } else {
          notify(`已撤销，「${task.title}」回到未完成`);
        }
      });
      await load();
    });

  // 「已完成」页签里完成不满 10 分钟的行上的［撤销完成］
  const undoCompleteOne = (task: Task) =>
    run(async () => {
      const result = await apiClient.undoTaskComplete([task.id]);
      if (result.failed.length > 0) {
        notify(result.failed[0].error || "已经过了 10 分钟，没法撤销了", undefined, "warning");
      } else {
        notify(`已撤销，「${task.title}」回到未完成`);
      }
      await load();
    });

  const startOne = (task: Task) =>
    run(async () => {
      await apiClient.setTaskStatus(task.id, "in_progress");
      notify("已开始处理");
      await load();
    });

  const cancelOne = (task: Task) =>
    run(async () => {
      await apiClient.setTaskStatus(task.id, "cancelled");
      notify("任务已取消");
      await load();
    });

  const restoreOne = (task: Task) =>
    run(async () => {
      await apiClient.setTaskStatus(task.id, "confirmed");
      notify("任务已恢复");
      await load();
    });

  const backOne = (task: Task) =>
    run(async () => {
      await apiClient.setTaskStatus(task.id, "confirmed");
      notify("已回退到已确认");
      await load();
    });

  // 挂到需求：选需求写 requirement_id，选候选写 candidate_id，「不挂」两个都清；撤销把原来的挂接写回去。
  const linkOne = (task: Task, option: LinkOption | null) =>
    run(async () => {
      const body =
        option === null
          ? { requirement_id: null, candidate_id: null }
          : option.kind === "requirement"
            ? { requirement_id: option.id }
            : { candidate_id: option.id };
      const original = task.requirement_id
        ? { requirement_id: task.requirement_id }
        : task.candidate_id
          ? { candidate_id: task.candidate_id }
          : { requirement_id: null, candidate_id: null };
      await apiClient.updateTask(task.id, body);
      notify(option ? `已挂到「${option.title}」` : "已设为不挂需求", async () => {
        await apiClient.updateTask(task.id, original);
        notify("已撤销，挂接回到原来的样子");
      });
      await load();
    });

  const switchTab = (key: TabKey) => {
    if (key === activeTab) return;
    hideToast(); // 上一个页签的提示（尤其是失败）不带到新页签
    setPage(0);
    setListFresh(false);
    setListTasks([]);
    setActiveTab(key);
  };

  const toggleProject = (projectId: string) => {
    setProjectIds((current) => toggled(current, projectId));
    // 换了项目范围，之前按的所属需求可能不在新范围里，清掉免得结果莫名为空
    if (requirementDraft || appliedRequirementId) {
      setRequirementDraft("");
      setAppliedRequirementId("");
    }
    setPage(0);
  };

  const saveSeats = async (ids: string[]) => {
    try {
      await apiClient.saveProjectSeats(ids);
      notify("座次已保存");
    } catch {
      // 多半是项目在别处改过（删掉、合并、另一个窗口排过座次）：重新取一遍，用新的一排再拖
      notify("座次没保存：项目有变化，已刷新，请再拖一次", undefined, "warning");
    }
    await load();
  };

  const applyQuery = (event: FormEvent) => {
    event.preventDefault();
    if (!isValidDate(dateFromDraft) || !isValidDate(dateToDraft)) {
      setQueryError("来源会议日期要填成 年-月-日，例如 2026-09-30");
      return;
    }
    setQueryError("");
    setAppliedRequirementId(requirementDraft);
    setAppliedAssignee(assigneeDraft);
    setAppliedDateFrom(dateFromDraft);
    setAppliedDateTo(dateToDraft);
    setAppliedName(nameDraft);
    setPage(0);
  };

  const resetQuery = () => {
    setQueryError("");
    setProjectIds([]);
    setRequirementDraft("");
    setAssigneeDraft("");
    setDateFromDraft("");
    setDateToDraft("");
    setNameDraft("");
    setAppliedRequirementId("");
    setAppliedAssignee("");
    setAppliedDateFrom("");
    setAppliedDateTo("");
    setAppliedName("");
    setPage(0);
  };

  // 审核卡只认会议这一层的筛选；对象要稳定，免得面板每次渲染都重取
  const panelFilters = useMemo(
    () => ({
      ...(projectKey ? { project_id: projectKey } : {}),
      ...(appliedDateFrom ? { meeting_date_from: appliedDateFrom } : {}),
      ...(appliedDateTo ? { meeting_date_to: appliedDateTo } : {}),
    }),
    [projectKey, appliedDateFrom, appliedDateTo],
  );

  // 分页清单里的行内操作：cancelled→恢复、expired→恢复/直接确认/驳回；done 只在完成 10 分钟内给［撤销完成］。
  const renderRowActions = (task: Task) => {
    if (!canWrite) return null;
    if (task.status === "done") {
      const doneAt = Date.parse(task.status_changed_at);
      if (!(Date.now() - doneAt < UNDO_COMPLETE_WINDOW_MS)) return null;
      return (
        <span className="task-row__actions">
          <button
            aria-label={`撤销完成：${task.title}`}
            className="text-button text-button--accent"
            disabled={busy}
            onClick={() => void undoCompleteOne(task)}
            type="button"
          >
            撤销完成
          </button>
        </span>
      );
    }
    if (task.status === "cancelled") {
      return (
        <span className="task-row__actions">
          <button
            aria-label={`恢复：${task.title}`}
            className="text-button"
            disabled={busy}
            onClick={() => void restoreOne(task)}
            type="button"
          >
            恢复
          </button>
        </span>
      );
    }
    if (task.status === "expired") {
      const menu: RowMenuItem[] = [
        { label: "直接确认", act: () => void confirmOne(task) },
        { label: "驳回", act: () => void rejectOne(task) },
      ];
      return (
        <span className="task-row__actions">
          <button
            aria-label={`恢复：${task.title}`}
            className="text-button"
            disabled={busy}
            onClick={() => void restoreExpired(task)}
            type="button"
          >
            恢复
          </button>
          <RowMenu disabled={busy} items={menu} label={`更多操作：${task.title}`} />
        </span>
      );
    }
    return null;
  };

  const renderRow = (task: Task) => (
    <tr
      className="tasks-table__row"
      key={task.id}
      onClick={() => setDrawerTaskId(task.id)}
      onKeyDown={(event) => {
        // 只响应行本身聚焦时的回车/空格，避免劫持行内按钮的原生激活
        if (event.target !== event.currentTarget) return;
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          setDrawerTaskId(task.id);
        }
      }}
      role="button"
      tabIndex={0}
    >
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
            <span className="tasks-table__project-name">{task.project_name}</span>
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
            <span className="tasks-table__requirement-name">{task.requirement_title}</span>
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

  const pendingCount = tabCounts.pending;

  return (
    <section className="page-content tasks-page">
      <header className="tasks-head">
        <div className="tasks-head__title">
          <span className="tasks-head__eyebrow">TODO / 待办</span>
          <div className="tasks-head__row">
            <h1>待办</h1>
            <p>会上答应的事和需求拆出来的步骤，都在这里按截止排</p>
          </div>
        </div>
        <div className="tasks-head__side">
          <div className="tasks-count">
            <strong>{todo ? tabCounts.open : "–"}</strong>
            <span>件 未完成</span>
          </div>
          {canWrite && (
            <button className="tasks-create" onClick={() => setCreating(true)} type="button">
              ＋ 新建任务
            </button>
          )}
        </div>
      </header>

      <div aria-label="任务状态" className="tasks-tabs" role="tablist">
        {TABS.map((tab) => (
          <button
            aria-selected={activeTab === tab.key}
            className={tab.key === "pending" && pendingCount > 0 ? "has-dot" : undefined}
            key={tab.key}
            onClick={() => switchTab(tab.key)}
            role="tab"
            type="button"
          >
            {tab.label}
            <span>{todo ? tabCounts[tab.key] : "–"}</span>
          </button>
        ))}
      </div>

      {/* 「未归项目」在条内、芯片之后：DirectionBar 本身不能改，这里让它的根 display: contents，
          标签、芯片、未排座次、提示都成为这个白底条的子项，用 order 把竖线和虚线胶囊插到提示之前 */}
      <div className="todo-direction">
        <DirectionBar
          canWrite={canWrite}
          onSeatsChange={saveSeats}
          onToggle={toggleProject}
          projects={directionProjects}
          selected={projectIds}
        />
        <span aria-hidden="true" className="todo-direction__sep" />
        <button
          aria-pressed={projectIds.includes(NONE_PROJECT)}
          className={`direction-chip todo-direction__none ${projectIds.includes(NONE_PROJECT) ? "is-selected" : ""}`}
          onClick={() => toggleProject(NONE_PROJECT)}
          type="button"
        >
          未归项目
          <span className="direction-chip__count">{noneCount}</span>
        </button>
      </div>

      <form className="tasks-query" onSubmit={applyQuery}>
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
              max="2099-12-31"
              min="2000-01-01"
              onChange={(event) => setDateFromDraft(event.target.value)}
              type="date"
              value={dateFromDraft}
            />
            <span aria-hidden="true">–</span>
            <input
              aria-label="结束日期"
              max="2099-12-31"
              min="2000-01-01"
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

      {queryError && (
        <p className="tasks-query__error" role="alert">
          {queryError}
        </p>
      )}

      {/* 固定在视口底部的浮层，不占文档流：出现、消失时列表不会跳，连点也不会点错行 */}
      {toastNode}

      <div className={`tasks-table-card ${isListTab(activeTab) ? "" : "tasks-table-card--floating"}`}>
        {state === "error" && (
          <>
            <AsyncState message={loadError ? `任务读取失败：${loadError}` : "任务读取失败"} state="error" />
            <p className="tasks-error-actions">
              <button
                className="text-button text-button--accent"
                onClick={() => {
                  resetQuery();
                  void load();
                }}
                type="button"
              >
                重置筛选
              </button>
            </p>
          </>
        )}
        {state === "loading" && <AsyncState state="loading" />}

        {state === "ready" && activeTab === "pending" && (
          <>
            <p className="tasks-hint">待确认放 7 天没处理会自动过期</p>
            <ReviewCardsPanel
              apiClient={apiClient}
              canWrite={canWrite}
              filters={panelFilters}
              onChanged={() => void load()}
              onClaimCandidate={onClaimCandidate}
              onEditTask={(task) => {
                setEditFromCard(true);
                setEditingTask(task);
              }}
              onNotify={notify}
              onOpenMeeting={onOpenMeeting}
              reloadKey={reloadKey}
            />
          </>
        )}

        {state === "ready" && activeTab === "open" && todo && (
          <TodoGroups
            apiClient={apiClient}
            busy={busy}
            canWrite={canWrite}
            groups={todo.groups}
            onBack={(task) => void backOne(task)}
            onCancel={(task) => void cancelOne(task)}
            onComplete={(task) => void completeOne(task)}
            onEdit={(task) => {
              setEditFromCard(false);
              setEditingTask(task);
            }}
            onLink={(task, option) => void linkOne(task, option)}
            onOpenMeeting={onOpenMeeting}
            onOpenProject={onOpenProject}
            onOpenRequirement={onOpenRequirement}
            onOpenTask={(task) => setDrawerTaskId(task.id)}
            onStart={(task) => void startOne(task)}
            today={todo.today}
          />
        )}

        {state === "ready" && isListTab(activeTab) && (
          <>
            {activeTab === "expired" && <p className="tasks-hint">待确认放 7 天没处理自动归到这里</p>}
            {!listFresh && <AsyncState state="loading" />}
            {listFresh && listTasks.length === 0 && <AsyncState message={EMPTY_MESSAGE[activeTab]} state="empty" />}
            {listFresh && listTasks.length > 0 && (
              <>
                <table className="tasks-table">
                  <thead>
                    <tr>
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
                  <tbody>{listTasks.map(renderRow)}</tbody>
                </table>
                <div className="tasks-table__pagination">
                  <p className="tasks-table__count">共 {listTotal} 条</p>
                  <Pagination
                    onChange={setPage}
                    page={page}
                    pageCount={Math.max(1, Math.ceil(listTotal / PAGE_SIZE))}
                  />
                </div>
              </>
            )}
          </>
        )}
      </div>

      {drawerTaskId && (
        <TaskDrawer
          apiClient={apiClient}
          canWrite={canWrite}
          onChanged={() => void refresh()}
          onClose={() => setDrawerTaskId(null)}
          onOpenMeeting={onOpenMeeting}
          onOpenPreview={onOpenPreview}
          onOpenRequirement={onOpenRequirement}
          parentBusy={busy}
          taskId={drawerTaskId}
        />
      )}
      {editingTask && (
        <TaskEditModal
          apiClient={apiClient}
          canWrite={canWrite}
          confirmOnSave={!editFromCard}
          onClose={() => setEditingTask(null)}
          onSaved={() => {
            notify(editFromCard ? `已保存「${editingTask.title}」，确认请在卡上点［确认］` : `已保存「${editingTask.title}」`);
            setEditingTask(null);
            void refresh();
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
            notify("已新建任务");
            setCreating(false);
            void refresh();
          }}
          projects={projects}
          task={null}
        />
      )}
    </section>
  );
}
