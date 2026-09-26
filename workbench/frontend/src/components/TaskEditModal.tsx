import { useEffect, useRef, useState } from "react";

import { similarProjectFrom } from "../api";
import type { ApiClient } from "../api";
import type { Project, RequirementSummary, SimilarProjectSuggestion, Task, TaskAssignee } from "../types";
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
}

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
}: TaskEditModalProps) {
  const isEdit = task !== null;
  const [description, setDescription] = useState(task?.title ?? "");
  const [projectId, setProjectId] = useState<string | null>(
    task ? task.project_id ?? null : defaultProjectId,
  );
  // 编辑已有任务时，只有动过项目下拉才提交 project_id：后端把显式 null 当成「清空项目」，
  // 没动过却带上 null 会把 AI 或会议给的项目冲掉。
  const [projectTouched, setProjectTouched] = useState(false);
  const [requirementId, setRequirementId] = useState<string | null>(
    task ? task.requirement_id ?? null : defaultRequirementId,
  );
  const [requirementOptions, setRequirementOptions] = useState<RequirementSummary[]>([]);
  const [assignee, setAssignee] = useState<TaskAssignee>(task?.assignee ?? "ai");
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
    if (!projectId) {
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
  }, [apiClient, projectId]);

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

  const primaryLabel = !isEdit
    ? "创建任务"
    : task.status === "pending_confirm"
      ? "保存并确认"
      : "保存";

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
    const payload = {
      title: trimmed,
      ...(!isEdit || projectTouched ? { project_id: projectId } : {}),
      requirement_id: requirementId,
      assignee,
    };
    try {
      if (!isEdit) {
        await apiClient.createTask(payload);
      } else if (task.status === "pending_confirm") {
        await apiClient.confirmTask(task.id, payload);
      } else {
        await apiClient.updateTask(task.id, payload);
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
          <h2>{isEdit ? "修改任务" : "新建任务"}</h2>
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
            <span className="task-edit-modal__label">任务描述</span>
            <textarea
              autoFocus={!isEdit}
              disabled={!canWrite}
              onChange={(event) => setDescription(event.target.value)}
              placeholder="要完成的事，例如：整理本周产品周报"
              rows={3}
              value={description}
            />
          </label>

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

          <div className="task-edit-modal__field">
            <span className="task-edit-modal__label">执行方</span>
            <div aria-label="执行方" className="task-edit-modal__segmented" role="group">
              <button
                aria-pressed={assignee === "ai"}
                disabled={!canWrite}
                onClick={() => setAssignee("ai")}
                type="button"
              >
                交给 AI
              </button>
              <button
                aria-pressed={assignee === "me"}
                disabled={!canWrite}
                onClick={() => setAssignee("me")}
                type="button"
              >
                我来做
              </button>
            </div>
          </div>
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
