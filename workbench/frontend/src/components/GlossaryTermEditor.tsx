import { useEffect, useMemo, useRef, useState } from "react";

import { termConflictFrom, type ApiClient } from "../api";
import { formatTime } from "../format";
import { isComposingKeydown } from "../keyboard";
import type { CueTermDetail } from "./graph/graphTypes";
import type { GlossaryTerm, GlossaryTermConflict, Project } from "../types";
import { GLOSSARY_CATEGORIES, formatFullDate, glossaryTextProblem, termNameError } from "./glossaryModel";
import { IconBack, IconPlay, IconTrash, IconX } from "./glossaryUi";

/**
 * 词典右栏：一条词条的详情，同时就是编辑表单（原来的编辑弹窗搬到这里）。
 * 校验、重名冲突（加到那条 / 改成公共词并合并 / 编辑时删掉这条）、归属三选一、输入法回车判断都在这里，
 * 页面只管选中哪条、保存后刷新。term 为 null 时是新增。
 */

export interface TermEditorSaved {
  message: string;
  termId: string;
  /** 接口返回的词条；合并进已有词条时没有 */
  term?: GlossaryTerm;
  /** 保存后这条落在哪个范围，页面据此把左栏的选中项跟过去 */
  owner: Pick<GlossaryTerm, "project_id" | "scope">;
}

interface TermEditorProps {
  apiClient: ApiClient;
  /** 归属＝项目 时的候选列表；来自 App 已加载的项目集合，不单独拉取。 */
  projects: Project[];
  canWrite: boolean;
  term: GlossaryTerm | null;
  /** 新增时预选的项目（在某个项目的范围里点「新增」） */
  defaultProjectId?: string | null;
  /** 新增时预填的正确写法（搜不到时「把『X』加进词典」） */
  initialText?: string;
  /** 面包屑上的归属名，新增时不显示 */
  ownerLabel?: { name: string; color: string | null } | null;
  onDirtyChange: (dirty: boolean) => void;
  onSaved: (result: TermEditorSaved) => void;
  /** 新增时取消 / Esc：回到没有选中的状态 */
  onCancelNew: () => void;
  onDelete?: (term: GlossaryTerm) => void;
  /** 手机单栏：回到列表 */
  onBack?: () => void;
  onOpenMeeting?: (meetingId: string, seekMs: number) => void;
}

/** custom 只给还挂在旧分组上的词条用：新词条只能是公共或某个项目。 */
type OwnerMode = "general" | "project" | "custom";

interface Draft {
  text: string;
  aliases: string[];
  also: string[];
  ownerMode: OwnerMode;
  projectId: string;
  isCue: boolean;
  category: string;
}

function initialOwnerMode(term: GlossaryTerm | null, defaultProjectId?: string | null): OwnerMode {
  if (term?.project_id) return "project";
  if (!term) return defaultProjectId ? "project" : "general";
  if (!term.scope || term.scope === "通用") return "general";
  return "custom";
}

function draftFrom(term: GlossaryTerm | null, defaultProjectId?: string | null, initialText = ""): Draft {
  return {
    text: term?.term ?? initialText,
    aliases: term?.aliases ?? [],
    also: term?.also ?? [],
    ownerMode: initialOwnerMode(term, defaultProjectId),
    projectId: term?.project_id ?? defaultProjectId ?? "",
    isCue: term?.is_cue ?? true,
    category: term?.category ?? "其他",
  };
}

function conflictWhere(conflict: GlossaryTermConflict) {
  return conflict.project_name ? `${conflict.project_name} 项目` : "公共 词典";
}

/** 错写 2–8 字，也叫 2–20 字 */
const CHIP_LIMITS = { aliases: [2, 8], also: [2, 20] } as const;

interface ChipFieldProps {
  kind: "aliases" | "also";
  label: string;
  note: string;
  placeholder: string;
  addLabel: string;
  removeLabel: string;
  hint: string;
  values: string[];
  draft: string;
  readOnly: boolean;
  saving: boolean;
  term: string;
  onDraft: (value: string) => void;
  onChange: (values: string[]) => void;
}

/** 一栏可回车添加、可 ✕ 删除的短词列表（错写 / 也叫） */
function ChipField({
  kind, label, note, placeholder, addLabel, removeLabel, hint, values, draft, readOnly, saving, term, onDraft, onChange,
}: ChipFieldProps) {
  const [problem, setProblem] = useState("");
  const [min, max] = CHIP_LIMITS[kind];
  const add = () => {
    const value = draft.trim();
    if (!value) return;
    const wrong = glossaryTextProblem(value, min, max);
    if (wrong) {
      setProblem(wrong === "length" ? `每条 ${min}–${max} 字` : "要有中文或字母，不能是纯数字");
      return;
    }
    if (values.includes(value) || value === term) {
      setProblem("已经有这一条了");
      return;
    }
    setProblem("");
    onChange([...values, value]);
    onDraft("");
  };
  return (
    <div className="gw-block">
      <div className="gw-block__label">
        {label} <em>{note}</em>
      </div>
      <div className="gw-chips">
        {values.map((value) => (
          <span className={`gw-chip${readOnly ? " gw-chip--ro" : ""}`} key={value}>
            {value}
            {!readOnly && (
              <button
                aria-label={`${removeLabel}：${value}`}
                disabled={saving}
                onClick={() => onChange(values.filter((item) => item !== value))}
                type="button"
              >
                <IconX />
              </button>
            )}
          </span>
        ))}
        {readOnly && values.length === 0 && <span className="gw-hint">{kind === "aliases" ? "还没有错写" : "没有"}</span>}
        {!readOnly && (
          <input
            aria-label={addLabel}
            className="gw-chipin"
            onChange={(event) => {
              onDraft(event.target.value);
              setProblem("");
            }}
            onKeyDown={(event) => {
              if (event.key === "Enter" && !isComposingKeydown(event)) {
                event.preventDefault();
                add();
              }
            }}
            placeholder={placeholder}
            value={draft}
          />
        )}
      </div>
      <p className={`gw-hint${problem ? " gw-hint--err" : ""}`}>{problem || hint}</p>
    </div>
  );
}

function TermMeetings({
  apiClient,
  termId,
  onOpenMeeting,
}: {
  apiClient: ApiClient;
  termId: string;
  onOpenMeeting?: (meetingId: string, seekMs: number) => void;
}) {
  const [detail, setDetail] = useState<CueTermDetail | null>(null);
  const [state, setState] = useState<"loading" | "ready" | "error">("loading");
  useEffect(() => {
    let alive = true;
    setState("loading");
    setDetail(null);
    if (typeof apiClient.glossaryTermDetail !== "function") {
      setState("ready");
      return;
    }
    apiClient
      .glossaryTermDetail(termId)
      .then((value) => {
        if (!alive) return;
        setDetail(value);
        setState("ready");
      })
      .catch(() => alive && setState("error"));
    return () => {
      alive = false;
    };
  }, [apiClient, termId]);

  const meetings = detail?.cue_meetings ?? [];
  return (
    <div className="gw-block" data-testid="term-meetings">
      <div className="gw-block__label">
        在哪些会上被提到 {meetings.length > 0 && <em>{meetings.length} 场</em>}
      </div>
      {state === "loading" && <p className="gw-hint">正在读取…</p>}
      {state === "error" && <p className="gw-softempty">这条的会议记录没读到，稍后再打开试试。</p>}
      {state === "ready" && meetings.length === 0 && (
        <p className="gw-softempty">还没有记录到。之后的新会议里提到它会记在这里。</p>
      )}
      {meetings.map((meeting) => (
        <div className="gw-meet" key={meeting.meeting_id}>
          <span className="gw-meet__title">{meeting.title}</span>
          <span className="gw-meet__count">提到 {meeting.count} 次</span>
          <span className="gw-meet__date">
            {formatFullDate(meeting.date)}
            {meeting.anchors_ms.length === 0 && " · 这场会里只用来识别项目，没有逐句时间点"}
          </span>
          {meeting.anchors_ms.length > 0 && (
            <span className="gw-meet__anchors">
              {meeting.anchors_ms.map((ms) => (
                <button
                  className="gw-tchip"
                  key={ms}
                  onClick={() => onOpenMeeting?.(meeting.meeting_id, ms)}
                  type="button"
                >
                  <IconPlay />
                  {formatTime(ms, true)}
                </button>
              ))}
            </span>
          )}
        </div>
      ))}
    </div>
  );
}

export function GlossaryTermEditor(props: TermEditorProps) {
  // Esc 放弃修改 = 换一个 nonce 重新挂载内部表单，草稿回到已保存的样子。
  const [nonce, setNonce] = useState(0);
  return <TermEditorForm key={nonce} {...props} onReset={() => setNonce((value) => value + 1)} />;
}

function TermEditorForm({
  apiClient,
  projects,
  canWrite,
  term,
  defaultProjectId = null,
  initialText = "",
  ownerLabel,
  onDirtyChange,
  onSaved,
  onCancelNew,
  onDelete,
  onBack,
  onOpenMeeting,
  onReset,
}: TermEditorProps & { onReset: () => void }) {
  const isEdit = term !== null;
  const [baseline, setBaseline] = useState<Draft>(() => draftFrom(term, defaultProjectId, initialText));
  const [draft, setDraft] = useState<Draft>(baseline);
  const [aliasDraft, setAliasDraft] = useState("");
  const [alsoDraft, setAlsoDraft] = useState("");
  const [saving, setSaving] = useState(false);
  const savingRef = useRef(false);
  const [error, setError] = useState("");
  const [conflict, setConflict] = useState<GlossaryTermConflict | null>(null);
  const startedMode = initialOwnerMode(term, defaultProjectId);
  const titleRef = useRef<HTMLInputElement>(null);

  const patch = (next: Partial<Draft>) => setDraft((current) => ({ ...current, ...next }));

  const trimmed = draft.text.trim();
  const nameError = termNameError(trimmed);
  const dirty = isEdit
    ? JSON.stringify(draft) !== JSON.stringify(baseline) || aliasDraft.trim() !== "" || alsoDraft.trim() !== ""
    : trimmed !== "" || draft.aliases.length > 0 || draft.also.length > 0 || aliasDraft.trim() !== "" || alsoDraft.trim() !== "";
  const canSave =
    trimmed.length > 0 && !nameError && !saving && (draft.ownerMode !== "project" || draft.projectId.length > 0);

  useEffect(() => onDirtyChange(dirty), [dirty, onDirtyChange]);
  useEffect(() => () => onDirtyChange(false), [onDirtyChange]);

  // 新增时光标直接落在正确写法上
  useEffect(() => {
    if (!isEdit) titleRef.current?.focus();
  }, [isEdit]);

  // Esc 放弃修改；保存进行中、输入法组合中、确认框开着时不响应。
  useEffect(() => {
    if (!canWrite) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Escape" || event.isComposing || event.keyCode === 229) return;
      if (savingRef.current) {
        event.preventDefault();
        return;
      }
      if (document.querySelector('[role="alertdialog"]')) return;
      if (!dirty && isEdit) return;
      event.preventDefault();
      if (isEdit) onReset();
      else onCancelNew();
    };
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [canWrite, dirty, isEdit, onCancelNew, onReset]);

  const selectedProject = projects.find((project) => project.id === draft.projectId) ?? null;

  /** 按归属拼出接口要的 project_id / scope；给了 project_id 就不带 scope。 */
  const ownerPayload = (): { project_id: string | null; scope?: string } => {
    if (draft.ownerMode === "project") return { project_id: draft.projectId || null };
    if (draft.ownerMode === "custom") return { project_id: null, scope: term?.scope || "通用" };
    return { project_id: null, scope: "通用" };
  };

  /** 敲了还没回车的错写 / 也叫，保存时一并带上（合规的才带） */
  const flushed = useMemo(() => {
    const take = (values: string[], pending: string, kind: "aliases" | "also") => {
      const value = pending.trim();
      const [min, max] = CHIP_LIMITS[kind];
      if (!value || glossaryTextProblem(value, min, max) || values.includes(value) || value === trimmed) return values;
      return [...values, value];
    };
    return { aliases: take(draft.aliases, aliasDraft, "aliases"), also: take(draft.also, alsoDraft, "also") };
  }, [draft.aliases, draft.also, aliasDraft, alsoDraft, trimmed]);

  const withSaving = async (work: () => Promise<void>) => {
    if (savingRef.current) return;
    savingRef.current = true;
    setSaving(true);
    setError("");
    try {
      await work();
      savingRef.current = false;
      setSaving(false);
    } catch (err) {
      setError(err instanceof Error ? err.message : "保存失败，请稍后重试");
      setSaving(false);
      savingRef.current = false;
    }
  };

  const handleSubmit = () => {
    // 名称框里的回车也走这里：和保存按钮同一个条件，没改动时不原样再写一遍
    if (!canSave || conflict || (isEdit && !dirty)) return;
    void withSaving(async () => {
      let saved: GlossaryTerm;
      try {
        if (!isEdit) {
          saved = await apiClient.createGlossaryTerm({
            term: trimmed,
            aliases: flushed.aliases,
            also: flushed.also,
            category: draft.category,
            source: "manual",
            confirmed: true,
            is_cue: draft.isCue,
            ...ownerPayload(),
          });
        } else {
          saved = await apiClient.updateGlossaryTerm(term.id, {
            term: trimmed,
            aliases: flushed.aliases,
            also: flushed.also,
            category: draft.category,
            is_cue: draft.isCue,
            ...ownerPayload(),
          });
        }
      } catch (err) {
        const found = termConflictFrom(err);
        if (!found) throw err;
        // 重名：就地给出「加到那条」，不当成错误
        setConflict(found);
        return;
      }
      const next = { ...draft, text: trimmed, aliases: flushed.aliases, also: flushed.also };
      setBaseline(next);
      setDraft(next);
      setAliasDraft("");
      setAlsoDraft("");
      onSaved({
        message: isEdit ? `已保存『${trimmed}』` : `已加入词典：『${trimmed}』`,
        termId: saved?.id ?? term?.id ?? "",
        term: saved,
        owner: {
          project_id: ownerPayload().project_id,
          scope: draft.ownerMode === "project" ? selectedProject?.name ?? "" : ownerPayload().scope ?? "通用",
        },
      });
    });
  };

  const mergeInto = (makePublic: boolean) => {
    if (!conflict) return;
    void withSaving(async () => {
      await apiClient.mergeGlossaryTerm(conflict.term_id, { aliases: flushed.aliases, also: flushed.also, make_public: makePublic });
      if (isEdit) {
        // 编辑时改名撞上了已有词条：错写并过去以后，原来这条就多余了
        await apiClient.deleteGlossaryTerm(term.id);
      }
      setBaseline(draft);
      onSaved({
        message: makePublic
          ? `已把『${conflict.term}』改成公共词，并合并了错写`
          : `已加到 ${conflict.project_name ?? "公共"} 的『${conflict.term}』`,
        termId: conflict.term_id,
        owner: makePublic
          ? { project_id: null, scope: "通用" }
          : { project_id: conflict.project_id, scope: conflict.project_name ?? "通用" },
      });
    });
  };

  const conflictInOtherProject =
    conflict !== null &&
    conflict.project_id !== null &&
    !(draft.ownerMode === "project" && draft.projectId === conflict.project_id);

  const readOnly = !canWrite;
  const footerStatus = saving ? "保存中…" : !isEdit ? "回车加入词典" : dirty ? "有未保存的修改" : "改完会亮起保存";

  return (
    <>
      <div className="gw-dscroll">
        <div className="gw-crumb">
          {onBack && (
            <button className="gw-btn gw-btn--sm gw-btn--ghost gw-mback" onClick={onBack} type="button">
              <IconBack />
              列表
            </button>
          )}
          {isEdit ? (
            <>
              {ownerLabel?.color ? <i className="gw-dot" style={{ background: ownerLabel.color }} /> : null}
              <span>{ownerLabel?.name ?? "公共"}</span>
              <span>/</span>
              <span>{term.category}</span>
            </>
          ) : (
            <span>新增术语</span>
          )}
          <span className="gw-crumb__sp" />
          {isEdit && canWrite && onDelete && (
            <button
              aria-label="删除这条"
              className="gw-iconbtn"
              disabled={saving}
              onClick={() => onDelete(term)}
              title="删除"
              type="button"
            >
              <IconTrash size={14} />
            </button>
          )}
        </div>

        <input
          aria-label="正确写法"
          className="gw-dtitle"
          maxLength={60}
          onChange={(event) => {
            patch({ text: event.target.value });
            setConflict(null);
          }}
          onKeyDown={(event) => {
            // 名称框里回车直接提交；输入法选词的回车不算。
            if (event.key === "Enter" && !isComposingKeydown(event)) {
              event.preventDefault();
              handleSubmit();
            }
          }}
          placeholder="正确写法"
          readOnly={readOnly}
          ref={titleRef}
          value={draft.text}
        />
        <p className={`gw-hint${nameError ? " gw-hint--err" : ""}`}>
          {nameError || (readOnly ? "" : "正确写法 · 1–40 字，须包含中文或字母")}
        </p>

        <ChipField
          addLabel="添加错写"
          draft={aliasDraft}
          hint={draft.aliases.length > 0 ? `已添加 ${draft.aliases.length} 条` : "转写里常见的错字，出纪要时会改成正确写法"}
          kind="aliases"
          label="错写"
          note="会被改正，每条 2–8 字"
          onChange={(aliases) => patch({ aliases })}
          onDraft={setAliasDraft}
          placeholder="＋ 添加错写"
          readOnly={readOnly}
          removeLabel="移除错写"
          saving={saving}
          term={trimmed}
          values={draft.aliases}
        />
        <ChipField
          addLabel="添加也叫"
          draft={alsoDraft}
          hint="缩写、全称、别称，不会被改写"
          kind="also"
          label="也叫"
          note="不改，只用于识别和搜索，2–20 字"
          onChange={(also) => patch({ also })}
          onDraft={setAlsoDraft}
          placeholder="＋ 添加也叫"
          readOnly={readOnly}
          removeLabel="移除也叫"
          saving={saving}
          term={trimmed}
          values={draft.also}
        />

        <div className="gw-block">
          <div className="gw-props">
            <span className="gw-props__k">归属</span>
            <span aria-label="归属" className="gw-seg" role="radiogroup">
              <button
                aria-checked={draft.ownerMode === "general"}
                disabled={readOnly}
                onClick={() => patch({ ownerMode: "general" })}
                role="radio"
                type="button"
              >
                公共
              </button>
              <button
                aria-checked={draft.ownerMode === "project"}
                disabled={readOnly}
                onClick={() => patch({ ownerMode: "project" })}
                role="radio"
                type="button"
              >
                项目
              </button>
              {startedMode === "custom" && (
                <button
                  aria-checked={draft.ownerMode === "custom"}
                  disabled={readOnly}
                  onClick={() => patch({ ownerMode: "custom" })}
                  role="radio"
                  type="button"
                >
                  旧分组「{term?.scope}」
                </button>
              )}
            </span>
            {draft.ownerMode === "project" && (
              <>
                <span className="gw-props__k">项目</span>
                <span className="gw-sel">
                  {selectedProject && <i className="gw-dot" style={{ background: selectedProject.color }} />}
                  <select
                    aria-label="选择项目"
                    disabled={readOnly}
                    onChange={(event) => patch({ projectId: event.target.value })}
                    value={draft.projectId}
                  >
                    <option value="">选择项目…</option>
                    {projects.map((project) => (
                      <option key={project.id} value={project.id}>
                        {project.name}
                      </option>
                    ))}
                  </select>
                </span>
                <span />
                <label className="gw-check">
                  <input
                    checked={draft.isCue}
                    disabled={readOnly}
                    onChange={(event) => patch({ isCue: event.target.checked })}
                    type="checkbox"
                  />
                  <span>
                    用来识别项目
                    <small>会上提到这个词，就更可能是这个项目的会</small>
                  </span>
                </label>
              </>
            )}
            <span className="gw-props__k">分类</span>
            <span className="gw-sel">
              <select
                aria-label="分类"
                disabled={readOnly}
                onChange={(event) => patch({ category: event.target.value })}
                value={draft.category}
              >
                {GLOSSARY_CATEGORIES.map((option) => (
                  <option key={option} value={option}>
                    {option}
                  </option>
                ))}
              </select>
            </span>
          </div>
          <p className="gw-hint">
            {draft.ownerMode === "project"
              ? "项目词只在这个项目的会里用来纠错和识别项目"
              : draft.ownerMode === "custom"
                ? "这是整理前的旧分组，建议改成公共或挂到项目"
                : "所有会都会用到的写法"}
          </p>
        </div>

        {conflict && (
          <div className="gw-alert" role="alert">
            <p>
              『{conflict.term}』已在 {conflictWhere(conflict)}
              {conflict.aliases.length > 0 && `（错写：${conflict.aliases.join("、")}）`}
            </p>
            <div className="gw-alert__acts">
              {conflictInOtherProject && (
                <button className="gw-btn gw-btn--sm" disabled={saving} onClick={() => mergeInto(true)} type="button">
                  改成公共词并合并错写{isEdit && "（删掉这条）"}
                </button>
              )}
              <button className="gw-btn gw-btn--sm gw-btn--pri" disabled={saving} onClick={() => mergeInto(false)} type="button">
                {conflictInOtherProject ? `加到 ${conflict.project_name} 那条` : "把新错写加到那条"}
                {isEdit && "（删掉这条）"}
              </button>
            </div>
          </div>
        )}

        {error && (
          <p className="gw-error" role="alert">
            {error}
          </p>
        )}

        {isEdit && <TermMeetings apiClient={apiClient} onOpenMeeting={onOpenMeeting} termId={term.id} />}
        {isEdit && (
          <p className="gw-meta">
            {term.source === "manual" ? "手动添加" : "记入"} · {formatFullDate(term.created_at)}
          </p>
        )}
      </div>

      {!readOnly && (
        <div className="gw-dfoot">
          <span className="gw-dfoot__st">{footerStatus}</span>
          <span className="gw-dfoot__sp" />
          {(dirty || !isEdit) && (
            <button
              className="gw-btn"
              disabled={saving}
              onClick={() => (isEdit ? onReset() : onCancelNew())}
              type="button"
            >
              取消 <kbd>Esc</kbd>
            </button>
          )}
          <button
            className="gw-btn gw-btn--pri"
            disabled={(isEdit && !dirty) || !canSave || conflict !== null}
            onClick={handleSubmit}
            type="button"
          >
            {saving ? "保存中…" : isEdit ? "保存修改" : "加入词典"}
          </button>
        </div>
      )}
    </>
  );
}
