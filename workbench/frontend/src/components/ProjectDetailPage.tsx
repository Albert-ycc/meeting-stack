import { useCallback, useEffect, useMemo, useState } from "react";

import type { ApiClient } from "../api";
import { formatDurationText, formatMonthDay } from "../format";
import type {
  MaterialRoot,
  Project,
  ProjectBoard,
  ProjectMeetingRow,
  ProjectSubfoldersPayload,
  RequirementRef,
  RequirementStatus,
  RequirementSummary,
  RequirementsPayload,
} from "../types";
import { AsyncState } from "./AsyncState";
import { FolderIcon } from "./FolderIcon";
import { MaterialRootPickerModal } from "./MaterialRootPickerModal";
import { ProjectCardsRow } from "./ProjectCardsRow";
import { Pagination } from "./Pagination";
import { ProjectFormModal } from "./ProjectFormModal";
import { ProjectRecognitionCard } from "./ProjectRecognitionCard";
import { PriorityBadge, RequirementStatusBadge } from "./RequirementBadges";
// FE-1 负责的需求弹窗；写这个文件时它可能还不存在，tsc 报「模块不存在」属于预期（简报第 5 节已钉死 props）。
import { RequirementModal } from "./RequirementModal";
import { useToast } from "./Toast";
import "./ProjectDetailPage.css";
import { useConfirm } from "./ConfirmDialog";
import { copyText } from "../clipboard";
import { NoticeBanner, useNotice } from "./Notice";
import { ProjectGlossary } from "./ProjectGlossary";
import { usePersistentState } from "../viewState";

interface ProjectDetailPageProps {
  apiClient: ApiClient;
  projectId: string;
  canWrite: boolean;
  onBack: () => void;
  onOpenGlossary: (projectId: string) => void;
  onOpenMeeting: (meetingId: string) => void;
  onOpenTask: (taskId: string) => void;
  reloadKey?: number;
  onProjectUpdated?: () => void;
  /** 新建需求弹窗要拿全量项目列表填「所属项目」下拉 */
  projects: Project[];
  onOpenRequirement: (id: string) => void;
  /** 挂根目录 / 选材料文件夹只在桌面端出现 */
  canPickFolders: boolean;
  onProjectsChanged?: () => void | Promise<void>;
  /** 合并后跳到目标项目 */
  onOpenProject?: (projectId: string) => void;
}

type LoadState = "loading" | "ready" | "error";

const REQUIREMENTS_PAGE_SIZE = 10;
const MEETINGS_PAGE_SIZE = 8;

const REQUIREMENT_TABS: Array<{ key: RequirementStatus | "all"; label: string }> = [
  { key: "active", label: "进行中" },
  { key: "done", label: "已完成" },
  { key: "shelved", label: "已搁置" },
  { key: "all", label: "全部" },
];

function RequirementRefChips({ items }: { items: RequirementRef[] }) {
  if (items.length === 0) return <span className="detail-table__muted">—</span>;
  const shown = items.slice(0, 2);
  const rest = items.length - shown.length;
  return (
    <span className="detail-chip-row">
      {shown.map((item) => (
        <span className="detail-chip" key={item.id}>
          {item.title}
        </span>
      ))}
      {rest > 0 && <span className="detail-chip detail-chip--more">等 {rest} 个</span>}
    </span>
  );
}

export function ProjectDetailPage({
  apiClient,
  projectId,
  canWrite,
  onBack,
  onOpenGlossary,
  onOpenMeeting,
  onOpenTask: _onOpenTask,
  reloadKey = 0,
  onProjectUpdated,
  projects,
  onOpenRequirement,
  canPickFolders,
  onProjectsChanged,
  onOpenProject,
}: ProjectDetailPageProps) {
  const [board, setBoard] = useState<ProjectBoard | null>(null);
  const [boardState, setBoardState] = useState<LoadState>("loading");
  const [subfolders, setSubfolders] = useState<ProjectSubfoldersPayload | null>(null);

  const [meetingRows, setMeetingRows] = useState<ProjectMeetingRow[] | null>(null);
  const [meetingsState, setMeetingsState] = useState<LoadState>("loading");
  // 项目详情里两张子表的页签与页码按项目分别记住，离开再回来还在原处。
  const [meetingsPage, setMeetingsPage] = usePersistentState(`project.${projectId}.meetingsPage`, 0);

  const [requirementsTab, setRequirementsTab] = usePersistentState<RequirementStatus | "all">(`project.${projectId}.requirementsTab`, "active");
  const [requirementsPage, setRequirementsPage] = usePersistentState(`project.${projectId}.requirementsPage`, 0);
  const [requirementsPayload, setRequirementsPayload] = useState<RequirementsPayload | null>(null);
  const [requirementsState, setRequirementsState] = useState<LoadState>("loading");

  const [editingProject, setEditingProject] = useState<false | "edit" | "merge" | "delete">(false);
  const [creatingRequirement, setCreatingRequirement] = useState(false);
  const [addingRoot, setAddingRoot] = useState(false);
  const [reselectingRoot, setReselectingRoot] = useState<MaterialRoot | null>(null);
  const [confirm, confirmDialog] = useConfirm();
  const [rootBusy, setRootBusy] = useState(false);
  // 挂/重选根目录失败的原因：显示在还开着的取径器里（D27），不是页面级 notice
  const [rootError, setRootError] = useState("");
  const { notice, setNotice, dismissNotice } = useNotice();

  const { toastNode, showToast } = useToast();

  const loadBoard = useCallback(async () => {
    try {
      const data = await apiClient.projectBoard(projectId);
      setBoard(data);
      setBoardState("ready");
    } catch {
      setBoardState("error");
    }
  }, [apiClient, projectId]);

  const loadSubfolders = useCallback(async () => {
    try {
      setSubfolders(await apiClient.projectMaterialSubfolders(projectId));
    } catch {
      // 子文件夹数只是辅助信息，读取失败不影响主卡片
      setSubfolders(null);
    }
  }, [apiClient, projectId]);

  const loadMeetings = useCallback(async () => {
    setMeetingsState("loading");
    try {
      const rows = await apiClient.projectMeetings(projectId);
      setMeetingRows(rows);
      setMeetingsState("ready");
    } catch {
      setMeetingsState("error");
    }
  }, [apiClient, projectId]);

  const loadRequirements = useCallback(async () => {
    setRequirementsState("loading");
    try {
      const payload = await apiClient.requirements({
        project_id: projectId,
        ...(requirementsTab === "all" ? {} : { status: requirementsTab }),
        limit: REQUIREMENTS_PAGE_SIZE,
        offset: requirementsPage * REQUIREMENTS_PAGE_SIZE,
      });
      setRequirementsPayload(payload);
      setRequirementsState("ready");
    } catch {
      setRequirementsState("error");
    }
  }, [apiClient, projectId, requirementsTab, requirementsPage]);

  useEffect(() => {
    void loadBoard();
    void loadSubfolders();
  }, [loadBoard, loadSubfolders, reloadKey]);

  useEffect(() => {
    void loadMeetings();
  }, [loadMeetings, reloadKey]);

  useEffect(() => {
    void loadRequirements();
  }, [loadRequirements, reloadKey]);

  const subfolderCounts = useMemo(() => {
    const map = new Map<number, number>();
    subfolders?.roots.forEach((entry) => map.set(entry.root_id, entry.folders.length));
    return map;
  }, [subfolders]);

  const meetingsPageCount = Math.ceil((meetingRows?.length ?? 0) / MEETINGS_PAGE_SIZE);
  const visibleMeetings = (meetingRows ?? []).slice(
    meetingsPage * MEETINGS_PAGE_SIZE,
    meetingsPage * MEETINGS_PAGE_SIZE + MEETINGS_PAGE_SIZE,
  );
  const requirementsPageCount = requirementsPayload
    ? Math.max(1, Math.ceil(requirementsPayload.total / REQUIREMENTS_PAGE_SIZE))
    : 1;

  const switchRequirementsTab = (key: RequirementStatus | "all") => {
    if (key === requirementsTab) return;
    setRequirementsTab(key);
    setRequirementsPage(0);
  };

  const copyWithToast = async (text: string, done: string) => {
    try {
      await copyText(text);
      showToast(done);
    } catch {
      setNotice("复制失败，请手动复制", "error");
    }
  };
  const copyPath = (path: string) => copyWithToast(path, "已复制路径");
  // 挂上文件夹时当场补写的会议卡片张数
  const cardsWrittenNote = (root: MaterialRoot | undefined) =>
    root?.cards_written ? `，已补写 ${root.cards_written} 张会议卡片` : "";

  const refreshAfterRootChange = async () => {
    await Promise.all([loadBoard(), loadSubfolders()]);
    await onProjectsChanged?.();
  };

  const addRoot = async (path: string) => {
    setRootBusy(true);
    setRootError("");
    try {
      const added = await apiClient.addProjectMaterialRoot(projectId, path);
      setAddingRoot(false);
      setNotice(`材料根目录已添加${cardsWrittenNote(added)}`);
      await refreshAfterRootChange();
    } catch (error) {
      // D27：取径器还开着，错误要就地显示在弹窗里，不能吞掉／丢到被弹窗盖住的页面级提示
      setRootError(error instanceof Error ? error.message : "添加失败，请稍后重试");
    } finally {
      setRootBusy(false);
    }
  };

  const reselectRoot = async (path: string) => {
    if (!reselectingRoot) return;
    setRootBusy(true);
    setRootError("");
    try {
      // 原子替换：只改路径，根目录 id 不变；失败时旧根目录原样保留，不会「删了没加上」。
      const replaced = await apiClient.replaceProjectMaterialRoot(projectId, reselectingRoot.id, path);
      setReselectingRoot(null);
      setNotice(`材料根目录已更新${cardsWrittenNote(replaced)}`);
      await refreshAfterRootChange();
    } catch (error) {
      setRootError(error instanceof Error ? error.message : "更新失败，请稍后重试");
    } finally {
      setRootBusy(false);
    }
  };

  // 确认弹窗里执行移除：失败原因留在弹窗里显示，不再写到被弹窗盖住的页面提示上。
  const removeRoot = async (root: MaterialRoot) => {
    setNotice("");
    const removed = await confirm({
      title: "移除材料根目录",
      message: (
        <span className="confirm-modal__path">
          <FolderIcon className="confirm-modal__path-icon" />
          {root.path}
        </span>
      ),
      confirmLabel: "移除",
      tone: "danger",
      action: () => apiClient.removeProjectMaterialRoot(projectId, root.id),
    });
    if (!removed) return;
    setNotice("材料根目录已移除");
    await refreshAfterRootChange();
  };

  const openAddRoot = () => {
    setRootError("");
    setAddingRoot(true);
  };

  const openReselectRoot = (root: MaterialRoot) => {
    setRootError("");
    setReselectingRoot(root);
  };

  const roots = board?.material_roots ?? [];
  const requirementCounts = board?.requirement_counts;
  // 挂/移/重选根目录既要能写这个项目，也要在桌面端；复制路径不受限，谁都能读
  const canManageFolders = canWrite && canPickFolders;
  const isEmptyProject = (board?.meeting_count ?? 0) === 0 && (requirementCounts?.all ?? 0) === 0;

  return (
    <section className="detail-page page-content">
      {toastNode}
      <header className="detail-head">
        <nav aria-label="面包屑" className="detail-breadcrumb">
          <button onClick={onBack} type="button">
            项目管理
          </button>
          <span aria-hidden="true">/</span>
          <span>{board?.name ?? ""}</span>
        </nav>
        <div className="detail-head__row">
          {board && (
            <div className="detail-head__title">
              <i aria-hidden="true" style={{ background: board.color }} />
              <h1>{board.name}</h1>
            </div>
          )}
          {canWrite && board && (
            <span className="detail-head__actions">
              <button className="detail-head__edit" onClick={() => setEditingProject("edit")} type="button">
                编辑项目
              </button>
              <button className="detail-head__create-requirement" onClick={() => setCreatingRequirement(true)} type="button">
                ＋ 新建需求
              </button>
            </span>
          )}
        </div>
        {board && (
          <p className="detail-head__subtitle">
            会议 {board.meeting_count ?? meetingRows?.length ?? 0} 场 · 需求 {requirementCounts?.all ?? 0} 个 ·
            未完成任务 {board.open_task_count ?? 0} 条
          </p>
        )}
      </header>

      <NoticeBanner notice={notice} onDismiss={dismissNotice} />

      {boardState === "loading" && <AsyncState state="loading" />}
      {boardState === "error" && (
        <div className="detail-error">
          <span>项目详情读取失败</span>
          <button onClick={() => void loadBoard()} type="button">
            重试
          </button>
        </div>
      )}

      {board && boardState === "ready" && (
        <>
          {board.origin === "ai" && roots.length === 0 && (
            <div className="detail-hint" role="note">
              <span>这个项目是 AI 自动建的，还没挂文件夹。是重复的就合并到别的项目，用不上可以删掉。</span>
              {canWrite && (
                <span className="detail-hint__actions">
                  {canManageFolders && (
                    <button className="text-button" onClick={openAddRoot} type="button">
                      挂上文件夹
                    </button>
                  )}
                  <button className="text-button" onClick={() => setEditingProject("merge")} type="button">
                    合并到…
                  </button>
                  {isEmptyProject && (
                    <button className="text-button" onClick={() => setEditingProject("delete")} type="button">
                      删除
                    </button>
                  )}
                </span>
              )}
            </div>
          )}

          <section className="detail-card">
            <header className="detail-card__head">
              <h2>材料根目录</h2>
              {canManageFolders && (
                <button className="detail-card__add" onClick={openAddRoot} type="button">
                  ＋ 添加目录
                </button>
              )}
            </header>
            {roots.length === 0 ? (
              <div className="detail-card__empty">
                <p>还没有材料根目录</p>
                {canManageFolders && (
                  <button onClick={openAddRoot} type="button">
                    ＋ 添加目录
                  </button>
                )}
              </div>
            ) : (
              <ul className="material-root-list">
                {roots.map((root) => {
                  const state = root.state ?? (root.exists ? "online" : "missing");
                  return (
                    <li className="material-root-row" key={root.id}>
                      <FolderIcon className="material-root-row__icon" />
                      <span className="material-root-row__path">{root.path}</span>
                      {state === "online" ? (
                        <span className="material-root-row__count">
                          {subfolderCounts.get(root.id) ?? 0} 个子文件夹
                        </span>
                      ) : state === "volume_offline" ? (
                        <span className="material-root-row__offline">资料盘未连接，插上后自动恢复</span>
                      ) : (
                        <span className="material-root-row__missing">找不到该目录</span>
                      )}
                      {(root.shared_with?.length ?? 0) > 0 && (
                        <span className="material-root-row__shared" role="note">
                          也挂在{root.shared_with!.map((entry) => `「${entry.project_name}」`).join("")}下，
                          {root.cards_owner_id === projectId
                            ? "会议卡片写在这个项目里"
                            : `会议卡片只写给先挂上的「${
                                root.shared_with!.find((entry) => entry.project_id === root.cards_owner_id)?.project_name ?? ""
                              }」，不需要可以在这里移除`}
                        </span>
                      )}
                      <span className="material-root-row__ops">
                        {state === "online" ? (
                          <button onClick={() => void copyPath(root.path)} type="button">
                            复制路径
                          </button>
                        ) : (
                          state === "missing" &&
                          canManageFolders && (
                            <button onClick={() => openReselectRoot(root)} type="button">
                              重新选择
                            </button>
                          )
                        )}
                        {canManageFolders && (
                          <button
                            className="material-root-row__remove"
                            onClick={() => void removeRoot(root)}
                            type="button"
                          >
                            移除
                          </button>
                        )}
                      </span>
                    </li>
                  );
                })}
              </ul>
            )}
            {board.cards && (
              <ProjectCardsRow
                apiClient={apiClient}
                canPickFolders={Boolean(canPickFolders)}
                canWrite={canWrite}
                cards={board.cards}
                onChanged={loadBoard}
                onCopy={copyWithToast}
                projectId={projectId}
              />
            )}
          </section>

          {board.profile && (
            <ProjectRecognitionCard
              apiClient={apiClient}
              canWrite={canWrite}
              onChanged={async () => {
                await loadBoard();
                await onProjectsChanged?.();
              }}
              profile={board.profile}
              projectId={projectId}
              projectName={board.name}
            />
          )}

          <section className="detail-card">
            <header className="detail-card__head">
              <h2>需求</h2>
            </header>
            <div aria-label="需求状态" className="detail-subtabs" role="tablist">
              {REQUIREMENT_TABS.map((tab) => (
                <button
                  aria-selected={requirementsTab === tab.key}
                  key={tab.key}
                  onClick={() => switchRequirementsTab(tab.key)}
                  role="tab"
                  type="button"
                >
                  {tab.label}
                  <span>
                    {tab.key === "all" ? requirementCounts?.all ?? 0 : requirementCounts?.[tab.key] ?? 0}
                  </span>
                </button>
              ))}
            </div>
            {requirementsState === "loading" && <AsyncState state="loading" />}
            {requirementsState === "error" && (
              <div className="detail-error">
                <span>需求读取失败</span>
                <button onClick={() => void loadRequirements()} type="button">
                  重试
                </button>
              </div>
            )}
            {requirementsState === "ready" && requirementsPayload && requirementsPayload.items.length === 0 && (
              <AsyncState message="还没有需求" state="empty" />
            )}
            {requirementsState === "ready" && requirementsPayload && requirementsPayload.items.length > 0 && (
              <>
                <table className="detail-table">
                  <thead>
                    <tr>
                      <th>需求名称</th>
                      <th>优先级</th>
                      <th>状态</th>
                      <th>未完成任务</th>
                      <th>关联会议</th>
                      <th>最近会议</th>
                      <th>操作</th>
                    </tr>
                  </thead>
                  <tbody>
                    {requirementsPayload.items.map((item: RequirementSummary) => (
                      <tr key={item.id}>
                        <td>
                          <button className="detail-table__link" onClick={() => onOpenRequirement(item.id)} type="button">
                            {item.title}
                          </button>
                        </td>
                        <td>
                          <PriorityBadge priority={item.priority} />
                        </td>
                        <td>
                          <RequirementStatusBadge status={item.status} />
                        </td>
                        <td>{item.open_task_count}</td>
                        <td>{item.meeting_count}</td>
                        <td>{formatMonthDay(item.latest_meeting_date)}</td>
                        <td>
                          <button className="text-button" onClick={() => onOpenRequirement(item.id)} type="button">
                            查看
                          </button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
                <div className="detail-table__pagination">
                  <p className="detail-table__count">共 {requirementsPayload.total} 条</p>
                  <Pagination onChange={setRequirementsPage} page={requirementsPage} pageCount={requirementsPageCount} />
                </div>
              </>
            )}
          </section>

          <section className="detail-card">
            <header className="detail-card__head">
              <h2>会议</h2>
            </header>
            {meetingsState === "loading" && <AsyncState state="loading" />}
            {meetingsState === "error" && (
              <div className="detail-error">
                <span>会议读取失败</span>
                <button onClick={() => void loadMeetings()} type="button">
                  重试
                </button>
              </div>
            )}
            {meetingsState === "ready" && (meetingRows?.length ?? 0) === 0 && (
              <AsyncState message="还没有会议" state="empty" />
            )}
            {meetingsState === "ready" && (meetingRows?.length ?? 0) > 0 && (
              <>
                <table className="detail-table">
                  <thead>
                    <tr>
                      <th>日期</th>
                      <th>会议</th>
                      <th>时长</th>
                      <th>关联需求</th>
                      <th>操作</th>
                    </tr>
                  </thead>
                  <tbody>
                    {visibleMeetings.map((row) => (
                      <tr key={row.id}>
                        <td>{formatMonthDay(row.recording_date)}</td>
                        <td>{row.title}</td>
                        <td>{row.duration_ms != null ? formatDurationText(row.duration_ms) : "--"}</td>
                        <td>
                          <RequirementRefChips items={row.requirements} />
                        </td>
                        <td>
                          <button className="text-button" onClick={() => onOpenMeeting(row.id)} type="button">
                            打开
                          </button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
                <div className="detail-table__pagination">
                  <p className="detail-table__count">共 {meetingRows?.length ?? 0} 场</p>
                  <Pagination onChange={setMeetingsPage} page={meetingsPage} pageCount={meetingsPageCount} />
                </div>
              </>
            )}
          </section>

          <ProjectGlossary
            apiClient={apiClient}
            canWrite={canWrite}
            onChanged={async (message) => {
              setNotice(message);
              await loadBoard();
            }}
            onOpenGlossary={onOpenGlossary}
            projectId={projectId}
            projectName={board.name}
            publicCount={board.public_glossary_count ?? 0}
            terms={board.glossary_terms ?? []}
            total={board.glossary_count ?? 0}
          />
        </>
      )}

      {editingProject && board && (
        <ProjectFormModal
          apiClient={apiClient}
          canPickFolders={canPickFolders}
          mode="edit"
          onClose={() => setEditingProject(false)}
          onSaved={() => {
            setEditingProject(false);
            setNotice("项目已更新");
            void loadBoard();
            onProjectUpdated?.();
            void onProjectsChanged?.();
          }}
          initialAction={editingProject === "edit" ? undefined : editingProject}
          onDeleted={() => {
            void onProjectsChanged?.();
            onBack();
          }}
          onMerged={(target) => {
            void onProjectsChanged?.();
            if (onOpenProject) onOpenProject(target.id);
            else onBack();
          }}
          project={board}
          projects={projects}
        />
      )}

      {creatingRequirement && (
        <RequirementModal
          apiClient={apiClient}
          canPickFolders={canPickFolders}
          defaultProjectId={projectId}
          mode="create"
          onClose={() => setCreatingRequirement(false)}
          onSaved={() => {
            setCreatingRequirement(false);
            setRequirementsPage(0);
            void loadBoard();
            void loadRequirements();
          }}
          projects={projects}
        />
      )}

      {addingRoot && (
        <MaterialRootPickerModal
          apiClient={apiClient}
          busy={rootBusy}
          error={rootError}
          onClose={() => setAddingRoot(false)}
          onConfirm={(path) => void addRoot(path)}
        />
      )}

      {reselectingRoot && (
        <MaterialRootPickerModal
          apiClient={apiClient}
          busy={rootBusy}
          error={rootError}
          onClose={() => setReselectingRoot(null)}
          onConfirm={(path) => void reselectRoot(path)}
        />
      )}

      {confirmDialog}
    </section>
  );
}
