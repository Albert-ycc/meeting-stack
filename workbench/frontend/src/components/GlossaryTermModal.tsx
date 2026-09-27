import { useEffect, useRef, useState } from "react";

import { termConflictFrom, type ApiClient } from "../api";
import type { GlossaryCategory, GlossaryTerm, GlossaryTermConflict, Project } from "../types";

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
  term?: GlossaryTerm | null;
  /** 新增时预选的项目（在某个项目的分组里点「新增」） */
  defaultProjectId?: string | null;
  onClose: () => void;
  /** message：保存结果的一句话（合并进已有词条时说明加到了哪条） */
  onSaved: (message?: string) => void;
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

/** custom 只给还挂在旧分组上的词条用：新词条只能是公共或某个项目。 */
type OwnerMode = "general" | "project" | "custom";

function initialOwnerMode(term: GlossaryTerm | null, defaultProjectId?: string | null): OwnerMode {
  if (term?.project_id) return "project";
  if (!term) return defaultProjectId ? "project" : "general";
  if (!term.scope || term.scope === "通用") return "general";
  return "custom";
}

function conflictWhere(conflict: GlossaryTermConflict) {
  return conflict.project_name ? `${conflict.project_name} 项目` : "公共 词典";
}

interface ChipInputProps {
  label: string;
  inputLabel: string;
  hint: string;
  placeholder: string;
  values: string[];
  disabled: boolean;
  removeLabel: string;
  onChange: (values: string[]) => void;
}

/** 一栏可回车添加、可 ✕ 删除的短词列表（错写 / 也叫） */
function ChipInput({ label, inputLabel, hint, placeholder, values, disabled, removeLabel, onChange }: ChipInputProps) {
  const [draft, setDraft] = useState("");
  const add = () => {
    const value = draft.trim();
    if (!value) return;
    if (!values.includes(value)) onChange([...values, value]);
    setDraft("");
  };
  return (
    <div className="glossary-modal__field">
      <span className="glossary-modal__label">{label}</span>
      <div className="glossary-modal__alias-row">
        <input
          aria-label={inputLabel}
          onChange={(event) => setDraft(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter" && !isComposingKeydown(event)) {
              event.preventDefault();
              add();
            }
          }}
          placeholder={placeholder}
          value={draft}
        />
        <button className="glossary-modal__alias-add" disabled={!draft.trim()} onClick={add} type="button">
          添加
        </button>
      </div>
      {values.length > 0 && (
        <div className="glossary-modal__alias-chips">
          {values.map((value) => (
            <span className="glossary-alias-chip" key={value}>
              {value}
              <button
                aria-label={`${removeLabel} ${value}`}
                disabled={disabled}
                onClick={() => onChange(values.filter((item) => item !== value))}
                type="button"
              >
                ✕
              </button>
            </span>
          ))}
        </div>
      )}
      <span className="glossary-modal__hint">{hint}</span>
    </div>
  );
}

export function GlossaryTermModal({
  apiClient,
  projects,
  term = null,
  defaultProjectId = null,
  onClose,
  onSaved,
}: GlossaryTermModalProps) {
  const dialogRef = useRef<HTMLDivElement>(null);
  useDialogFocus(dialogRef);
  const isEdit = term !== null;
  const startMode = initialOwnerMode(term, defaultProjectId);
  const [termText, setTermText] = useState(term?.term ?? "");
  const [aliases, setAliases] = useState<string[]>(term?.aliases ?? []);
  const [also, setAlso] = useState<string[]>(term?.also ?? []);
  const [ownerMode, setOwnerMode] = useState<OwnerMode>(startMode);
  const [ownerProjectId, setOwnerProjectId] = useState(term?.project_id ?? defaultProjectId ?? "");
  const [isCue, setIsCue] = useState(term?.is_cue ?? true);
  const [category, setCategory] = useState<string>(term?.category ?? "其他");
  const [saving, setSaving] = useState(false);
  const savingRef = useRef(false);
  const [error, setError] = useState("");
  const [conflict, setConflict] = useState<GlossaryTermConflict | null>(null);

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

  const selectedProject = projects.find((project) => project.id === ownerProjectId) ?? null;

  /** 按归属拼出接口要的 project_id / scope；给了 project_id 就不带 scope。 */
  const buildOwnerPayload = (): { project_id: string | null; scope?: string } => {
    if (ownerMode === "project") return { project_id: ownerProjectId || null };
    if (ownerMode === "custom") return { project_id: null, scope: term?.scope || "通用" };
    return { project_id: null, scope: "通用" };
  };

  const withSaving = async (work: () => Promise<void>) => {
    if (savingRef.current) return;
    savingRef.current = true;
    setSaving(true);
    setError("");
    try {
      await work();
    } catch (err) {
      setError(err instanceof Error ? err.message : "保存失败，请稍后重试");
      setSaving(false);
      savingRef.current = false;
    }
  };

  const handleSubmit = () => {
    if (!canSave) return;
    void withSaving(async () => {
      try {
        if (!isEdit) {
          await apiClient.createGlossaryTerm({
            term: trimmed,
            aliases,
            also,
            category,
            source: "manual",
            confirmed: true,
            is_cue: isCue,
            ...buildOwnerPayload(),
          });
        } else {
          await apiClient.updateGlossaryTerm(term.id, {
            term: trimmed,
            aliases,
            also,
            category,
            is_cue: isCue,
            ...buildOwnerPayload(),
          });
        }
      } catch (err) {
        const found = termConflictFrom(err);
        if (!found) throw err;
        // 重名：就地给出「加到那条」，不当成错误
        setConflict(found);
        setSaving(false);
        savingRef.current = false;
        return;
      }
      onSaved();
      onClose();
    });
  };

  const mergeInto = (makePublic: boolean) => {
    if (!conflict) return;
    void withSaving(async () => {
      await apiClient.mergeGlossaryTerm(conflict.term_id, { aliases, also, make_public: makePublic });
      if (isEdit) {
        // 编辑时改名撞上了已有词条：错写并过去以后，原来这条就多余了
        await apiClient.deleteGlossaryTerm(term.id);
      }
      onSaved(
        makePublic
          ? `已把『${conflict.term}』改成公共词，并合并了错写`
          : `已加到 ${conflict.project_name ?? "公共"} 的『${conflict.term}』`,
      );
      onClose();
    });
  };

  const conflictInOtherProject =
    conflict !== null &&
    conflict.project_id !== null &&
    !(ownerMode === "project" && ownerProjectId === conflict.project_id);

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
            <span className="glossary-modal__label">正确写法</span>
            <input
              autoFocus
              onChange={(event) => {
                setTermText(event.target.value);
                setConflict(null);
              }}
              onKeyDown={(event) => {
                // 名称框里回车直接提交；输入法选词的回车不算。
                if (event.key === "Enter" && !event.nativeEvent.isComposing) {
                  event.preventDefault();
                  handleSubmit();
                }
              }}
              placeholder="权威写法，例如：儿童生长发育"
              value={termText}
            />
            <span className="glossary-modal__hint">1–40 字，须包含中文或字母</span>
          </label>

          <div className="glossary-modal__columns">
            <ChipInput
              disabled={saving}
              hint={aliases.length > 0 ? `已添加 ${aliases.length} 条，每条 2–8 字` : "转写里常见的错字，出纪要时会改成正确写法"}
              inputLabel="别名内容"
              label="错写（会被改正，2–8 字）"
              onChange={setAliases}
              placeholder="例如：儿保科"
              removeLabel="移除别名"
              values={aliases}
            />
            <ChipInput
              disabled={saving}
              hint="缩写、全称、别称，不会被改写"
              inputLabel="也叫内容"
              label="也叫（不改，只用于识别和搜索，2–20 字）"
              onChange={setAlso}
              placeholder="例如：CRF"
              removeLabel="移除也叫"
              values={also}
            />
          </div>

          <div className="glossary-modal__field">
            <span className="glossary-modal__label">归属</span>
            <div className="glossary-modal__owner-tabs" role="radiogroup" aria-label="归属">
              <button
                aria-pressed={ownerMode === "general"}
                onClick={() => setOwnerMode("general")}
                type="button"
              >
                公共
              </button>
              <button
                aria-pressed={ownerMode === "project"}
                onClick={() => setOwnerMode("project")}
                type="button"
              >
                项目
              </button>
              {startMode === "custom" && (
                <button
                  aria-pressed={ownerMode === "custom"}
                  onClick={() => setOwnerMode("custom")}
                  type="button"
                >
                  旧分组「{term?.scope}」
                </button>
              )}
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
                <label className="glossary-modal__check">
                  <input checked={isCue} onChange={(event) => setIsCue(event.target.checked)} type="checkbox" />
                  用来识别项目（会上提到这个词，就更可能是这个项目的会）
                </label>
              </>
            )}

            <span className="glossary-modal__hint">
              {ownerMode === "project"
                ? "项目词只在这个项目的会里用来纠错和识别项目"
                : ownerMode === "custom"
                  ? "这是整理前的旧分组，建议改成公共或挂到项目"
                  : "所有会都会用到的写法"}
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

        {conflict && (
          <div className="glossary-modal__conflict" role="alert">
            <p>
              『{conflict.term}』已在 {conflictWhere(conflict)}
              {conflict.aliases.length > 0 && `（错写：${conflict.aliases.join("、")}）`}
            </p>
            <div className="glossary-modal__conflict-actions">
              {conflictInOtherProject && (
                <button className="ghost-button" disabled={saving} onClick={() => mergeInto(true)} type="button">
                  改成公共词并合并错写{isEdit && "（删掉这条）"}
                </button>
              )}
              <button className="ghost-button" disabled={saving} onClick={() => mergeInto(false)} type="button">
                {conflictInOtherProject ? `加到 ${conflict.project_name} 那条` : "把新错写加到那条"}
                {isEdit && "（删掉这条）"}
              </button>
            </div>
          </div>
        )}

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
            disabled={!canSave || conflict !== null}
            onClick={handleSubmit}
            type="button"
          >
            {saving ? "保存中…" : isEdit ? "保存修改" : "加入词典"}
          </button>
        </footer>
      </div>
    </div>
  );
}
