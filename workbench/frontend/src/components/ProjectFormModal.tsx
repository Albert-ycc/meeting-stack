import { useEffect, useRef, useState } from "react";

import type { ApiClient } from "../api";
import type { Project } from "../types";
import { FolderIcon } from "./FolderIcon";
import { useDialogFocus } from "./useDialog";
import { MaterialRootPickerModal } from "./MaterialRootPickerModal";
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

export function ProjectFormModal({
  apiClient,
  mode,
  project = null,
  canPickFolders,
  onClose,
  onSaved,
}: ProjectFormModalProps) {
  const isEdit = mode === "edit" && project !== null;
  const [name, setName] = useState(project?.name ?? "");
  const [color, setColor] = useState(project?.color ?? PROJECT_COLORS[2]);
  const [roots, setRoots] = useState<string[]>((project?.material_roots ?? []).map((root) => root.path));
  const [pickerOpen, setPickerOpen] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  const cardRef = useRef<HTMLDivElement>(null);
  useDialogFocus(cardRef);
  const pickerOpenRef = useRef(pickerOpen);
  useEffect(() => {
    pickerOpenRef.current = pickerOpen;
  }, [pickerOpen]);
  const savingRef = useRef(false);

  // Esc 关闭；子级取径器开着时不关（那一层自己处理）。表单弹窗点背景不关，免得丢掉填了一半的内容。
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Escape" || event.isComposing || pickerOpenRef.current) return;
      if (!savingRef.current) onClose();
    };
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [onClose]);

  const trimmedName = name.trim();
  const canSave = trimmedName.length > 0 && !saving;

  const removeRoot = (path: string) => setRoots((current) => current.filter((entry) => entry !== path));
  const addRoot = (path: string) => {
    setRoots((current) => (current.includes(path) ? current : [...current, path]));
    setPickerOpen(false);
  };

  const submit = async () => {
    if (!canSave || savingRef.current) return;
    savingRef.current = true;
    setSaving(true);
    setError("");
    try {
      const saved =
        mode === "create"
          ? await apiClient.createProject(trimmedName, color, roots.length ? roots : undefined)
          : await apiClient.updateProject(project!.id, { name: trimmedName, color, material_roots: roots });
      onSaved(saved);
      onClose();
    } catch (err) {
      setError(err instanceof Error ? err.message : "保存失败，请稍后重试");
      setSaving(false);
      savingRef.current = false;
    }
  };

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

        <div className="project-form-modal__body">
          <label className="project-form-modal__field">
            <span className="project-form-modal__label">项目名称</span>
            <input
              autoFocus
              onChange={(event) => setName(event.target.value)}
              onKeyDown={(event) => {
                // 名称框里回车直接提交；输入法选词的回车不算。
                if (event.key === "Enter" && !event.nativeEvent.isComposing) {
                  event.preventDefault();
                  void submit();
                }
              }}
              placeholder="例如：互联网医院"
              value={name}
            />
          </label>

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
              <button
                className="project-form-modal__add-root"
                onClick={() => setPickerOpen(true)}
                type="button"
              >
                ＋ 添加目录
              </button>
            )}
          </div>
        </div>

        {error && (
          <p className="project-form-modal__error" role="alert">
            {error}
          </p>
        )}

        <footer className="project-form-modal__footer">
          <button className="project-form-modal__cancel" disabled={saving} onClick={onClose} type="button">
            取消
          </button>
          <button
            className="project-form-modal__submit"
            disabled={!canSave}
            onClick={() => void submit()}
            type="button"
          >
            {saving ? "保存中…" : isEdit ? "保存" : "创建"}
          </button>
        </footer>
      </div>

      {pickerOpen && (
        <MaterialRootPickerModal
          apiClient={apiClient}
          onClose={() => setPickerOpen(false)}
          onConfirm={addRoot}
        />
      )}
    </div>
  );
}
