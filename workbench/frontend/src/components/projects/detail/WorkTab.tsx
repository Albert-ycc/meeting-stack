import { useCallback, useEffect, useRef, useState, type DragEvent } from "react";

import type { ApiClient } from "../../../api";
import type {
  LinkOption,
  PoolItem,
  Project,
  ProjectWorkPayload,
  ProjectWorkRequirement,
  Task,
  TaskDetail,
} from "../../../types";
import { usePersistentState } from "../../../viewState";
import { AsyncState } from "../../AsyncState";
import { PosterCard } from "../../pool/PosterCard";
import { copiedMessage, copyFailureReason, copyRequirementBackground } from "../../pool/requirementCopy";
import { PriorityBadge } from "../../RequirementBadges";
import { TaskEditModal } from "../../TaskEditModal";
import type { ToastOptions } from "../../Toast";
import { TaskPanel } from "./TaskPanel";
import { TaskRow, type RowPopover } from "./TaskRow";
import { linkBody, originalLink, requirementOption } from "./workModel";
import "./detail.css";

interface WorkTabProps {
  apiClient: ApiClient;
  projectId: string;
  projectName: string;
  projectSeat: number | null;
  projects: Project[];
  canWrite: boolean;
  /** 页面级重取（页头的计数等）靠它；这个标签页自己的数据在内部取 */
  reloadKey: number;
  showToast: (message: string, options?: ToastOptions) => void;
  onOpenTask: (taskId: string) => void;
  onOpenMeeting: (meetingId: string, seekMs?: number) => void;
  onOpenRequirement: (id: string) => void;
  /** 写操作成功后通知外层刷新项目列表（待办数、需求数在列表卡片上） */
  onChanged?: () => void;
  /** 横幅里的「去认领」；不传就不画这个按钮 */
  onClaim?: () => void;
}

interface WorkData {
  work: ProjectWorkPayload;
  posters: Map<string, PoolItem>;
}

type Popover = { id: string; kind: Exclude<RowPopover, null> } | null;

const CLOSED_STATUS_TEXT = { done: "已完成", shelved: "已搁置" } as const;

/** 项目详情「需求与任务」（R06-4～13）：待认领横幅、进行中需求（海报＋任务面板）、已完成/已搁置折叠区、没挂需求的任务 */
export function WorkTab({
  apiClient,
  projectId,
  projectName,
  projectSeat,
  projects,
  canWrite,
  reloadKey,
  showToast,
  onOpenTask,
  onOpenMeeting,
  onOpenRequirement,
  onChanged,
  onClaim,
}: WorkTabProps) {
  const [data, setData] = useState<WorkData | null>(null);
  const [state, setState] = useState<"loading" | "ready" | "error">("loading");
  // 写成功之后的重取没取回来：旧数据留着，横条说一声；不去顶掉带［撤销］的提示
  const [stale, setStale] = useState(false);
  const [busy, setBusy] = useState(false);
  const busyRef = useRef(false);
  const requestRef = useRef(0);
  const [popover, setPopover] = useState<Popover>(null);
  const [expanded, setExpanded] = useState<Set<string>>(() => new Set());
  const [closedOpen, setClosedOpen] = usePersistentState(`project.${projectId}.closedOpen`, true);
  const [dragging, setDragging] = useState<Task | null>(null);
  const [overId, setOverId] = useState<string | null>(null);
  const [editing, setEditing] = useState<Task | null>(null);
  const [creatingFor, setCreatingFor] = useState<ProjectWorkRequirement | null>(null);

  // silent：写操作之后的重取，不闪加载态；失败时留着旧数据并提示
  const load = useCallback(
    async (silent = false) => {
      const request = ++requestRef.current;
      if (!silent) setState("loading");
      try {
        const [work, pool] = await Promise.all([
          apiClient.projectWork(projectId),
          apiClient.requirementPool({ status: "active", project_id: projectId }),
        ]);
        if (request !== requestRef.current) return;
        setData({ work, posters: new Map(pool.items.map((item) => [item.id, item])) });
        setStale(false);
        setState("ready");
      } catch {
        if (request !== requestRef.current) return;
        if (silent) setStale(true);
        else setState("error");
      }
    },
    [apiClient, projectId],
  );

  // 写成功后：重取这个标签页，并让外层刷新项目列表
  const refreshAfterWrite = async () => {
    await load(true);
    onChanged?.();
  };

  useEffect(() => {
    void load();
  }, [load, reloadKey]);

  // 写操作互斥：ref 级，双击同帧不会连发两个写请求。runningRef 记着手头这次，撤销要等它做完
  const runningRef = useRef<Promise<void> | null>(null);
  const run = async (action: () => Promise<void>) => {
    if (busyRef.current) return;
    busyRef.current = true;
    setBusy(true);
    const running = (async () => {
      try {
        await action();
      } catch (error) {
        showToast(error instanceof Error ? error.message : "操作失败，请稍后重试", { tone: "error" });
      } finally {
        busyRef.current = false;
        setBusy(false);
      }
    })();
    runningRef.current = running;
    await running;
  };

  // 提示条上的［撤销］：提示在写成功后、重取还没回来时就弹出，这时点撤销不能被互斥吞掉，
  // 而是等手头那次做完再撤
  const withUndo = (message: string, undo: () => Promise<void>) =>
    showToast(message, {
      onUndo: async () => {
        while (busyRef.current && runningRef.current) await runningRef.current;
        await run(async () => {
          await undo();
          await refreshAfterWrite();
        });
      },
    });

  const completeTask = (task: Task) =>
    run(async () => {
      await apiClient.setTaskStatus(task.id, "done");
      withUndo(`已完成「${task.title}」`, async () => {
        const result = await apiClient.undoTaskComplete([task.id]);
        if (result.failed.length > 0) {
          showToast(result.failed[0].error || "已经过了 10 分钟，没法撤销了", { tone: "error" });
        } else {
          showToast(`已撤销，「${task.title}」回到未完成`);
        }
      });
      await refreshAfterWrite();
    });

  const setStatus = (task: Task, status: Task["status"], message: string) =>
    run(async () => {
      await apiClient.setTaskStatus(task.id, status);
      showToast(message);
      await refreshAfterWrite();
    });

  const linkTask = (task: Task, option: LinkOption | null) =>
    run(async () => {
      const original = originalLink(task, await apiClient.updateTask(task.id, linkBody(option)));
      withUndo(option ? `已挂到「${option.title}」` : "已设为不挂需求", async () => {
        await apiClient.updateTask(task.id, original);
        showToast("已撤销，挂接回到原来的样子");
      });
      await refreshAfterWrite();
    });

  // 「接下」：把需求背景复制出去，同需求池海报；这个函数由点击处理函数同步调用，复制在手势里发起
  const takeRequirement = async (item: PoolItem): Promise<boolean> => {
    try {
      const context = await copyRequirementBackground(() => apiClient.requirementContext(item.id));
      showToast(copiedMessage(context));
      return true;
    } catch (error) {
      showToast(`没复制成功：${copyFailureReason(error)}`);
      return false;
    }
  };

  const actions = {
    onComplete: (task: Task) => void completeTask(task),
    onStart: (task: Task) => void setStatus(task, "in_progress", "已开始处理"),
    onBack: (task: Task) => void setStatus(task, "confirmed", "已回退到已确认"),
    onCancel: (task: Task) => void setStatus(task, "cancelled", "任务已取消"),
    onEdit: (task: Task) => setEditing(task),
    onLink: (task: Task, option: LinkOption | null) => void linkTask(task, option),
    onOpenTask,
    onOpenMeeting,
  };

  if (state === "loading") return <AsyncState state="loading" />;
  if (state === "error" || !data) {
    return (
      <div className="detail-error">
        <span>需求与任务读取失败</span>
        <button onClick={() => void load()} type="button">
          重试
        </button>
      </div>
    );
  }

  const { work, posters } = data;
  const active = work.requirements;
  // 本项目没有进行中需求时，没挂需求的任务没有地方可挂
  const canLink = canWrite && active.length > 0;
  const closedDone = work.closed_requirements.filter((item) => item.status === "done").length;
  const closedShelved = work.closed_requirements.length - closedDone;

  const dragActive = dragging !== null;
  const dragOver = (event: DragEvent, requirementId: string) => {
    if (!dragActive) return;
    event.preventDefault();
    if (event.dataTransfer) event.dataTransfer.dropEffect = "move";
    if (overId !== requirementId) setOverId(requirementId);
  };
  const dragLeave = (event: DragEvent, requirementId: string) => {
    if (event.currentTarget.contains(event.relatedTarget as Node | null)) return;
    if (overId === requirementId) setOverId(null);
  };
  const endDrag = () => {
    setDragging(null);
    setOverId(null);
  };
  const drop = (event: DragEvent, requirement: ProjectWorkRequirement) => {
    if (!dragActive) return;
    event.preventDefault();
    const task = dragging;
    endDrag();
    if (task) void linkTask(task, requirementOption(requirement, projectId));
  };

  const candidates = work.pending_candidates;
  const candidateNames =
    candidates.length > 2
      ? `${candidates.slice(0, 2).map((item) => item.title).join("、")} 等`
      : candidates.map((item) => item.title).join("、");
  const pendingIds = new Set(candidates.map((item) => item.id));

  return (
    <div className="work-tab">
      {stale && (
        <div className="work-stale" role="status">
          <span>刚才的操作已保存，但没能重新读取，下面显示的可能不是最新的</span>
          <button onClick={() => void load(true)} type="button">
            重新读取
          </button>
        </div>
      )}
      {candidates.length > 0 && (
        <div className="work-banner" role="note">
          <span aria-hidden="true" className="work-banner__dot" />
          <span className="work-banner__text">
            这个项目有 {candidates.length} 条待认领的需求候选：
            <b title={candidates.map((item) => item.title).join("、")}>{candidateNames}</b>
          </span>
          {onClaim && (
            <button className="work-banner__go" onClick={onClaim} type="button">
              去认领 <span aria-hidden="true">→</span>
            </button>
          )}
        </div>
      )}

      <section aria-label="进行中的需求" className="work-section">
        <h2 className="work-section__head">
          进行中的需求 <span>{active.length}</span>
        </h2>
        {active.length === 0 ? (
          <p className="work-empty">本项目没有进行中的需求</p>
        ) : (
          <div className="work-pairs">
            {active.map((requirement) => {
              const poster = posters.get(requirement.id);
              const isTarget = dragActive && overId === requirement.id;
              const dim = dragActive && overId !== null && overId !== requirement.id;
              return (
                <div
                  className={`work-pair ${isTarget ? "is-target" : ""} ${dim ? "is-dim" : ""}`}
                  data-requirement-id={requirement.id}
                  key={requirement.id}
                  onDragEnter={(event) => dragOver(event, requirement.id)}
                  onDragLeave={(event) => dragLeave(event, requirement.id)}
                  onDragOver={(event) => dragOver(event, requirement.id)}
                  onDrop={(event) => drop(event, requirement)}
                >
                  {isTarget && <span className="work-pair__hint">放到这里挂上</span>}
                  <div className="work-pair__poster">
                    {poster ? (
                      <PosterCard
                        canWrite={canWrite}
                        item={poster}
                        onOpen={() => onOpenRequirement(requirement.id)}
                        onOpenMeeting={onOpenMeeting}
                        onTake={takeRequirement}
                      />
                    ) : (
                      <button className="work-pair__bare" onClick={() => onOpenRequirement(requirement.id)} type="button">
                        <PriorityBadge priority={requirement.priority} />
                        {requirement.title}
                      </button>
                    )}
                  </div>
                  <TaskPanel
                    {...actions}
                    apiClient={apiClient}
                    busy={busy}
                    canWrite={canWrite}
                    expanded={expanded.has(requirement.id)}
                    onCreate={() => setCreatingFor(requirement)}
                    onPopover={setPopover}
                    onToggleExpanded={() =>
                      setExpanded((current) => {
                        const next = new Set(current);
                        if (!next.delete(requirement.id)) next.add(requirement.id);
                        return next;
                      })
                    }
                    popover={popover}
                    requirementTitle={requirement.title}
                    tasks={requirement.tasks}
                  />
                </div>
              );
            })}
          </div>
        )}
      </section>

      {work.closed_requirements.length > 0 && (
        <section aria-label="已完成和已搁置的需求" className="work-closed">
          <button
            aria-expanded={closedOpen}
            className="work-closed__bar"
            onClick={() => setClosedOpen(!closedOpen)}
            type="button"
          >
            <span aria-hidden="true" className={`work-closed__chev ${closedOpen ? "is-open" : ""}`}>
              ⌃
            </span>
            <span>
              已完成 {closedDone} · 已搁置 {closedShelved}
            </span>
            <span className="work-closed__toggle">{closedOpen ? "收起" : "展开"}</span>
          </button>
          {closedOpen && (
            <div className="work-closed__grid">
              {work.closed_requirements.map((item) => (
                <article
                  aria-label={`${CLOSED_STATUS_TEXT[item.status as "done" | "shelved"] ?? item.status}需求：${item.title}`}
                  className="work-mini"
                  key={item.id}
                  onClick={() => onOpenRequirement(item.id)}
                >
                  <header className="work-mini__head">
                    <span className="work-mini__project">
                      {projectSeat !== null && <span className="work-mini__seat">{projectSeat}</span>}
                      {projectName}
                    </span>
                    <span className="work-mini__tags">
                      <span className={`work-mini__status work-mini__status--${item.status}`}>
                        {CLOSED_STATUS_TEXT[item.status as "done" | "shelved"] ?? item.status}
                      </span>
                      <PriorityBadge priority={item.priority} />
                    </span>
                  </header>
                  <h3 className="work-mini__title">{item.title}</h3>
                  <footer className="work-mini__foot">
                    <span>
                      任务 {item.task_count}
                      {item.task_count > 0 && (item.all_done ? " · 全部完成" : " · 还有未完成")}
                    </span>
                    <button
                      className="work-mini__view"
                      onClick={(event) => {
                        event.stopPropagation();
                        onOpenRequirement(item.id);
                      }}
                      type="button"
                    >
                      查看
                    </button>
                  </footer>
                </article>
              ))}
            </div>
          )}
        </section>
      )}

      <section aria-label="没挂需求的任务" className="work-unlinked">
        <header className="work-unlinked__head">
          <h2>
            没挂需求的任务 <span>{work.unlinked_tasks.length}</span>
          </h2>
          <p>会上答应的跟进事项{canLink ? "，可以拖到上面某条需求里挂上" : ""}</p>
        </header>
        {work.unlinked_tasks.length === 0 ? (
          <p className="work-empty">没有未挂需求的任务</p>
        ) : (
          <>
            <div aria-hidden="true" className="work-cols work-cols--unlinked">
              <span />
              <span />
              <span>任务</span>
              <span>状态</span>
              <span>负责人</span>
              <span>截止</span>
              <span>来源</span>
              <span className="work-cols__ops">操作</span>
            </div>
            <ul className="work-unlinked__list">
              {work.unlinked_tasks.map((task) => (
                <TaskRow
                  {...actions}
                  apiClient={apiClient}
                  busy={busy}
                  candidateVisible={Boolean(task.candidate_id && pendingIds.has(task.candidate_id))}
                  canLink={canLink}
                  canWrite={canWrite}
                  key={task.id}
                  onDragEnd={endDrag}
                  onDragStart={setDragging}
                  onPopover={(kind) => setPopover(kind ? { id: task.id, kind } : null)}
                  popover={popover?.id === task.id ? popover.kind : null}
                  task={task}
                  variant="unlinked"
                />
              ))}
            </ul>
          </>
        )}
      </section>

      {editing && (
        <TaskEditModal
          apiClient={apiClient}
          canWrite={canWrite}
          onClose={() => setEditing(null)}
          onSaved={({ title }) => {
            showToast(`已保存「${title}」`);
            setEditing(null);
            void refreshAfterWrite();
          }}
          projects={projects}
          task={editing as TaskDetail}
        />
      )}
      {creatingFor && (
        <TaskEditModal
          apiClient={apiClient}
          canWrite={canWrite}
          defaultProjectId={projectId}
          fixedHint="固定挂在这条需求上"
          fixedRequirement={{ id: creatingFor.id, title: creatingFor.title, priority: creatingFor.priority }}
          onClose={() => setCreatingFor(null)}
          onSaved={() => {
            showToast("已新建任务");
            // 新任务排在未完成的最后，折在「还有 N 项」里就看不到：展开这块面板
            setExpanded((current) => new Set(current).add(creatingFor.id));
            setCreatingFor(null);
            void refreshAfterWrite();
          }}
          projects={projects}
          task={null}
        />
      )}
    </div>
  );
}
