import { useEffect, useMemo, useRef, useState } from "react";
import type { ReactNode } from "react";

import { similarProjectFrom } from "../api";
import type { ApiClient } from "../api";
import type { FolderMatch, FolderMatchesPayload, Project, SimilarProjectSuggestion } from "../types";
import { FolderIcon } from "./FolderIcon";
import { MaterialRootPickerModal } from "./MaterialRootPickerModal";
import { SimilarProjectQuestion } from "./SimilarProjectQuestion";
import "./ProjectFormModal.css";

export interface ProjectFormModalProps {
  apiClient: ApiClient;
  mode: "create" | "edit";
  /** edit 模式必传 */
  project?: Project | null;
  /** 挂根目录只在桌面端出现；为 false 时材料根目录只读展示 */
  canPickFolders: boolean;
  onClose: () => void;
  onSaved: (project: Project) => void;
  /** 新建撞上近似重名、用户点「用它」时，打开已有的那个项目 */
  onUseExisting?: (projectId: string) => void;
  /** 编辑模式：传了才出现「合并到…」，合并目标从这里挑 */
  projects?: Project[];
  onMerged?: (target: Project) => void;
  /** 编辑模式：传了且项目下没有会议和需求时才出现「删除项目」 */
  onDeleted?: () => void;
}

/** 项目颜色的 8 个可选色块，和样板数据里实际用到的项目色对齐。 */
const PROJECT_COLORS = [
  "#3ecf8e",
  "#2c8d83",
  "#3f51b5",
  "#5090ff",
  "#7c3aed",
  "#831fa8",
  "#8c772c",
  "#f0783b",
];

/** 新建时最多列几个还没挂到项目的文件夹 */
const MAX_FOLDER_OPTIONS = 5;
const FOLDER_LOOKUP_DELAY_MS = 250;

type FolderChoice = { kind: "none" } | { kind: "mount"; path: string } | { kind: "create" };

const MATCH_LABELS: Record<string, string> = { exact: "同名", similar: "相近" };

export function ProjectFormModal({
  apiClient,
  mode,
  project = null,
  canPickFolders,
  onClose,
  onSaved,
  onUseExisting,
  projects,
  onMerged,
  onDeleted,
}: ProjectFormModalProps) {
  const isEdit = mode === "edit" && project !== null;
  const [name, setName] = useState(project?.name ?? "");
  const [color, setColor] = useState(project?.color ?? PROJECT_COLORS[2]);
  const initialRoots = (project?.material_roots ?? []).map((root) => root.path);
  const [roots, setRoots] = useState<string[]>(initialRoots);
  const [pickerOpen, setPickerOpen] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  // —— 新建：项目文件夹 ——
  const canLookupFolders = !isEdit && canPickFolders && typeof apiClient.folderMatches === "function";
  const [folders, setFolders] = useState<FolderMatchesPayload | null>(null);
  const [choice, setChoice] = useState<FolderChoice>({ kind: "none" });
  const [choiceTouched, setChoiceTouched] = useState(false);
  /** 用户在「选别的文件夹」里挑的、不在推荐列表里的文件夹 */
  const [extraFolder, setExtraFolder] = useState<string | null>(null);
  /** 用户改过新文件夹放在哪 */
  const [createParent, setCreateParent] = useState<string | null>(null);
  const [pickerTarget, setPickerTarget] = useState<"root" | "folder" | "parent">("root");
  /** 名字是从选中的文件夹自动填的：再换文件夹时跟着换 */
  const autoNameRef = useRef<string | null>(null);
  const [suggestion, setSuggestion] = useState<SimilarProjectSuggestion | null>(null);
  const [created, setCreated] = useState<Project | null>(null);

  // —— 编辑：合并、删除 ——
  const [danger, setDanger] = useState<"merge" | "delete" | null>(null);
  const [mergeTarget, setMergeTarget] = useState("");

  const cardRef = useRef<HTMLDivElement>(null);
  const pickerOpenRef = useRef(pickerOpen);
  useEffect(() => {
    pickerOpenRef.current = pickerOpen;
  }, [pickerOpen]);
  const savingRef = useRef(false);

  // 点卡片外关闭；子级取径器开着时不关（那一层自己处理点外）
  useEffect(() => {
    const onPointerDown = (event: MouseEvent) => {
      if (pickerOpenRef.current) return;
      if (cardRef.current && !cardRef.current.contains(event.target as Node) && !savingRef.current) {
        onClose();
      }
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Escape" || pickerOpenRef.current) return;
      if (!savingRef.current) onClose();
    };
    document.addEventListener("mousedown", onPointerDown);
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("mousedown", onPointerDown);
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [onClose]);

  const trimmedName = name.trim();

  // 名字一变就重新找同名/相近的文件夹；打开弹窗时先列最近修改的
  useEffect(() => {
    if (!canLookupFolders) return;
    let cancelled = false;
    const timer = window.setTimeout(
      () => {
        apiClient
          .folderMatches(trimmedName || undefined)
          .then((payload) => {
            if (!cancelled) setFolders(payload);
          })
          .catch(() => {
            // 找不到推荐文件夹不影响建项目，照样能「选别的文件夹」
            if (!cancelled) setFolders(null);
          });
      },
      trimmedName ? FOLDER_LOOKUP_DELAY_MS : 0,
    );
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [apiClient, canLookupFolders, trimmedName]);

  const folderOptions = useMemo(() => {
    const options: FolderMatch[] = [];
    const seen = new Set<string>();
    for (const entry of [...(folders?.matches ?? []), ...(folders?.recent ?? [])]) {
      if (seen.has(entry.path) || options.length >= MAX_FOLDER_OPTIONS) continue;
      seen.add(entry.path);
      options.push(entry);
    }
    return options;
  }, [folders]);

  // 没手动选过时：有同名文件夹就默认挂它，否则先不挂
  useEffect(() => {
    if (choiceTouched) return;
    const exact = folders?.matches.find((entry) => entry.match === "exact");
    setChoice(exact ? { kind: "mount", path: exact.path } : { kind: "none" });
  }, [folders, choiceTouched]);

  const parentForCreate = createParent ?? folders?.create_parent ?? "";
  const createName = folders?.create_name ?? "";
  // 默认位置下已经有同名文件夹时，挂它就行，不再给「新建」
  const createTarget = `${parentForCreate.replace(/\/+$/, "")}/${createName}`;
  const canCreateFolder =
    Boolean(trimmedName && createName && parentForCreate) &&
    !(folders?.matches ?? []).some((entry) => entry.path === createTarget);
  // 名字清空后「新建文件夹」这一项没了，别让它还选着
  const effectiveChoice: FolderChoice = choice.kind === "create" && !canCreateFolder ? { kind: "none" } : choice;

  const pickFolder = (folder: { path: string; name: string }) => {
    setChoiceTouched(true);
    setChoice({ kind: "mount", path: folder.path });
    setSuggestion(null);
    if (!trimmedName || name === autoNameRef.current) {
      setName(folder.name);
      autoNameRef.current = folder.name;
    }
  };

  const chooseCreate = () => {
    setChoiceTouched(true);
    setChoice({ kind: "create" });
  };

  const chooseNone = () => {
    setChoiceTouched(true);
    setChoice({ kind: "none" });
  };

  const canSave = trimmedName.length > 0 && !saving && created === null;

  const removeRoot = (path: string) => setRoots((current) => current.filter((entry) => entry !== path));

  const openPicker = (target: "root" | "folder" | "parent") => {
    setPickerTarget(target);
    setPickerOpen(true);
  };

  const onPicked = (path: string) => {
    setPickerOpen(false);
    if (pickerTarget === "parent") {
      setCreateParent(path);
      chooseCreate();
      return;
    }
    if (pickerTarget === "folder") {
      setExtraFolder(path);
      pickFolder({ path, name: path.split("/").filter(Boolean).pop() ?? path });
      return;
    }
    setRoots((current) => (current.includes(path) ? current : [...current, path]));
  };

  const beginSave = () => {
    savingRef.current = true;
    setSaving(true);
    setError("");
  };

  const endSave = () => {
    setSaving(false);
    savingRef.current = false;
  };

  const folderPayload = () => {
    if (!canLookupFolders) {
      return roots.length ? { material_roots: roots } : {};
    }
    if (effectiveChoice.kind === "mount") return { folder: { mode: "mount" as const, path: effectiveChoice.path } };
    if (effectiveChoice.kind === "create") {
      return { folder: { mode: "create" as const, path: parentForCreate, name: trimmedName } };
    }
    return {};
  };

  const create = async (force = false) => {
    if (savingRef.current) return;
    beginSave();
    try {
      const saved = await apiClient.createProjectWith({
        name: trimmedName,
        color,
        ...folderPayload(),
        ...(force ? { force: true } : {}),
      });
      setSuggestion(null);
      if (saved.folder_pending) {
        // 盘没插：项目已经建好，文件夹还没建，说一声再走
        setCreated(saved);
        endSave();
        return;
      }
      onSaved(saved);
      onClose();
    } catch (err) {
      const similar = similarProjectFrom(err);
      if (similar) setSuggestion(similar);
      else setError(err instanceof Error ? err.message : "保存失败，请稍后重试");
      endSave();
    }
  };

  const save = async () => {
    if (savingRef.current) return;
    beginSave();
    try {
      const saved = await apiClient.updateProject(project!.id, {
        name: trimmedName,
        color,
        // 根目录只在真的增删过时才提交：没变时后端什么都不用动，盘没插也不影响改名
        ...(roots.join("\u0000") !== initialRoots.join("\u0000") ? { material_roots: roots } : {}),
      });
      onSaved(saved);
      onClose();
    } catch (err) {
      setError(err instanceof Error ? err.message : "保存失败，请稍后重试");
      endSave();
    }
  };

  const submit = () => {
    if (!canSave) return;
    void (isEdit ? save() : create());
  };

  const mergeTargets = (projects ?? []).filter((entry) => entry.id !== project?.id);
  const target = mergeTargets.find((entry) => entry.id === mergeTarget) ?? null;
  const canMerge = isEdit && onMerged !== undefined && mergeTargets.length > 0;
  const canDelete =
    isEdit && onDeleted !== undefined && (project?.meeting_count ?? 0) === 0 && (project?.requirement_counts?.all ?? 0) === 0;

  const merge = async () => {
    if (!target || savingRef.current) return;
    beginSave();
    try {
      const merged = await apiClient.mergeProject(project!.id, target.id);
      onMerged?.(merged);
      onClose();
    } catch (err) {
      setError(err instanceof Error ? err.message : "合并失败，请稍后重试");
      endSave();
    }
  };

  const remove = async () => {
    if (savingRef.current) return;
    beginSave();
    try {
      await apiClient.deleteProject(project!.id);
      onDeleted?.();
      onClose();
    } catch (err) {
      setError(err instanceof Error ? err.message : "删除失败，请稍后重试");
      endSave();
    }
  };

  const folderRadio = (key: string, checked: boolean, onChange: () => void, label: ReactNode, hint?: ReactNode) => (
    <label className={`project-form-modal__option ${checked ? "is-selected" : ""}`} key={key}>
      <input checked={checked} disabled={saving} name="project-folder" onChange={onChange} type="radio" />
      <span className="project-form-modal__option-text">
        <span className="project-form-modal__option-label">{label}</span>
        {hint && <span className="project-form-modal__option-hint">{hint}</span>}
      </span>
    </label>
  );

  const offlineParent = folders?.create_parent_state === "volume_offline" && createParent === null;
  const replaced = folders?.create_replaced ?? [];

  return (
    <div className="project-form-modal__overlay">
      <div
        aria-label={isEdit ? "编辑项目" : "新建项目"}
        aria-modal="true"
        className="project-form-modal__card"
        ref={cardRef}
        role="dialog"
      >
        <header className="project-form-modal__head">
          <h2>{isEdit ? "编辑项目" : "新建项目"}</h2>
          <button
            aria-label="关闭"
            className="project-form-modal__close"
            disabled={saving}
            onClick={onClose}
            type="button"
          >
            ✕
          </button>
        </header>

        {created ? (
          <div className="project-form-modal__body">
            <p className="project-form-modal__done" role="status">
              项目「{created.name}」已建好。{created.folder_pending!.reason}：
              <span className="project-form-modal__done-path">{created.folder_pending!.path}</span>
            </p>
          </div>
        ) : (
          <div className="project-form-modal__body">
            <label className="project-form-modal__field">
              <span className="project-form-modal__label">项目名称</span>
              <input
                autoFocus
                onChange={(event) => {
                  setName(event.target.value);
                  setSuggestion(null);
                }}
                placeholder="例如：互联网医院"
                value={name}
              />
            </label>

            {suggestion && (
              <SimilarProjectQuestion
                disabled={saving}
                onForce={() => void create(true)}
                onUse={(projectId) => {
                  onClose();
                  onUseExisting?.(projectId);
                }}
                suggestion={suggestion}
              />
            )}

            <div className="project-form-modal__field">
              <span className="project-form-modal__label">项目颜色</span>
              <div aria-label="项目颜色" className="project-form-modal__swatches" role="group">
                {PROJECT_COLORS.map((swatch) => (
                  <button
                    aria-label={`选择颜色 ${swatch}`}
                    aria-pressed={color === swatch}
                    className={`project-form-modal__swatch ${color === swatch ? "is-selected" : ""}`}
                    key={swatch}
                    onClick={() => setColor(swatch)}
                    style={{ background: swatch }}
                    type="button"
                  >
                    {color === swatch && <span aria-hidden="true">✓</span>}
                  </button>
                ))}
              </div>
            </div>

            {canLookupFolders ? (
              <div aria-label="项目文件夹" className="project-form-modal__field" role="radiogroup">
                <span className="project-form-modal__label">项目文件夹</span>
                {folderOptions.map((folder) =>
                  folderRadio(
                    folder.path,
                    effectiveChoice.kind === "mount" && effectiveChoice.path === folder.path,
                    () => pickFolder(folder),
                    <>
                      <FolderIcon className="project-form-modal__root-icon" />
                      {folder.name}
                      {folder.match && <em className="project-form-modal__match">{MATCH_LABELS[folder.match]}</em>}
                    </>,
                    folder.path,
                  ),
                )}
                {extraFolder &&
                  !folderOptions.some((folder) => folder.path === extraFolder) &&
                  folderRadio(
                    extraFolder,
                    effectiveChoice.kind === "mount" && effectiveChoice.path === extraFolder,
                    () => pickFolder({ path: extraFolder, name: extraFolder.split("/").filter(Boolean).pop() ?? extraFolder }),
                    <>
                      <FolderIcon className="project-form-modal__root-icon" />
                      {extraFolder.split("/").filter(Boolean).pop()}
                    </>,
                    extraFolder,
                  )}
                {canCreateFolder &&
                  folderRadio(
                    "create",
                    effectiveChoice.kind === "create",
                    chooseCreate,
                    <>
                      在 {parentForCreate} 下新建「{createName}」
                      {replaced.length > 0 && `（${replaced.join(" ")} 已换成 -）`}
                    </>,
                    offlineParent ? "资料盘未连接，会先建项目，插上后再建文件夹" : undefined,
                  )}
                {folderRadio("none", effectiveChoice.kind === "none", chooseNone, "先不挂文件夹")}
                <span className="project-form-modal__folder-links">
                  <button className="project-form-modal__add-root" disabled={saving} onClick={() => openPicker("folder")} type="button">
                    ＋ 选别的文件夹
                  </button>
                  {canCreateFolder && (
                    <button className="project-form-modal__add-root" disabled={saving} onClick={() => openPicker("parent")} type="button">
                      改新文件夹的位置
                    </button>
                  )}
                </span>
              </div>
            ) : (
              <div className="project-form-modal__field">
                <span className="project-form-modal__label">材料根目录</span>
                {roots.length > 0 && (
                  <ul className="project-form-modal__roots">
                    {roots.map((path) => (
                      <li className="project-form-modal__root" key={path}>
                        <FolderIcon className="project-form-modal__root-icon" />
                        <span className="project-form-modal__root-path">{path}</span>
                        {canPickFolders && (
                          <button
                            aria-label={`移除 ${path}`}
                            className="project-form-modal__root-remove"
                            onClick={() => removeRoot(path)}
                            type="button"
                          >
                            ✕
                          </button>
                        )}
                      </li>
                    ))}
                  </ul>
                )}
                {canPickFolders && (
                  <button className="project-form-modal__add-root" onClick={() => openPicker("root")} type="button">
                    ＋ 添加目录
                  </button>
                )}
              </div>
            )}

            {(canMerge || canDelete) && (
              <div className="project-form-modal__danger-zone">
                {danger === null && (
                  <span className="project-form-modal__danger-links">
                    {canMerge && (
                      <button disabled={saving} onClick={() => setDanger("merge")} type="button">
                        合并到…
                      </button>
                    )}
                    {canDelete && (
                      <button disabled={saving} onClick={() => setDanger("delete")} type="button">
                        删除项目
                      </button>
                    )}
                  </span>
                )}
                {danger === "merge" && (
                  <div className="project-form-modal__confirm">
                    <label className="project-form-modal__field">
                      <span className="project-form-modal__label">合并到哪个项目</span>
                      <select
                        aria-label="合并到哪个项目"
                        disabled={saving}
                        onChange={(event) => setMergeTarget(event.target.value)}
                        value={mergeTarget}
                      >
                        <option value="">选一个项目</option>
                        {mergeTargets.map((entry) => (
                          <option key={entry.id} value={entry.id}>
                            {entry.name}
                          </option>
                        ))}
                      </select>
                    </label>
                    {target && (
                      <p className="project-form-modal__confirm-text">
                        「{project!.name}」的会议、任务、需求、词条和文件夹都并进「{target.name}」；
                        「{project!.name}」以后算作「{target.name}」的另一个叫法，这个项目会删掉。
                      </p>
                    )}
                    <span className="project-form-modal__confirm-actions">
                      <button disabled={saving} onClick={() => setDanger(null)} type="button">
                        取消
                      </button>
                      <button
                        className="project-form-modal__danger"
                        disabled={saving || !target}
                        onClick={() => void merge()}
                        type="button"
                      >
                        {saving ? "合并中…" : "确认合并"}
                      </button>
                    </span>
                  </div>
                )}
                {danger === "delete" && (
                  <div className="project-form-modal__confirm">
                    <p className="project-form-modal__confirm-text">
                      删除「{project!.name}」？它下面的任务会变成未归项目，项目词改成公共词，文件夹不动。
                    </p>
                    <span className="project-form-modal__confirm-actions">
                      <button disabled={saving} onClick={() => setDanger(null)} type="button">
                        取消
                      </button>
                      <button className="project-form-modal__danger" disabled={saving} onClick={() => void remove()} type="button">
                        {saving ? "删除中…" : "确认删除"}
                      </button>
                    </span>
                  </div>
                )}
              </div>
            )}
          </div>
        )}

        {error && (
          <p className="project-form-modal__error" role="alert">
            {error}
          </p>
        )}

        <footer className="project-form-modal__footer">
          {created ? (
            <button
              className="project-form-modal__submit"
              onClick={() => {
                onSaved(created);
                onClose();
              }}
              type="button"
            >
              知道了
            </button>
          ) : (
            <>
              <button className="project-form-modal__cancel" disabled={saving} onClick={onClose} type="button">
                取消
              </button>
              <button
                className="project-form-modal__submit"
                disabled={!canSave || danger !== null}
                onClick={submit}
                type="button"
              >
                {saving && danger === null ? "保存中…" : isEdit ? "保存" : "创建"}
              </button>
            </>
          )}
        </footer>
      </div>

      {pickerOpen && (
        <MaterialRootPickerModal
          apiClient={apiClient}
          onClose={() => setPickerOpen(false)}
          onConfirm={onPicked}
        />
      )}
    </div>
  );
}
