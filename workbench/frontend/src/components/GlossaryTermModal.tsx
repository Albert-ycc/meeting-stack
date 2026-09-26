import { useEffect, useRef, useState } from "react";

import type { ApiClient } from "../api";
import type { GlossaryCategory, GlossaryTerm, Project } from "../types";

import { isComposingKeydown } from "../keyboard";
import "./GlossaryTermModal.css";
import { useDialogFocus } from "./useDialog";

/** 中文输入法组合态下按 Enter 是在确认拼音候选字，不是要提交这一条别名；
 * 不做这层判断，会把还没敲完的候选词当成别名提前塞进去（复现见「新增术语」回归测试）。
 * `isComposing` 覆盖标准实现，`keyCode === 229` 兜底旧版 IME 不设它的情况。 */

export interface GlossaryTermModalProps {
  apiClient: ApiClient;
  /** 归属＝项目 时的候选列表；来自 App 已加载的项目集合，不单独拉取。 */
  projects: Project[];
  /** 归属＝其他范围 时的输入建议，取自现有自由桶名，方便复用而不是每次新造一个。 */
  bucketSuggestions?: string[];
  term?: GlossaryTerm | null;
  onClose: () => void;
  onSaved: () => void;
}

/** 分类字典与后端 CATEGORIES 契约一致，别单独改。 */
const GLOSSARY_CATEGORIES: GlossaryCategory[] = [
  "人名",
  "机构",
  "术语",
  "药品",
  "地名",
  "其他",
];

type OwnerMode = "general" | "project" | "custom";

/** 词条归属的三种落点，与词典页 chips 的 kind 一一对应。 */
function initialOwnerMode(term: GlossaryTerm | null): OwnerMode {
  if (term?.project_id) return "project";
  if (!term || !term.scope || term.scope === "通用") return "general";
  return "custom";
}

export function GlossaryTermModal({
  apiClient,
  projects,
  bucketSuggestions = [],
  term = null,
  onClose,
  onSaved,
}: GlossaryTermModalProps) {
  const dialogRef = useRef<HTMLDivElement>(null);
  useDialogFocus(dialogRef);
  const isEdit = term !== null;
  const [termText, setTermText] = useState(term?.term ?? "");
  const [aliases, setAliases] = useState<string[]>(term?.aliases ?? []);
  const [aliasDraft, setAliasDraft] = useState("");
  const [ownerMode, setOwnerMode] = useState<OwnerMode>(() => initialOwnerMode(term));
  const [ownerProjectId, setOwnerProjectId] = useState(term?.project_id ?? "");
  const [ownerCustom, setOwnerCustom] = useState(
    initialOwnerMode(term) === "custom" ? term?.scope ?? "" : "",
  );
  const [category, setCategory] = useState<string>(term?.category ?? "其他");
  const [saving, setSaving] = useState(false);
  const savingRef = useRef(false);
  const [error, setError] = useState("");

  const trimmed = termText.trim();
  const canSave =
    trimmed.length > 0 &&
    !saving &&
    (ownerMode !== "project" || ownerProjectId.length > 0);

  // 保存进行中禁掉 Escape 关闭，避免误触丢数据（与任务弹窗同款约定）。
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !event.isComposing && !savingRef.current) onClose();
    };
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [onClose]);

  const addAlias = () => {
    const value = aliasDraft.trim();
    if (!value) return;
    if (aliases.includes(value)) return;
    setAliases((current) => [...current, value]);
    setAliasDraft("");
  };

  const removeAlias = (value: string) =>
    setAliases((current) => current.filter((alias) => alias !== value));

  const selectedProject = projects.find((project) => project.id === ownerProjectId) ?? null;

  /** 按归属三态拼出接口要的 project_id / scope；给了 project_id 就不带 scope。 */
  const buildOwnerPayload = (): { project_id: string | null; scope?: string } => {
    if (ownerMode === "project") return { project_id: ownerProjectId || null };
    if (ownerMode === "custom") return { project_id: null, scope: ownerCustom.trim() || "通用" };
    return { project_id: null, scope: "通用" };
  };

  const handleSubmit = async () => {
    if (!canSave || savingRef.current) return;
    savingRef.current = true;
    setSaving(true);
    setError("");
    try {
      if (!isEdit) {
        await apiClient.createGlossaryTerm({
          term: trimmed,
          aliases,
          category,
          source: "manual",
          confirmed: true,
          ...buildOwnerPayload(),
        });
      } else {
        await apiClient.updateGlossaryTerm(term.id, {
          term: trimmed,
          aliases,
          category,
          ...buildOwnerPayload(),
        });
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
    <div className="glossary-modal__overlay">
      <div
        aria-label={isEdit ? "编辑术语" : "新增术语"}
        aria-modal="true"
        className="glossary-modal__card"
        ref={dialogRef}
        role="dialog"
      >
        <header className="glossary-modal__head">
          <h2>{isEdit ? "编辑术语" : "新增术语"}</h2>
          <button
            aria-label="关闭"
            className="glossary-modal__close"
            disabled={saving}
            onClick={onClose}
            type="button"
          >
            ✕
          </button>
        </header>

        <div className="glossary-modal__body">
          <label className="glossary-modal__field">
            <span className="glossary-modal__label">术语</span>
            <input
              autoFocus
              onChange={(event) => setTermText(event.target.value)}
              onKeyDown={(event) => {
                // 名称框里回车直接提交；输入法选词的回车不算。
                if (event.key === "Enter" && !event.nativeEvent.isComposing) {
                  event.preventDefault();
                  void handleSubmit();
                }
              }}
              placeholder="权威写法，例如：儿童生长发育"
              value={termText}
            />
            <span className="glossary-modal__hint">1–40 字，须包含中文或字母</span>
          </label>

          <div className="glossary-modal__field">
            <span className="glossary-modal__label">别名</span>
            <div className="glossary-modal__alias-row">
              <input
                aria-label="别名内容"
                onChange={(event) => setAliasDraft(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === "Enter" && !isComposingKeydown(event)) {
                    event.preventDefault();
                    addAlias();
                  }
                }}
                placeholder="转写里常见的错字，例如：儿保科"
                value={aliasDraft}
              />
              <button
                className="glossary-modal__alias-add"
                disabled={!aliasDraft.trim()}
                onClick={addAlias}
                type="button"
              >
                添加
              </button>
            </div>
            {aliases.length > 0 && (
              <div className="glossary-modal__alias-chips">
                {aliases.map((alias) => (
                  <span className="glossary-alias-chip" key={alias}>
                    {alias}
                    <button
                      aria-label={`移除别名 ${alias}`}
                      disabled={saving}
                      onClick={() => removeAlias(alias)}
                      type="button"
                    >
                      ✕
                    </button>
                  </span>
                ))}
              </div>
            )}
            <span className="glossary-modal__hint">
              {aliases.length > 0
                ? `已添加 ${aliases.length} 条别名，每条 2–8 字`
                : "可留空；别名越多，纪要错字修正越准"}
            </span>
          </div>

          <div className="glossary-modal__field">
            <span className="glossary-modal__label">归属</span>
            <div className="glossary-modal__owner-tabs" role="radiogroup" aria-label="归属">
              <button
                aria-pressed={ownerMode === "general"}
                onClick={() => setOwnerMode("general")}
                type="button"
              >
                通用
              </button>
              <button
                aria-pressed={ownerMode === "project"}
                onClick={() => setOwnerMode("project")}
                type="button"
              >
                项目
              </button>
              <button
                aria-pressed={ownerMode === "custom"}
                onClick={() => setOwnerMode("custom")}
                type="button"
              >
                其他范围
              </button>
            </div>

            {ownerMode === "project" && (
              <>
                <select
                  aria-label="选择项目"
                  onChange={(event) => setOwnerProjectId(event.target.value)}
                  value={ownerProjectId}
                >
                  <option value="">选择项目…</option>
                  {projects.map((project) => (
                    <option key={project.id} value={project.id}>
                      {project.name}
                    </option>
                  ))}
                </select>
                {selectedProject && (
                  <span className="glossary-modal__owner-preview">
                    <i aria-hidden="true" style={{ background: selectedProject.color }} />
                    {selectedProject.name}
                  </span>
                )}
              </>
            )}

            {ownerMode === "custom" && (
              <>
                <input
                  list="glossary-modal-bucket-suggestions"
                  onChange={(event) => setOwnerCustom(event.target.value)}
                  placeholder="新范围名称，例如：儿科"
                  value={ownerCustom}
                />
                <datalist id="glossary-modal-bucket-suggestions">
                  {bucketSuggestions.map((name) => (
                    <option key={name} value={name} />
                  ))}
                </datalist>
              </>
            )}

            <span className="glossary-modal__hint">
              {ownerMode === "project"
                ? "挂到某个项目下，词典页按项目分组时会归到这里"
                : ownerMode === "custom"
                  ? "不挂具体项目，按自定义范围名分组"
                  : "不区分项目的通用写法"}
            </span>
          </div>

          <div className="glossary-modal__field">
            <span className="glossary-modal__label">分类</span>
            <select
              onChange={(event) => setCategory(event.target.value)}
              value={category}
            >
              {GLOSSARY_CATEGORIES.map((option) => (
                <option key={option} value={option}>
                  {option}
                </option>
              ))}
            </select>
          </div>
        </div>

        {error && (
          <p className="glossary-modal__error" role="alert">
            {error}
          </p>
        )}

        <footer className="glossary-modal__footer">
          <button
            className="glossary-modal__cancel"
            disabled={saving}
            onClick={onClose}
            type="button"
          >
            取消
          </button>
          <button
            className="glossary-modal__submit"
            disabled={!canSave}
            onClick={() => void handleSubmit()}
            type="button"
          >
            {saving ? "保存中…" : isEdit ? "保存修改" : "加入词典"}
          </button>
        </footer>
      </div>
    </div>
  );
}
