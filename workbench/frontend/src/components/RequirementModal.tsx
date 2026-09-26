import { useEffect, useRef, useState } from "react";

import type { ApiClient } from "../api";
import type {
  MaterialFolderStat,
  Project,
  RequirementDetail,
  RequirementPriority,
  RequirementStatus,
  RequirementSummary,
} from "../types";
import { REQUIREMENT_PRIORITIES, REQUIREMENT_STATUS_LABELS } from "./RequirementBadges";
import { MaterialFolderPickerModal } from "./MaterialFolderPickerModal";
import { useDialogFocus } from "./useDialog";
import "./RequirementModal.css";

export interface RequirementModalProps {
  apiClient: ApiClient;
  projects: Project[];
  mode: "create" | "edit";
  /** edit 必传 */
  requirement?: RequirementSummary | RequirementDetail;
  /** create 预选 */
  defaultProjectId?: string | null;
  canPickFolders: boolean;
  onClose: () => void;
  onSaved: (requirement: RequirementDetail) => void;
  /** A-02-5「去挂根目录」：项目没挂材料根目录时，二级弹窗引导去项目详情挂根目录 */
  onOpenProject?: (projectId: string) => void;
}

function hasFolders(
  requirement: RequirementSummary | RequirementDetail,
): requirement is RequirementDetail {
  return "folders" in requirement;
}

const STATUS_OPTIONS: RequirementStatus[] = ["active", "done", "shelved"];

const priorityFolderCountLabel = (folder: MaterialFolderStat) =>
  folder.file_count_capped ? "2000+ 个文件" : `${folder.file_count} 个文件`;

/** 新建 / 编辑需求弹窗（A-02-3 / A-02-6）。挂载即打开，不做路由。 */
export function RequirementModal({
  apiClient,
  projects,
  mode,
  requirement,
  defaultProjectId,
  canPickFolders,
  onClose,
  onSaved,
  onOpenProject,
}: RequirementModalProps) {
  const dialogRef = useRef<HTMLDivElement>(null);
  useDialogFocus(dialogRef);
  const [title, setTitle] = useState(requirement?.title ?? "");
  const [projectId, setProjectId] = useState(
    requirement?.project_id ?? defaultProjectId ?? projects[0]?.id ?? "",
  );
  const [priority, setPriority] = useState<RequirementPriority>(requirement?.priority ?? "P2");
  const [status, setStatus] = useState<RequirementStatus>(requirement?.status ?? "active");
  const [selectedFolders, setSelectedFolders] = useState<MaterialFolderStat[]>(
    requirement && hasFolders(requirement) ? requirement.folders : [],
  );
  const [foldersLoaded, setFoldersLoaded] = useState(
    mode === "create" || (requirement ? hasFolders(requirement) : false),
  );
  const [projectMenuOpen, setProjectMenuOpen] = useState(false);
  const [pickerOpen, setPickerOpen] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const prevProjectIdRef = useRef(projectId);
  const projectFieldRef = useRef<HTMLDivElement>(null);

  // 编辑态若手上只有列表页给的摘要（没有 folders 字段），先补一次详情，避免保存时把已有文件夹当成空覆盖掉。
  useEffect(() => {
    if (mode !== "edit" || !requirement || hasFolders(requirement)) return;
    let active = true;
    void apiClient
      .requirement(requirement.id)
      .then((detail) => {
        if (!active) return;
        setSelectedFolders(detail.folders);
        setFoldersLoaded(true);
      })
      .catch(() => {
        if (active) setFoldersLoaded(false);
      });
    return () => {
      active = false;
    };
  }, [apiClient, mode, requirement]);

  // D6：换了所属项目，旧项目根目录下的文件夹不再合法，清空让用户重选。
  useEffect(() => {
    if (prevProjectIdRef.current === projectId) return;
    prevProjectIdRef.current = projectId;
    setSelectedFolders([]);
  }, [projectId]);

  useEffect(() => {
    if (!projectMenuOpen) return;
    const onPointerDown = (event: MouseEvent) => {
      if (projectFieldRef.current && !projectFieldRef.current.contains(event.target as Node)) {
        setProjectMenuOpen(false);
      }
    };
    document.addEventListener("mousedown", onPointerDown);
    return () => document.removeEventListener("mousedown", onPointerDown);
  }, [projectMenuOpen]);

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Escape" || event.isComposing || pickerOpen || saving) return;
      onClose();
    };
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [onClose, pickerOpen, saving]);

  const currentProject = projects.find((project) => project.id === projectId) ?? null;
  const trimmedTitle = title.trim();
  const canSave = trimmedTitle.length > 0 && Boolean(projectId) && !saving;

  const removeFolder = (path: string) =>
    setSelectedFolders((current) => current.filter((folder) => folder.path !== path));

  const handleSubmit = async () => {
    if (!canSave) return;
    setSaving(true);
    setError("");
    try {
      if (mode === "create") {
        const created = await apiClient.createRequirement({
          project_id: projectId,
          title: trimmedTitle,
          priority,
          folder_paths: canPickFolders ? selectedFolders.map((folder) => folder.path) : [],
        });
        onSaved(created);
      } else {
        const patch: Parameters<ApiClient["updateRequirement"]>[1] = {
          title: trimmedTitle,
          project_id: projectId,
          priority,
          status,
        };
        if (canPickFolders && foldersLoaded) {
          patch.folder_paths = selectedFolders.map((folder) => folder.path);
        }
        const updated = await apiClient.updateRequirement(requirement!.id, patch);
        onSaved(updated);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "保存失败，请稍后重试");
      setSaving(false);
    }
  };

  return (
    <div className="requirement-modal__overlay">
      <div
        aria-label={mode === "create" ? "新建需求" : "编辑需求"}
        aria-modal="true"
        className="requirement-modal__card"
        onClick={(event) => event.stopPropagation()}
        ref={dialogRef}
        role="dialog"
      >
        <header className="requirement-modal__head">
          <h2>{mode === "create" ? "新建需求" : "编辑需求"}</h2>
          <button
            aria-label="关闭"
            className="requirement-modal__close"
            disabled={saving}
            onClick={onClose}
            type="button"
          >
            ✕
          </button>
        </header>

        <div className="requirement-modal__body">
          <label className="requirement-modal__field">
            <span className="requirement-modal__label">需求名称</span>
            <input
              autoFocus
              disabled={saving}
              onChange={(event) => setTitle(event.target.value)}
              onKeyDown={(event) => {
                // 名称框里回车直接提交；输入法选词的回车不算。
                if (event.key === "Enter" && !event.nativeEvent.isComposing) {
                  event.preventDefault();
                  void handleSubmit();
                }
              }}
              placeholder="例如：北辰仓快递配送"
              value={title}
            />
          </label>

          <div className="requirement-modal__field" ref={projectFieldRef}>
            <span className="requirement-modal__label">所属项目</span>
            <button
              aria-expanded={projectMenuOpen}
              aria-haspopup="listbox"
              className="requirement-modal__select-trigger"
              disabled={saving}
              onClick={() => setProjectMenuOpen((value) => !value)}
              type="button"
            >
              <span className="requirement-modal__select-value">
                {currentProject && (
                  <i className="requirement-modal__dot" style={{ background: currentProject.color }} />
                )}
                <span className={currentProject ? undefined : "requirement-modal__select-placeholder"}>
                  {currentProject?.name ?? "选择项目"}
                </span>
              </span>
              <span aria-hidden="true" className="requirement-modal__caret">▾</span>
            </button>
            {projectMenuOpen && (
              <div className="requirement-modal__menu" role="listbox">
                {projects.map((project) => (
                  <button
                    aria-selected={project.id === projectId}
                    className={`requirement-modal__option ${project.id === projectId ? "is-selected" : ""}`}
                    key={project.id}
                    onClick={() => {
                      setProjectId(project.id);
                      setProjectMenuOpen(false);
                    }}
                    role="option"
                    type="button"
                  >
                    <span className="requirement-modal__option-name">
                      <i className="requirement-modal__dot" style={{ background: project.color }} />
                      {project.name}
                    </span>
                    {project.id === projectId && (
                      <span aria-hidden="true" className="requirement-modal__check">✓</span>
                    )}
                  </button>
                ))}
              </div>
            )}
          </div>

          <div className="requirement-modal__field">
            <span className="requirement-modal__label">优先级</span>
            <div aria-label="优先级" className="requirement-modal__segmented" role="group">
              {REQUIREMENT_PRIORITIES.map((value) => (
                <button
                  aria-pressed={priority === value}
                  data-priority={value}
                  disabled={saving}
                  key={value}
                  onClick={() => setPriority(value)}
                  type="button"
                >
                  {value}
                </button>
              ))}
            </div>
          </div>

          {mode === "edit" && (
            <div className="requirement-modal__field">
              <span className="requirement-modal__label">状态</span>
              <div aria-label="状态" className="requirement-modal__segmented requirement-modal__segmented--status" role="group">
                {STATUS_OPTIONS.map((value) => (
                  <button
                    aria-pressed={status === value}
                    data-status={value}
                    disabled={saving}
                    key={value}
                    onClick={() => setStatus(value)}
                    type="button"
                  >
                    {REQUIREMENT_STATUS_LABELS[value]}
                  </button>
                ))}
              </div>
            </div>
          )}

          {canPickFolders && (
            <div className="requirement-modal__field">
              <span className="requirement-modal__label">材料文件夹</span>
              <button
                className="requirement-modal__pick-folder"
                disabled={saving || !projectId || !foldersLoaded}
                onClick={() => setPickerOpen(true)}
                type="button"
              >
                选择文件夹
              </button>
              {selectedFolders.length > 0 && (
                <ul className="requirement-modal__folder-list">
                  {selectedFolders.map((folder) => (
                    <li key={folder.path}>
                      <span className="requirement-modal__folder-name">{folder.name}</span>
                      <span className="requirement-modal__folder-count">
                        {priorityFolderCountLabel(folder)}
                      </span>
                      <button
                        aria-label={`移除 ${folder.name}`}
                        disabled={saving}
                        onClick={() => removeFolder(folder.path)}
                        type="button"
                      >
                        ✕
                      </button>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          )}
        </div>

        {error && (
          <p className="requirement-modal__error" role="alert">{error}</p>
        )}

        <footer className="requirement-modal__footer">
          <button className="requirement-modal__cancel" disabled={saving} onClick={onClose} type="button">
            取消
          </button>
          <button className="requirement-modal__submit" disabled={!canSave} onClick={() => void handleSubmit()} type="button">
            {saving ? "保存中…" : mode === "create" ? "创建" : "保存"}
          </button>
        </footer>

        {pickerOpen && currentProject && (
          <MaterialFolderPickerModal
            apiClient={apiClient}
            onCancel={() => setPickerOpen(false)}
            onConfirm={(folders) => {
              setSelectedFolders(folders);
              setPickerOpen(false);
            }}
            onOpenProject={(pid) => {
              // D22：去挂根目录＝放弃这次新建/编辑，整个需求弹窗一起关掉，不留半填的内容。
              onClose();
              onOpenProject?.(pid);
            }}
            projectId={currentProject.id}
            projectName={currentProject.name}
            selectedFolders={selectedFolders}
          />
        )}
      </div>
    </div>
  );
}
