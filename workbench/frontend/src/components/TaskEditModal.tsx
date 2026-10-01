import { useEffect, useRef, useState } from "react";

import { similarProjectFrom } from "../api";
import type { ApiClient } from "../api";
import type {
  Project,
  RequirementPriority,
  RequirementSummary,
  SimilarProjectSuggestion,
  Task,
  TaskAssignee,
} from "../types";
import { isComposingKeydown } from "../keyboard";
import { useDialogFocus } from "./useDialog";
import { PriorityBadge } from "./RequirementBadges";
import { SimilarProjectQuestion } from "./SimilarProjectQuestion";
import "./TaskEditModal.css";

export interface TaskEditModalProps {
  apiClient: ApiClient;
  projects: Project[];
  task?: Task | null;
  canWrite: boolean;
  onClose: () => void;
  onSaved: () => void;
  /** 仅新建模式（task 为空）生效 */
  defaultProjectId?: string | null;
  /** 仅新建模式生效 */
  defaultRequirementId?: string | null;
  /**
   * 从需求详情新建任务（R05-6、S12-c）：任务固定挂在这条需求上。弹窗里「挂到需求」是只读的一行，
   * 副标题写「挂在「需求名」下」，不能改需求，也不能改项目（所属项目由 defaultProjectId 给）。
   * 负责人默认选「我」。仅新建模式生效；不传时和以前完全一样。
   */
  fixedRequirement?: { id: string; title: string; priority: RequirementPriority };
  /** 待确认的任务点保存时是否同时确认，默认是。审核卡打开的传 false：只保存，确认留给卡上的［确认］ */
  confirmOnSave?: boolean;
}

type EditableStatus = "confirmed" | "in_progress" | "done";

const STATUS_CHOICES: Array<{ value: EditableStatus; label: string }> = [
  { value: "confirmed", label: "已确认" },
  { value: "in_progress", label: "进行中" },
  { value: "done", label: "已完成" },
];

/** 快速新建项目时给一个默认识别色，用户可到项目页再调整。 */
const DEFAULT_PROJECT_COLOR = "#3ecf8e";

export function TaskEditModal({
  apiClient,
  projects,
  task = null,
  canWrite,
  onClose,
  onSaved,
  defaultProjectId = null,
  defaultRequirementId = null,
  fixedRequirement,
  confirmOnSave = true,
}: TaskEditModalProps) {
  const isEdit = task !== null;
  const lockedRequirement = isEdit ? null : (fixedRequirement ?? null);
  const [description, setDescription] = useState(task?.title ?? "");
  const [projectId, setProjectId] = useState<string | null>(
    task ? task.project_id ?? null : defaultProjectId,
  );
  // 编辑已有任务时，只有动过项目下拉才提交 project_id：后端把显式 null 当成「清空项目」，
  // 没动过却带上 null 会把 AI 或会议给的项目冲掉。
  const [projectTouched, setProjectTouched] = useState(false);
  const [requirementId, setRequirementId] = useState<string | null>(
    task ? task.requirement_id ?? null : (fixedRequirement?.id ?? defaultRequirementId),
  );
  const [requirementOptions, setRequirementOptions] = useState<RequirementSummary[]>([]);
  const [assignee, setAssignee] = useState<TaskAssignee>(task?.assignee ?? (fixedRequirement ? "me" : "ai"));
  // 截止：只有动过才提交（清空提交 null），没动过不带 due_date，免得把纪要抽到的日期冲掉
  const [dueDate, setDueDate] = useState(task?.due_date ?? "");
  const [dueTouched, setDueTouched] = useState(false);
  // 状态分段只对已确认、进行中的任务出现；保存时变了才调状态接口
  const editableStatus = task?.status === "confirmed" || task?.status === "in_progress" ? task.status : null;
  const [status, setStatus] = useState<EditableStatus>(editableStatus ?? "confirmed");
  // 项目下拉和需求下拉只留一个开着：project | requirement | null
  const [openMenu, setOpenMenu] = useState<"project" | "requirement" | null>(null);
  const [creatingProject, setCreatingProject] = useState(false);
  const [newProjectName, setNewProjectName] = useState("");
  const [creatingProjectBusy, setCreatingProjectBusy] = useState(false);
  /** 原地新建项目撞上近似重名时，问「已有『X』，用它？」 */
  const [projectSuggestion, setProjectSuggestion] = useState<SimilarProjectSuggestion | null>(null);
  const [saving, setSaving] = useState(false);
  const savingRef = useRef(false);
  const [error, setError] = useState("");

  const cardRef = useRef<HTMLDivElement>(null);
  useDialogFocus(cardRef);
  const projectDropdownRef = useRef<HTMLDivElement>(null);
  const requirementDropdownRef = useRef<HTMLDivElement>(null);
  const openMenuRef = useRef(openMenu);
  useEffect(() => {
    openMenuRef.current = openMenu;
  }, [openMenu]);

  // 所属需求下拉跟随所属项目收窄：只列该项目进行中的需求
  useEffect(() => {
    // 固定挂在某条需求上时不用列需求
    if (!projectId || lockedRequirement) {
      setRequirementOptions([]);
      return;
    }
    let cancelled = false;
    void (async () => {
      try {
        const payload = await apiClient.requirements({ project_id: projectId, status: "active", limit: 200 });
        if (!cancelled) setRequirementOptions(payload.items);
      } catch {
        if (!cancelled) setRequirementOptions([]);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [apiClient, lockedRequirement, projectId]);

  const trimmed = description.trim();
  const canSave = trimmed.length > 0 && !saving && canWrite;

  const currentProject = projects.find((project) => project.id === projectId) ?? null;
  const currentProjectName =
    currentProject?.name ?? (isEdit && task.project_name ? task.project_name : null);

  // 需求下拉的选项来自当前项目的进行中需求；已挂的需求如果不在这批里（比如已完成/已搁置），退回任务自带的字段显示
  const matchedRequirement = requirementOptions.find((requirement) => requirement.id === requirementId) ?? null;
  const currentRequirement = matchedRequirement
    ? { title: matchedRequirement.title, priority: matchedRequirement.priority }
    : requirementId && isEdit && task.requirement_title && task.requirement_priority
      ? { title: task.requirement_title, priority: task.requirement_priority }
      : null;

  const confirming = isEdit && task.status === "pending_confirm" && confirmOnSave;
  const primaryLabel = !isEdit
    ? lockedRequirement
      ? "创建"
      : "创建任务"
    : confirming
      ? "保存并确认"
      : "保存";

  const assigneeChoices: Array<{ value: TaskAssignee; label: string }> = lockedRequirement
    ? [
        { value: "me", label: "我" },
        { value: "ai", label: "AI" },
      ]
    : [
        { value: "ai", label: "交给 AI" },
        { value: "me", label: "我来做" },
      ];

  const closeMenus = () => {
    openMenuRef.current = null;
    setOpenMenu(null);
    setCreatingProject(false);
  };

  // 点卡片外：菜单开着就收菜单（表单弹窗点背景不关，免得丢掉填了一半的内容）；点卡片内但点在开着的那个下拉外：收菜单。
  useEffect(() => {
    const onPointerDown = (event: MouseEvent) => {
      const target = event.target as Node;
      if (cardRef.current && cardRef.current.contains(target)) {
        const current = openMenuRef.current;
        if (current === "project" && projectDropdownRef.current && !projectDropdownRef.current.contains(target)) {
          closeMenus();
        } else if (
          current === "requirement" &&
          requirementDropdownRef.current &&
          !requirementDropdownRef.current.contains(target)
        ) {
          closeMenus();
        }
        return;
      }
      if (openMenuRef.current) closeMenus();
    };
    document.addEventListener("mousedown", onPointerDown);
    return () => document.removeEventListener("mousedown", onPointerDown);
  }, [onClose]);

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Escape" || event.isComposing) return;
      if (openMenuRef.current) closeMenus();
      else if (!savingRef.current) onClose();
    };
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [onClose]);

  const takeProject = (id: string) => {
    setProjectId(id);
    setProjectTouched(true);
    setRequirementId(null); // 换了项目，旧需求不再适用；新建的项目也还没有需求
    setNewProjectName("");
    setProjectSuggestion(null);
    closeMenus();
  };

  const createProject = async (force = false) => {
    const name = newProjectName.trim();
    if (!name || creatingProjectBusy) return;
    setCreatingProjectBusy(true);
    setError("");
    try {
      const created = force
        ? await apiClient.createProjectWith({ name, color: DEFAULT_PROJECT_COLOR, force: true })
        : await apiClient.createProject(name, DEFAULT_PROJECT_COLOR);
      takeProject(created.id);
    } catch (err) {
      const similar = similarProjectFrom(err);
      if (similar) setProjectSuggestion(similar);
      else setError(err instanceof Error ? err.message : "项目创建失败，请稍后重试");
    } finally {
      setCreatingProjectBusy(false);
    }
  };

  const handleSubmit = async () => {
    if (!canSave || savingRef.current) return;
    savingRef.current = true;
    setSaving(true);
    setError("");
    // 只保存不确认的待确认任务：需求的选择还在审核卡上，没动过需求下拉就不带 requirement_id，免得把它清掉
    const keepLink = isEdit && task.status === "pending_confirm" && !confirmOnSave && requirementId === (task.requirement_id ?? null);
    const payload = {
      title: trimmed,
      ...(!isEdit || projectTouched ? { project_id: projectId } : {}),
      ...(keepLink ? {} : { requirement_id: requirementId }),
      assignee,
      ...(dueTouched ? { due_date: dueDate || null } : {}),
    };
    try {
      if (!isEdit) {
        await apiClient.createTask(payload);
      } else if (confirming) {
        await apiClient.confirmTask(task.id, payload);
      } else {
        await apiClient.updateTask(task.id, payload);
        if (editableStatus && status !== editableStatus) await apiClient.setTaskStatus(task.id, status);
      }
      onSaved();
      onClose();
    } catch (err) {
      setError(err instanceof Error ? err.message : "保存失败，请稍后重试");
      setSaving(false);
      savingRef.current = false;
    }
  };

  return (
    <div className="task-edit-modal__overlay">
      <div
        aria-label={isEdit ? "修改任务" : "新建任务"}
        aria-modal="true"
        className="task-edit-modal__card"
        ref={cardRef}
        role="dialog"
      >
        <header className="task-edit-modal__head">
          {lockedRequirement ? (
            <div className="task-edit-modal__titles">
              <h2>新建任务</h2>
              <p className="task-edit-modal__subtitle">挂在「{lockedRequirement.title}」下</p>
            </div>
          ) : (
            <h2>{isEdit ? "修改任务" : "新建任务"}</h2>
          )}
          <button
            aria-label="关闭"
            className="task-edit-modal__close"
            disabled={saving}
            onClick={onClose}
            type="button"
          >
            ✕
          </button>
        </header>

        <div className="task-edit-modal__body">
          <label className="task-edit-modal__field">
            <span className="task-edit-modal__label">{lockedRequirement ? "任务名" : "任务描述"}</span>
            <textarea
              autoFocus={!isEdit}
              className={lockedRequirement ? "is-compact" : undefined}
              disabled={!canWrite}
              onChange={(event) => setDescription(event.target.value)}
              placeholder={lockedRequirement ? "要做的一件事" : "要完成的事，例如：整理本周产品周报"}
              rows={lockedRequirement ? 2 : 3}
              value={description}
            />
          </label>

          {lockedRequirement ? (
            <div className="task-edit-modal__field">
              <div className="task-edit-modal__label-row">
                <span className="task-edit-modal__label">挂到需求</span>
                <span className="task-edit-modal__hint">从需求详情新建，固定挂在这条需求上</span>
              </div>
              <div aria-label="挂到需求" className="task-edit-modal__locked" role="group">
                <svg aria-hidden="true" className="task-edit-modal__flag" fill="none" height="14" stroke="currentColor" strokeLinecap="round" strokeLinejoin="round" strokeWidth="1.5" viewBox="0 0 16 16" width="14">
                  <path d="M3.5 14V2.5M3.5 3h8l-1.8 2.7L11.5 8.4h-8" />
                </svg>
                <span className="task-edit-modal__locked-title">{lockedRequirement.title}</span>
                <PriorityBadge priority={lockedRequirement.priority} />
                <svg aria-label="不能修改" className="task-edit-modal__lock" fill="none" height="14" role="img" stroke="currentColor" strokeLinecap="round" strokeLinejoin="round" strokeWidth="1.5" viewBox="0 0 16 16" width="14">
                  <rect height="6.5" rx="1.4" width="9" x="3.5" y="7.5" />
                  <path d="M5.5 7.5V5.5a2.5 2.5 0 015 0v2" />
                </svg>
              </div>
            </div>
          ) : (
            <>
              <div className="task-edit-modal__field" ref={projectDropdownRef}>
                <span className="task-edit-modal__label">所属项目</span>
                <button
                  aria-expanded={openMenu === "project"}
                  aria-haspopup="listbox"
                  className="task-edit-modal__select-trigger"
                  disabled={!canWrite}
                  onClick={() => setOpenMenu((current) => (current === "project" ? null : "project"))}
                  type="button"
                >
                  <span className="task-edit-modal__select-value">
                    {currentProject && (
                      <i className="task-edit-modal__dot" style={{ background: currentProject.color }} />
                    )}
                    <span
                      className={currentProjectName ? undefined : "task-edit-modal__select-placeholder"}
                    >
                      {currentProjectName ?? "未归项目"}
                    </span>
                  </span>
                  <span aria-hidden="true" className="task-edit-modal__caret">
                    ▾
                  </span>
                </button>

                {openMenu === "project" && (
                  <div className="task-edit-modal__menu" role="listbox">
                    <button
                      aria-selected={projectId === null}
                      className={`task-edit-modal__option ${projectId === null ? "is-selected" : ""}`}
                      onClick={() => {
                        setProjectId(null);
                        setProjectTouched(true);
                        setRequirementId(null); // 换掉/清空所属项目，之前挂的需求不属于新项目了
                        closeMenus();
                      }}
                      role="option"
                      type="button"
                    >
                      <span className="task-edit-modal__option-name">未归项目</span>
                      {projectId === null && (
                        <span aria-hidden="true" className="task-edit-modal__check">
                          ✓
                        </span>
                      )}
                    </button>
                    {projects.map((project) => (
                      <button
                        aria-selected={project.id === projectId}
                        className={`task-edit-modal__option ${project.id === projectId ? "is-selected" : ""}`}
                        key={project.id}
                        onClick={() => {
                          setProjectId(project.id);
                          setProjectTouched(true);
                          setRequirementId(null); // 换了所属项目，之前挂的需求不属于新项目了
                          closeMenus();
                        }}
                        role="option"
                        type="button"
                      >
                        <span className="task-edit-modal__option-name">
                          <i className="task-edit-modal__dot" style={{ background: project.color }} />
                          {project.name}
                        </span>
                        {project.id === projectId && (
                          <span aria-hidden="true" className="task-edit-modal__check">
                            ✓
                          </span>
                        )}
                      </button>
                    ))}
                    <div className="task-edit-modal__menu-sep" />
                    {creatingProject ? (
                      <div className="task-edit-modal__inline-create">
                        <input
                          aria-label="新项目名称"
                          autoFocus
                          onChange={(event) => {
                            setNewProjectName(event.target.value);
                            setProjectSuggestion(null);
                          }}
                          onKeyDown={(event) => {
                            if (event.key === "Enter" && !isComposingKeydown(event)) {
                              event.preventDefault();
                              void createProject();
                            }
                          }}
                          placeholder="新项目名称"
                          value={newProjectName}
                        />
                        <button
                          className="task-edit-modal__inline-confirm"
                          disabled={creatingProjectBusy || !newProjectName.trim()}
                          onClick={() => void createProject()}
                          type="button"
                        >
                          {creatingProjectBusy ? "创建中…" : "创建"}
                        </button>
                        <button
                          className="task-edit-modal__inline-cancel"
                          disabled={creatingProjectBusy}
                          onClick={() => {
                            setNewProjectName("");
                            setProjectSuggestion(null);
                            setCreatingProject(false);
                          }}
                          type="button"
                        >
                          取消
                        </button>
                        {projectSuggestion && (
                          <SimilarProjectQuestion
                            disabled={creatingProjectBusy}
                            onForce={() => void createProject(true)}
                            onUse={takeProject}
                            suggestion={projectSuggestion}
                          />
                        )}
                      </div>
                    ) : (
                      <button
                        className="task-edit-modal__create-option"
                        onClick={() => setCreatingProject(true)}
                        type="button"
                      >
                        ＋ 新建项目
                      </button>
                    )}
                  </div>
                )}
              </div>

              <div className="task-edit-modal__field" ref={requirementDropdownRef}>
                <span className="task-edit-modal__label">所属需求</span>
                <button
                  aria-expanded={openMenu === "requirement"}
                  aria-haspopup="listbox"
                  className="task-edit-modal__select-trigger"
                  disabled={!canWrite || !projectId}
                  onClick={() => setOpenMenu((current) => (current === "requirement" ? null : "requirement"))}
                  type="button"
                >
                  <span className="task-edit-modal__select-value">
                    {currentRequirement && <PriorityBadge priority={currentRequirement.priority} />}
                    <span
                      className={currentRequirement ? undefined : "task-edit-modal__select-placeholder"}
                    >
                      {currentRequirement?.title ?? "未归需求"}
                    </span>
                  </span>
                  <span aria-hidden="true" className="task-edit-modal__caret">
                    ▾
                  </span>
                </button>

                {openMenu === "requirement" && (
                  <div className="task-edit-modal__menu" role="listbox">
                    <button
                      aria-selected={requirementId === null}
                      className={`task-edit-modal__option ${requirementId === null ? "is-selected" : ""}`}
                      onClick={() => {
                        setRequirementId(null);
                        closeMenus();
                      }}
                      role="option"
                      type="button"
                    >
                      <span className="task-edit-modal__option-name">未归需求</span>
                      {requirementId === null && (
                        <span aria-hidden="true" className="task-edit-modal__check">
                          ✓
                        </span>
                      )}
                    </button>
                    {requirementOptions.map((requirement) => (
                      <button
                        aria-selected={requirement.id === requirementId}
                        className={`task-edit-modal__option ${requirement.id === requirementId ? "is-selected" : ""}`}
                        key={requirement.id}
                        onClick={() => {
                          setRequirementId(requirement.id);
                          closeMenus();
                        }}
                        role="option"
                        type="button"
                      >
                        <span className="task-edit-modal__option-name">
                          <PriorityBadge priority={requirement.priority} />
                          {requirement.title}
                        </span>
                        {requirement.id === requirementId && (
                          <span aria-hidden="true" className="task-edit-modal__check">
                            ✓
                          </span>
                        )}
                      </button>
                    ))}
                  </div>
                )}
              </div>
            </>
          )}

          <div className="task-edit-modal__field">
            <span className="task-edit-modal__label">{lockedRequirement ? "负责人" : "执行方"}</span>
            <div aria-label={lockedRequirement ? "负责人" : "执行方"} className="task-edit-modal__segmented" role="group">
              {assigneeChoices.map((choice) => (
                <button
                  aria-pressed={assignee === choice.value}
                  disabled={!canWrite}
                  key={choice.value}
                  onClick={() => setAssignee(choice.value)}
                  type="button"
                >
                  {choice.label}
                </button>
              ))}
            </div>
          </div>
          <div className="task-edit-modal__field">
            <span className="task-edit-modal__label">截止</span>
            <div className="task-edit-modal__due">
              <input
                aria-label="截止"
                disabled={!canWrite}
                max="2099-12-31"
                min="2000-01-01"
                onChange={(event) => {
                  setDueDate(event.target.value);
                  setDueTouched(true);
                }}
                type="date"
                value={dueDate}
              />
              <button
                className="task-edit-modal__due-clear"
                disabled={!canWrite || !dueDate}
                onClick={() => {
                  setDueDate("");
                  setDueTouched(true);
                }}
                type="button"
              >
                清空＝未定
              </button>
              {isEdit && task.due_phrase ? (
                <span className="task-edit-modal__due-note">纪要原话：{task.due_phrase}</span>
              ) : isEdit && !task.due_date ? (
                <span className="task-edit-modal__due-note">纪要里没抽到时间</span>
              ) : null}
            </div>
          </div>

          {editableStatus && (
            <div className="task-edit-modal__field">
              <span className="task-edit-modal__label">状态</span>
              <div aria-label="状态" className="task-edit-modal__segmented task-edit-modal__segmented--three" role="group">
                {STATUS_CHOICES.map((choice) => (
                  <button
                    aria-pressed={status === choice.value}
                    disabled={!canWrite}
                    key={choice.value}
                    onClick={() => setStatus(choice.value)}
                    type="button"
                  >
                    {choice.label}
                  </button>
                ))}
              </div>
            </div>
          )}
        </div>

        {error && (
          <p className="task-edit-modal__error" role="alert">
            {error}
          </p>
        )}

        <footer className="task-edit-modal__footer">
          <button
            className="task-edit-modal__cancel"
            disabled={saving}
            onClick={onClose}
            type="button"
          >
            取消
          </button>
          <button
            className="task-edit-modal__submit"
            disabled={!canSave}
            onClick={() => void handleSubmit()}
            type="button"
          >
            {saving ? "保存中…" : primaryLabel}
          </button>
        </footer>
      </div>
    </div>
  );
}
