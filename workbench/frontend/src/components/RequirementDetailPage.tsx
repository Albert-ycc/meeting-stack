import { useCallback, useEffect, useRef, useState } from "react";

import type { ApiClient } from "../api";
import { formatDurationText, formatMonthDay, formatMonthDayClock } from "../format";
import type {
  MaterialFolderStat,
  Project,
  RequirementDetail,
  RequirementFile,
  RequirementFolder,
  Task,
  TaskStatus,
} from "../types";
import { FolderIcon } from "./FolderIcon";
import { PriorityBadge, RequirementStatusBadge } from "./RequirementBadges";
import { RequirementModal } from "./RequirementModal";
import { MaterialFolderPickerModal } from "./MaterialFolderPickerModal";
import { LinkMeetingsModal } from "./LinkMeetingsModal";
import { LinkTasksModal } from "./LinkTasksModal";
import { TaskEditModal } from "./TaskEditModal";
import { useToast } from "./Toast";
import "./RequirementDetailPage.css";
import { copyText } from "../clipboard";

interface RequirementDetailPageProps {
  apiClient: ApiClient;
  requirementId: string;
  canWrite: boolean;
  canPickFolders: boolean;
  projects: Project[];
  onBack: () => void;
  onOpenMeeting: (meetingId: string, seekMs?: number) => void;
  onOpenTask: (taskId: string) => void;
  onOpenProject: (projectId: string) => void;
  onProjectsChanged?: () => void | Promise<void>;
  reloadKey?: number;
}

type LoadState = "loading" | "ready" | "error";

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

function formatBytes(size: number): string {
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / (1024 * 1024)).toFixed(1)} MB`;
}

function folderCountLabel(folder: RequirementFolder): string {
  return folder.file_count_capped ? "2000+ 个文件" : `${folder.file_count} 个文件`;
}

// 三张卡各自的空态图标 + 文件夹/文件行前缀图标，同一套 15×15 线性风格（跟侧栏图标一致）。
function MicIcon() {
  return (
    <svg fill="none" stroke="currentColor" strokeLinecap="round" strokeLinejoin="round" strokeWidth="1.5" viewBox="0 0 15 15">
      <rect height="7.6" rx="2.3" width="4.6" x="5.2" y="1.5" />
      <path d="M3 7.3a4.5 4.5 0 0 0 9 0" />
      <path d="M7.5 11.8v1.7M5.3 13.5h4.4" />
    </svg>
  );
}

function TaskEmptyIcon() {
  return (
    <svg fill="none" stroke="currentColor" strokeLinecap="round" strokeWidth="1.5" viewBox="0 0 15 15">
      <rect height="11" rx="2" width="11" x="2" y="2" />
      <path d="M5 7.5h5" />
    </svg>
  );
}

function FileIcon({ className }: { className?: string }) {
  return (
    <svg className={className} fill="none" stroke="currentColor" strokeLinecap="round" strokeLinejoin="round" strokeWidth="1.3" viewBox="0 0 15 15">
      <path d="M4 1.5h4.5L11 4v8.5a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V2.5a1 1 0 0 1 1-1Z" />
      <path d="M8.3 1.5V4h2.5" />
    </svg>
  );
}

function CopyIcon() {
  return (
    <svg aria-hidden="true" fill="none" stroke="currentColor" strokeLinecap="round" strokeLinejoin="round" strokeWidth="1.3" viewBox="0 0 15 15">
      <rect height="8.2" rx="1.3" width="8.2" x="5.8" y="5.8" />
      <path d="M9.2 5.8V4a1.3 1.3 0 0 0-1.3-1.3H4A1.3 1.3 0 0 0 2.7 4v4.5A1.3 1.3 0 0 0 4 9.8h1.8" />
    </svg>
  );
}

/** 需求详情（A-04-1）：关联会议 / 材料文件夹 / 任务三张卡。 */
export function RequirementDetailPage({
  apiClient,
  requirementId,
  canWrite,
  canPickFolders,
  projects,
  onBack,
  onOpenMeeting,
  onOpenTask,
  onOpenProject,
  onProjectsChanged,
  reloadKey = 0,
}: RequirementDetailPageProps) {
  const { toastNode, showToast } = useToast();
  const [detail, setDetail] = useState<RequirementDetail | null>(null);
  const [state, setState] = useState<LoadState>("loading");
  const [editing, setEditing] = useState(false);
  const [linkingMeetings, setLinkingMeetings] = useState(false);
  const [pickingFolders, setPickingFolders] = useState(false);
  const [linkingTasks, setLinkingTasks] = useState(false);
  const [creatingTask, setCreatingTask] = useState(false);
  const [expandedIds, setExpandedIds] = useState<Set<number>>(new Set());
  const [expandedFiles, setExpandedFiles] = useState<Map<number, { items: RequirementFile[]; capped: boolean }>>(new Map());
  const [expandLoading, setExpandLoading] = useState<Set<number>>(new Set());

  // 已经有这条需求的数据时静默刷新：留着页面只换数据，不整页闪成「正在读取」。
  const loadedIdRef = useRef<string | null>(null);
  const load = useCallback(async () => {
    const silent = loadedIdRef.current === requirementId;
    if (!silent) setState("loading");
    try {
      const payload = await apiClient.requirement(requirementId);
      loadedIdRef.current = requirementId;
      setDetail(payload);
      setState("ready");
    } catch (error) {
      if (silent) showToast(error instanceof Error ? `刷新失败：${error.message}` : "刷新失败，请稍后重试");
      else setState("error");
    }
    // showToast 每次渲染都是新函数，放进依赖会让 load 反复变化、页面循环刷新。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [apiClient, requirementId]);

  useEffect(() => {
    void load();
  }, [load, reloadKey]);

  // 页面上的写操作统一走这里：进行中禁用按钮防连点，失败给出原因，成功后静默刷新。
  const [mutating, setMutating] = useState(false);
  const mutate = async (action: () => Promise<unknown>, success?: string) => {
    if (mutating) return;
    setMutating(true);
    try {
      await action();
      if (success) showToast(success);
      await load();
    } catch (error) {
      showToast(error instanceof Error ? error.message : "操作失败，请稍后重试");
    } finally {
      setMutating(false);
    }
  };

  const copyPath = async (path: string, message: string) => {
    try {
      await copyText(path);
      showToast(message);
    } catch {
      showToast("复制失败，请稍后重试");
    }
  };

  const materialPaths = (loaded: RequirementDetail): string[] => [
    ...loaded.meetings.map((meeting) => meeting.canonical_dir).filter((path): path is string => Boolean(path)),
    ...loaded.folders.map((folder) => folder.path),
  ];

  const copyAllMaterials = () => {
    if (!detail) return;
    const paths = materialPaths(detail);
    void copyPath(paths.join("\n"), `已复制 ${paths.length} 条路径`);
  };

  const removeMeeting = (meetingId: string) =>
    void mutate(() => apiClient.removeRequirementMeeting(requirementId, meetingId), "已移除关联会议");

  const removeFolder = (folderId: number) =>
    void mutate(() => apiClient.removeRequirementFolder(requirementId, folderId), "已移除材料文件夹");

  const toggleExpandFolder = (folder: RequirementFolder) => {
    if (expandedIds.has(folder.id)) {
      setExpandedIds((current) => {
        const next = new Set(current);
        next.delete(folder.id);
        return next;
      });
      return;
    }
    if (expandedFiles.has(folder.id)) {
      setExpandedIds((current) => new Set(current).add(folder.id));
      return;
    }
    setExpandLoading((current) => new Set(current).add(folder.id));
    void apiClient
      .requirementFolderFiles(requirementId, folder.id)
      .then((payload) => {
        setExpandedFiles((current) => new Map(current).set(folder.id, { items: payload.items, capped: payload.capped }));
        setExpandedIds((current) => new Set(current).add(folder.id));
      })
      .catch((error: unknown) => {
        showToast(error instanceof Error ? `文件清单读取失败：${error.message}` : "文件清单读取失败");
      })
      .finally(() => {
        setExpandLoading((current) => {
          const next = new Set(current);
          next.delete(folder.id);
          return next;
        });
      });
  };

  const savePickedFolders = async (folders: MaterialFolderStat[]) => {
    setPickingFolders(false);
    await mutate(
      () => apiClient.updateRequirement(requirementId, { folder_paths: folders.map((folder) => folder.path) }),
      "材料文件夹已更新",
    );
  };

  if (state === "loading" || !detail) {
    return (
      <section className="page-content requirement-detail">
        {state === "error" ? (
          <div className="requirement-detail__state requirement-detail__state--error" role="alert">
            需求详情读取失败
            <div className="requirement-detail__state-actions">
              <button onClick={() => void load()} type="button">重试</button>
              <button onClick={onBack} type="button">返回需求池</button>
            </div>
          </div>
        ) : (
          <div className="requirement-detail__state">正在读取需求…</div>
        )}
      </section>
    );
  }

  const currentProject = projects.find((project) => project.id === detail.project_id) ?? null;

  return (
    <section className="page-content requirement-detail">
      {toastNode}
      <header className="requirement-detail__head">
        <nav aria-label="面包屑" className="requirement-detail__breadcrumb">
          <button onClick={onBack} type="button">需求池</button>
          <span>/</span>
          <span>{detail.title}</span>
        </nav>
        <div className="requirement-detail__title-row">
          <h1>{detail.title}</h1>
          <div className="requirement-detail__actions">
            <button
              className="requirement-detail__copy"
              disabled={materialPaths(detail).length === 0}
              onClick={copyAllMaterials}
              type="button"
            >
              <CopyIcon />
              复制材料清单
            </button>
            {canWrite && (
              <button className="requirement-detail__edit" onClick={() => setEditing(true)} type="button">编辑需求</button>
            )}
          </div>
        </div>
        <div className="requirement-detail__meta">
          <button
            className="requirement-detail__project"
            onClick={() => onOpenProject(detail.project_id)}
            type="button"
          >
            <i style={{ background: detail.project_color }} />
            {detail.project_name}
          </button>
          <PriorityBadge priority={detail.priority} />
          <RequirementStatusBadge status={detail.status} />
          <span className="requirement-detail__created">创建于 {formatMonthDay(detail.created_at)}</span>
        </div>
      </header>

      <section className="requirement-detail__card">
        <header className="requirement-detail__card-head">
          <strong>关联会议</strong>
          <span className="requirement-detail__count">{detail.meetings.length}</span>
          {canWrite && (
            <button onClick={() => setLinkingMeetings(true)} type="button">＋ 关联会议</button>
          )}
        </header>
        {detail.meetings.length === 0 ? (
          <div className="requirement-detail__empty">
            <span aria-hidden="true"><MicIcon /></span>
            <p>还没有关联会议</p>
          </div>
        ) : (
          <div className="requirement-detail__meeting-table">
            <div className="requirement-detail__meeting-row requirement-detail__meeting-row--head">
              <span>日期</span>
              <span>会议</span>
              <span>时长</span>
              <span>操作</span>
            </div>
            {detail.meetings.map((meeting) => (
              <div className="requirement-detail__meeting-row" key={meeting.id}>
                <span>{formatMonthDayClock(meeting.recording_date)}</span>
                <span className="requirement-detail__meeting-title">{meeting.title}</span>
                <span>{formatDurationText(meeting.duration_ms)}</span>
                <span className="requirement-detail__row-actions">
                  <button onClick={() => onOpenMeeting(meeting.id)} type="button">打开</button>
                  {meeting.canonical_dir && (
                    <button onClick={() => void copyPath(meeting.canonical_dir!, "已复制路径")} type="button">复制路径</button>
                  )}
                  {canWrite && (
                    <button disabled={mutating} onClick={() => removeMeeting(meeting.id)} type="button">移除</button>
                  )}
                </span>
              </div>
            ))}
          </div>
        )}
      </section>

      <section className="requirement-detail__card">
        <header className="requirement-detail__card-head">
          <strong>材料文件夹</strong>
          <span className="requirement-detail__count">{detail.folders.length}</span>
          {canWrite && canPickFolders && (
            <button onClick={() => setPickingFolders(true)} type="button">＋ 选择文件夹</button>
          )}
        </header>
        {detail.folders.length === 0 ? (
          <div className="requirement-detail__empty">
            <span aria-hidden="true"><FolderIcon /></span>
            <p>还没有材料文件夹</p>
          </div>
        ) : (
          <div className="requirement-detail__folder-table">
            <div className="requirement-detail__folder-row requirement-detail__folder-row--head">
              <span>名称</span>
              <span>大小</span>
              <span>修改日期</span>
              <span>操作</span>
            </div>
            {detail.folders.map((folder) => {
              const expanded = expandedIds.has(folder.id);
              const loadingFiles = expandLoading.has(folder.id);
              const cached = expandedFiles.get(folder.id);
              const files: RequirementFile[] = expanded && cached ? cached.items : folder.preview_files;
              const hasMore = folder.file_count > folder.preview_files.length;
              return (
                <div className="requirement-detail__folder-group" key={folder.id}>
                  <div className="requirement-detail__folder-row requirement-detail__folder-row--group">
                    <span className="requirement-detail__folder-name">
                      <FolderIcon className="requirement-detail__row-icon" />
                      {folder.name}
                      {folder.exists ? null : <em className="requirement-detail__folder-missing">找不到该文件夹</em>}
                      <small>{folder.path}</small>
                    </span>
                    <span>{folderCountLabel(folder)}</span>
                    <span>{formatMonthDay(folder.modified_at)}</span>
                    <span className="requirement-detail__row-actions">
                      <button onClick={() => void copyPath(folder.path, "已复制路径")} type="button">复制路径</button>
                      {canWrite && (
                        <button disabled={mutating} onClick={() => removeFolder(folder.id)} type="button">移除</button>
                      )}
                    </span>
                  </div>
                  {files.map((file) => (
                    <div className="requirement-detail__folder-row requirement-detail__folder-row--file" key={file.relative_path}>
                      <span className="requirement-detail__folder-name">
                        <FileIcon className="requirement-detail__row-icon" />
                        {file.relative_path}
                      </span>
                      <span>{formatBytes(file.size_bytes)}</span>
                      <span>{formatMonthDay(file.modified_at)}</span>
                      <span />
                    </div>
                  ))}
                  {hasMore && (
                    <button
                      className="requirement-detail__toggle-files"
                      onClick={() => toggleExpandFolder(folder)}
                      type="button"
                    >
                      {loadingFiles
                        ? "加载中…"
                        : expanded
                          ? "收起"
                          : `查看全部 ${folder.file_count_capped ? "2000+" : folder.file_count} 个文件 ›`}
                    </button>
                  )}
                  {expanded && cached?.capped && (
                    <p className="requirement-detail__capped-note">只列出前 2000 个</p>
                  )}
                </div>
              );
            })}
          </div>
        )}
      </section>

      <section className="requirement-detail__card">
        <header className="requirement-detail__card-head">
          <strong>任务</strong>
          <span className="requirement-detail__count requirement-detail__count--muted">未完成 {detail.open_task_count}</span>
          {canWrite && (
            <span className="requirement-detail__card-head-actions">
              <button className="requirement-detail__link-tasks" onClick={() => setLinkingTasks(true)} type="button">关联已有任务</button>
              <button className="requirement-detail__new-task" onClick={() => setCreatingTask(true)} type="button">＋ 新建任务</button>
            </span>
          )}
        </header>
        {detail.tasks.length === 0 ? (
          <div className="requirement-detail__empty">
            <span aria-hidden="true"><TaskEmptyIcon /></span>
            <p>还没有任务</p>
          </div>
        ) : (
          <div className="requirement-detail__task-table">
            <div className="requirement-detail__task-row requirement-detail__task-row--head">
              <span>任务</span>
              <span>状态</span>
              <span>执行方</span>
              <span>来源会议</span>
              <span>停滞</span>
              <span>操作</span>
            </div>
            {detail.tasks.map((task: Task) => (
              <div className="requirement-detail__task-row" key={task.id}>
                <span className="requirement-detail__task-title">{task.title}</span>
                <span>
                  <span className={`requirement-detail__status-chip requirement-detail__status-chip--${STATUS_TONE[task.status]}`}>
                    {STATUS_LABEL[task.status]}
                  </span>
                </span>
                <span>{task.assignee === "ai" ? "AI" : "我"}</span>
                <span className="requirement-detail__task-meeting">{task.meeting_title || "—"}</span>
                <span>{task.stalled && task.stall_days > 0 ? `停滞 ${Math.floor(task.stall_days)} 天` : "—"}</span>
                <span className="requirement-detail__row-actions">
                  <button onClick={() => onOpenTask(task.id)} type="button">查看</button>
                </span>
              </div>
            ))}
          </div>
        )}
      </section>

      {editing && (
        <RequirementModal
          apiClient={apiClient}
          canPickFolders={canPickFolders}
          mode="edit"
          onClose={() => setEditing(false)}
          onOpenProject={onOpenProject}
          onSaved={async () => {
            setEditing(false);
            showToast("已保存");
            await load();
            await onProjectsChanged?.();
          }}
          projects={projects}
          requirement={detail}
        />
      )}

      {linkingMeetings && (
        <LinkMeetingsModal
          apiClient={apiClient}
          onCancel={() => setLinkingMeetings(false)}
          onSaved={async (updated) => {
            setDetail(updated);
            setLinkingMeetings(false);
            await load();
          }}
          projectId={detail.project_id}
          requirementId={requirementId}
          selectedIds={detail.meetings.map((meeting) => meeting.id)}
        />
      )}

      {linkingTasks && (
        <LinkTasksModal
          apiClient={apiClient}
          onCancel={() => setLinkingTasks(false)}
          onSaved={async (updated) => {
            setDetail(updated);
            setLinkingTasks(false);
            await load();
          }}
          projectId={detail.project_id}
          requirementId={requirementId}
        />
      )}

      {pickingFolders && currentProject && (
        <MaterialFolderPickerModal
          apiClient={apiClient}
          onCancel={() => setPickingFolders(false)}
          onConfirm={(folders) => void savePickedFolders(folders)}
          onOpenProject={onOpenProject}
          projectId={currentProject.id}
          projectName={currentProject.name}
          selectedFolders={detail.folders}
        />
      )}

      {creatingTask && (
        <TaskEditModal
          apiClient={apiClient}
          canWrite={canWrite}
          defaultProjectId={detail.project_id}
          defaultRequirementId={detail.id}
          onClose={() => setCreatingTask(false)}
          onSaved={() => { setCreatingTask(false); void load(); }}
          projects={projects}
          task={null}
        />
      )}
    </section>
  );
}
