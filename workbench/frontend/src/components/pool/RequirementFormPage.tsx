import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";

import { ApiError, type ApiClient } from "../../api";
import { formatDurationText, formatMonthDayClock } from "../../format";
import type {
  CandidateDetail,
  MaterialFolderStat,
  PoolItem,
  Project,
  RequirementDetail,
  RequirementPriority,
  RequirementSource,
  RequirementStatus,
  TitleConflict,
} from "../../types";
import { MaterialFolderPickerModal } from "../MaterialFolderPickerModal";
import { REQUIREMENT_PRIORITIES, REQUIREMENT_STATUS_LABELS } from "../RequirementBadges";
import { anchorLabel, PosterCard } from "./PosterCard";
import { PosterWaveform } from "./PosterWaveform";
import { SourcePickerDialog, type SourceDraft } from "./SourcePickerDialog";
import "./RequirementFormPage.css";

export const SUMMARY_MAX = 70;

export type RequirementFormResult = {
  kind: "claimed" | "merged" | "created" | "edited";
  requirement: RequirementDetail;
};

/** 从逐字稿选句进来（R01-10、S10）：来源带上，所属项目默认取会议归属 */
export interface RequirementPrefill {
  source: SourceDraft;
  projectId: string | null;
}

interface RequirementFormPageProps {
  apiClient: ApiClient;
  /** claim：认领候选（S02）；create：需求池里新建或逐字稿选句（S09、S10）；edit：修改需求（S11） */
  mode: "claim" | "create" | "edit";
  candidateId?: string;
  requirementId?: string;
  prefill?: RequirementPrefill;
  projects: Project[];
  canPickFolders: boolean;
  onCancel: () => void;
  onDone: (result: RequirementFormResult) => void;
  onOpenProject?: (projectId: string) => void;
  /** 候选已经被认领、合并时，提示里的［打开那条需求］ */
  onOpenRequirement?: (requirementId: string) => void;
}

/** 说明按码点数，和后端的 70 字一致（一个汉字、一个常见表情各算一个；组合表情按它的码点数算） */
export function summaryLength(value: string): number {
  return [...value.trim()].length;
}

/** 项目下拉：排了座次的按名次在前，其余按名字 */
function orderedProjects(projects: Project[]): Project[] {
  return [...projects].sort((left, right) => {
    const a = left.seat ?? Number.POSITIVE_INFINITY;
    const b = right.seat ?? Number.POSITIVE_INFINITY;
    if (a !== b) return a - b;
    return left.name.localeCompare(right.name, "zh-CN");
  });
}

const MERGEABLE = new Set(["active", "shelved"]);
// 修改需求时状态只在这三态之间切换，不能改回待认领（R04-5）
const EDIT_STATUSES: RequirementStatus[] = ["active", "done", "shelved"];

function sourceKey(source: Pick<SourceDraft, "meeting_id" | "quote" | "anchor_ms"> | null | undefined): string {
  return source ? `${source.meeting_id}|${source.quote}|${source.anchor_ms ?? ""}` : "";
}

function draftOf(source: RequirementSource): SourceDraft {
  const { id: _id, kind: _kind, via_candidate_title: _via, ...draft } = source;
  return draft;
}

/** 来源面板：会名、时间、原话和波形；children 是面板底下的操作或补充说明 */
function SourcePanel({ source, children }: { source: SourceDraft; children?: ReactNode }) {
  return (
    <div className="form-source">
      <div className="form-source__head">
        <strong>{source.meeting_title}</strong>
        <span>
          {formatMonthDayClock(source.recording_date)}
          {source.duration_ms ? ` · ${formatDurationText(source.duration_ms)}` : ""}
        </span>
      </div>
      {(source.anchor_ms !== null || source.quote) && (
        <p className="form-source__quote">
          {source.anchor_ms !== null && <span className="form-source__anchor">▶ {anchorLabel(source.anchor_ms)}</span>}
          {source.quote && <span>「{source.quote}」</span>}
        </p>
      )}
      <PosterWaveform
        artifactId={source.audio_artifact_id}
        bars={260}
        height={34}
        label={`${source.meeting_title} 的录音波形`}
        markers={source.anchor_ms !== null ? [{ atMs: source.anchor_ms }] : []}
      />
      {children}
    </div>
  );
}

/** 修改页来源底下的一行：合并进来的原话（R01 合并），只看不改 */
function mergedNote(merged: RequirementSource[], originMeetingId: string | null): string {
  const label = (item: RequirementSource) =>
    [
      item.via_candidate_title ? `候选「${item.via_candidate_title}」` : null,
      item.meeting_id !== originMeetingId ? item.meeting_title : null,
      item.anchor_ms !== null ? anchorLabel(item.anchor_ms) : null,
    ]
      .filter(Boolean)
      .join(" · ");
  if (merged.length === 1) {
    const [item] = merged;
    return item.via_candidate_title
      ? `另有 1 句原话合并自${label(item)}`
      : `另有 1 句原话合并进来 · ${label(item)}`;
  }
  return `另有 ${merged.length} 句原话合并进来：${merged.map(label).join("；")}`;
}

/**
 * 新增、认领、修改需求的二级页（R04-1：不是弹窗；保存或放弃后回到进入前的页面）。
 * 左边表单、右边「墙上预览」，底部一条固定的操作栏。认领时需求名和说明由 AI 预填，
 * 来源出自会议纪要、不可改；撞上同项目的同名需求时不新建，提示改名或改为合并到那一条（R01 异常）。
 * 新增、修改时来源可以选、换、清空（R04-3、R04-5）；修改时状态在进行中、已完成、已搁置之间切换。
 */
export function RequirementFormPage({
  apiClient,
  mode,
  candidateId,
  requirementId,
  prefill,
  projects,
  canPickFolders,
  onCancel,
  onDone,
  onOpenProject,
  onOpenRequirement,
}: RequirementFormPageProps) {
  const claiming = mode === "claim";
  const editing = mode === "edit";
  const [candidate, setCandidate] = useState<CandidateDetail | null>(null);
  const [requirement, setRequirement] = useState<RequirementDetail | null>(null);
  const [loadError, setLoadError] = useState("");
  const [title, setTitle] = useState("");
  const [summary, setSummary] = useState("");
  const [projectId, setProjectId] = useState(prefill?.projectId ?? "");
  const [priority, setPriority] = useState<RequirementPriority>("P2");
  const [status, setStatus] = useState<RequirementStatus>("active");
  // 新增、修改时的来源（认领时来源取候选的、不可改）
  const [source, setSource] = useState<SourceDraft | null>(prefill?.source ?? null);
  const [pickingSource, setPickingSource] = useState(false);
  const [folders, setFolders] = useState<MaterialFolderStat[]>([]);
  const [pickerOpen, setPickerOpen] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [conflict, setConflict] = useState<TitleConflict | null>(null);
  const titleRef = useRef<HTMLInputElement>(null);
  const conflictRef = useRef<HTMLDivElement>(null);

  // 从滚动过的需求池点进来时页面停在半截：二级页从页头开始看
  useEffect(() => {
    document.documentElement.scrollTop = 0;
    document.body.scrollTop = 0;
  }, []);
  const prevProjectRef = useRef(projectId);

  useEffect(() => {
    if (!claiming || !candidateId) return;
    let active = true;
    void apiClient
      .requirementCandidate(candidateId)
      .then((detail) => {
        if (!active) return;
        setCandidate(detail);
        setTitle(detail.title);
        setSummary(detail.summary);
        setProjectId(detail.project_id ?? "");
        prevProjectRef.current = detail.project_id ?? "";
      })
      .catch((err: unknown) => {
        if (active) setLoadError(err instanceof Error ? err.message : "候选读取失败");
      });
    return () => {
      active = false;
    };
  }, [apiClient, candidateId, claiming]);

  useEffect(() => {
    if (!editing || !requirementId) return;
    let active = true;
    void apiClient
      .requirement(requirementId)
      .then((detail) => {
        if (!active) return;
        setRequirement(detail);
        setTitle(detail.title);
        setSummary(detail.summary ?? "");
        setProjectId(detail.project_id);
        prevProjectRef.current = detail.project_id;
        setPriority(detail.priority);
        setStatus(detail.status);
        setFolders(detail.folders);
        setSource(detail.source ? draftOf(detail.source) : null);
      })
      .catch((err: unknown) => {
        if (active) setLoadError(err instanceof Error ? err.message : "需求读取失败");
      });
    return () => {
      active = false;
    };
  }, [apiClient, editing, requirementId]);

  // 撞名的提示在需求名底下：窗口矮、页面滚过时滚到看得见的地方
  useEffect(() => {
    if (conflict) conflictRef.current?.scrollIntoView?.({ block: "nearest", behavior: "smooth" });
  }, [conflict]);

  // 换了所属项目，原来那个项目根目录下的文件夹不再合法（D6），清空重选；撞名的提示也作废
  useEffect(() => {
    if (prevProjectRef.current === projectId) return;
    prevProjectRef.current = projectId;
    setFolders([]);
    setConflict(null);
  }, [projectId]);

  const sortedProjects = useMemo(() => orderedProjects(projects), [projects]);
  const project = projects.find((item) => item.id === projectId) ?? null;
  const chars = summaryLength(summary);
  const handled = claiming && candidate !== null && candidate.status !== "pending";
  const ready = claiming ? candidate !== null : editing ? requirement !== null : true;
  const canSave = ready && !handled && title.trim() !== "" && projectId !== "" && chars <= SUMMARY_MAX && !saving;

  // 墙上预览：认领看候选的来源和数，修改看需求的，新增看表单上选的来源
  const previewSource: RequirementSource | null = claiming
    ? (candidate?.source ?? null)
    : source && { ...source, id: 0, kind: "origin", via_candidate_title: null };
  const meetingIds = new Set(
    [...(requirement?.meetings.map((meeting) => meeting.id) ?? []), source?.meeting_id].filter(Boolean),
  );
  const preview: PoolItem = {
    kind: "requirement",
    id: "preview",
    title: title.trim(),
    summary: summary.trim(),
    status: editing ? status : "active",
    priority,
    project_id: projectId || null,
    project_name: project?.name ?? null,
    project_color: project?.color ?? null,
    project_seat: project?.seat ?? null,
    open_task_count: candidate?.open_task_count ?? requirement?.open_task_count ?? 0,
    meeting_count: candidate?.meeting_count ?? meetingIds.size,
    folder_count: folders.length,
    latest_meeting_date: candidate?.latest_meeting_date ?? previewSource?.recording_date ?? null,
    source: previewSource,
    follow_up_count: candidate?.follow_up_count ?? Math.max(meetingIds.size - 1, 0),
    similar_requirement: null,
    default_action: null,
    can_merge: false,
    created_at: candidate?.created_at ?? requirement?.created_at ?? new Date().toISOString(),
    updated_at: candidate?.updated_at ?? requirement?.updated_at ?? new Date().toISOString(),
  };

  const submit = async () => {
    if (!canSave) return;
    setSaving(true);
    setError("");
    setConflict(null);
    const body = {
      title: title.trim(),
      summary: summary.trim(),
      project_id: projectId,
      priority,
      folder_paths: canPickFolders ? folders.map((folder) => folder.path) : [],
    };
    const sourceInput = source && { meeting_id: source.meeting_id, quote: source.quote, anchor_ms: source.anchor_ms };
    try {
      if (claiming && candidateId) {
        onDone({ kind: "claimed", requirement: await apiClient.claimCandidate(candidateId, body) });
      } else if (editing && requirementId) {
        // 来源动过才传：换成别的会、换一句、清空（R04-5）；没动不传，不碰合并进来的原话
        const sourceChanged = sourceKey(source) !== sourceKey(requirement?.source);
        const updated = await apiClient.updateRequirement(requirementId, {
          title: body.title,
          summary: body.summary,
          project_id: body.project_id,
          priority: body.priority,
          status,
          ...(canPickFolders ? { folder_paths: body.folder_paths } : {}),
          ...(sourceChanged ? { source: sourceInput } : {}),
        });
        onDone({ kind: "edited", requirement: updated });
      } else {
        onDone({
          kind: "created",
          requirement: await apiClient.createRequirement({ ...body, ...(sourceInput ? { source: sourceInput } : {}) }),
        });
      }
    } catch (err) {
      const data = err instanceof ApiError ? (err.data as TitleConflict | null) : null;
      if (err instanceof ApiError && err.status === 409 && data?.existing) {
        setConflict(data);
      } else if (claiming && err instanceof ApiError && err.status === 404) {
        setError("这条候选已经不在了（纪要重新抽取时换掉了），回需求池看看新的候选");
      } else {
        setError(err instanceof Error ? err.message : "保存失败，请稍后重试");
        // 候选在别处已经认领、合并或丢掉：重读一遍，页上显示它的状态，表单不能再提交
        if (claiming && candidateId && err instanceof ApiError && err.status === 409) {
          void apiClient
            .requirementCandidate(candidateId)
            .then(setCandidate)
            .catch(() => undefined);
        }
      }
      setSaving(false);
    }
  };

  const mergeIntoExisting = async () => {
    const existing = conflict?.existing;
    if (!candidateId || !existing) return;
    setSaving(true);
    setError("");
    try {
      const requirement = await apiClient.mergeCandidate(candidateId, existing.id, projectId);
      onDone({ kind: "merged", requirement });
    } catch (err) {
      setError(err instanceof Error ? err.message : "合并失败，请稍后重试");
      setSaving(false);
    }
  };

  const candidateSource = candidate?.source ?? null;
  const extraSources = (candidate?.sources ?? []).filter((item) => item.id !== candidateSource?.id);
  const mergedSources = (requirement?.sources ?? []).filter((item) => item.kind === "merged");
  const heading = claiming ? "认领候选" : editing ? "修改需求" : "新增需求";
  const lead = claiming
    ? "AI 从会议纪要里抽出来的，改好再认领，认领后挂上「进行中」的墙。"
    : editing
      ? "改完保存，墙上的海报会跟着变。"
      : prefill
        ? "来源已从逐字稿带入，补上需求名就能建。"
        : "从会上听到的一句话开始，或者直接写下要做的事。";

  return (
    <section className="page-content form-page">
      <header className="form-page__head">
        <p className="form-page__crumb">
          <span>
            {prefill
              ? `录音档案 / ${prefill.source.meeting_title} /`
              : editing && requirement
                ? `需求池 / ${requirement.title} /`
                : "需求池 /"}
          </span>{" "}
          {heading}
        </p>
        <div className="form-page__title">
          <h1>{heading}</h1>
          <p>{lead}</p>
        </div>
      </header>

      {loadError && (
        <p className="form-page__alert" role="alert">
          {loadError}
        </p>
      )}
      {handled && candidate && (
        <p className="form-page__alert" role="alert">
          这条候选已经{candidate.status === "claimed" ? "认领" : candidate.status === "merged" ? "合并" : "丢掉"}了。
          {candidate.requirement_id && onOpenRequirement && (
            <button onClick={() => onOpenRequirement(candidate.requirement_id!)} type="button">
              打开那条需求
            </button>
          )}
        </p>
      )}

      <div className="form-page__body">
        <form
          className="form-card"
          onSubmit={(event) => {
            event.preventDefault();
            void submit();
          }}
        >
          <label className="form-field">
            <span className="form-field__label">
              需求名
              {claiming && <em className="form-field__ai">AI 预填</em>}
            </span>
            <input
              disabled={saving || !ready || handled}
              maxLength={200}
              onChange={(event) => {
                setTitle(event.target.value);
                setConflict(null);
              }}
              placeholder="给这条需求起个名字"
              ref={titleRef}
              value={title}
            />
          </label>

          {conflict?.existing && (
            <div className="form-conflict" ref={conflictRef} role="alert">
              <p>
                「{project?.name ?? "这个项目"}」里已有同名需求「{conflict.existing.title}」（
                {REQUIREMENT_STATUS_LABELS[conflict.existing.status]}），不会重复新建。
              </p>
              <div>
                <button onClick={() => titleRef.current?.focus()} type="button">
                  改个名字
                </button>
                {claiming && MERGEABLE.has(conflict.existing.status) ? (
                  <button className="is-primary" disabled={saving} onClick={() => void mergeIntoExisting()} type="button">
                    改为合并到这条需求
                  </button>
                ) : (
                  claiming && <span>它已完成，不能合并，请改个名字</span>
                )}
              </div>
            </div>
          )}

          <label className="form-field">
            <span className="form-field__label">
              说明
              {claiming ? <em className="form-field__ai">AI 预填</em> : <em className="form-field__optional">选填</em>}
            </span>
            <span className="form-field__textarea">
              <textarea
                disabled={saving || !ready || handled}
                onChange={(event) => setSummary(event.target.value)}
                placeholder="一两句话说清要做什么，40～70 字"
                rows={3}
                value={summary}
              />
              <span className={`form-field__counter ${chars > SUMMARY_MAX ? "is-over" : ""}`}>
                {chars} / {SUMMARY_MAX}
              </span>
            </span>
          </label>

          <div className={`form-field__row ${editing ? "form-field__row--three" : ""}`}>
            <label className="form-field">
              <span className="form-field__label form-field__label--split">
                所属项目
                {prefill && <small>随会议归属，可改</small>}
              </span>
              <select
                disabled={saving || !ready || handled}
                onChange={(event) => setProjectId(event.target.value)}
                value={projectId}
              >
                <option value="">选择项目</option>
                {sortedProjects.map((item) => (
                  <option key={item.id} value={item.id}>
                    {item.seat ? `${item.seat}  ${item.name}` : item.name}
                  </option>
                ))}
              </select>
            </label>
            <div className="form-field">
              <span className="form-field__label">优先级</span>
              <div aria-label="优先级" className="form-segmented" role="group">
                {REQUIREMENT_PRIORITIES.map((value) => (
                  <button
                    aria-pressed={priority === value}
                    disabled={saving || !ready || handled}
                    key={value}
                    onClick={() => setPriority(value)}
                    type="button"
                  >
                    {value}
                  </button>
                ))}
              </div>
            </div>
            {editing && (
              <div className="form-field">
                <span className="form-field__label">状态</span>
                <div aria-label="状态" className="form-segmented" role="group">
                  {EDIT_STATUSES.map((value) => (
                    <button
                      aria-pressed={status === value}
                      disabled={saving || !ready}
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
          </div>

          {claiming && (
            <div className="form-field">
              <span className="form-field__label form-field__label--split">
                来源
                <small>出自会议纪要，不可改</small>
              </span>
              {candidateSource ? (
                <SourcePanel source={candidateSource}>
                  {extraSources.length > 0 && (
                    <p className="form-source__more">另有 {extraSources.length} 句原话出自其他会议</p>
                  )}
                </SourcePanel>
              ) : (
                <p className="form-source form-source--empty">
                  {loadError
                    ? "读不到这条候选"
                    : !candidate
                      ? "正在读取…"
                      : handled
                        ? "来源已经跟着候选挂到需求上了"
                        : "这条候选没有来源"}
                </p>
              )}
            </div>
          )}

          {!claiming && (
            <div className="form-field">
              <span className="form-field__label form-field__label--split">
                <span>
                  来源 <em className="form-field__optional">选填</em>
                </span>
                <small>{prefill ? "来自逐字稿选句" : "选定的会同时加进关联会议"}</small>
              </span>
              {source ? (
                <SourcePanel source={source}>
                  <div className="form-source__actions">
                    <button disabled={saving || !ready} onClick={() => setPickingSource(true)} type="button">
                      换一个
                    </button>
                    <button disabled={saving || !ready} onClick={() => setSource(null)} type="button">
                      清空来源
                    </button>
                  </div>
                </SourcePanel>
              ) : (
                <button
                  className="form-folders__pick"
                  disabled={saving || !ready}
                  onClick={() => setPickingSource(true)}
                  type="button"
                >
                  ＋ 选来源会议，再挑会上原话
                </button>
              )}
              {mergedSources.length > 0 && (
                <p className="form-source__more">{mergedNote(mergedSources, source?.meeting_id ?? null)}</p>
              )}
            </div>
          )}

          {canPickFolders && (
            <div className="form-field">
              <span className="form-field__label">
                材料文件夹 <em className="form-field__optional">选填</em>
              </span>
              {folders.length > 0 && (
                <ul className="form-folders">
                  {folders.map((folder) => (
                    <li key={folder.path}>
                      <strong>{folder.name}</strong>
                      <span>{folder.path}</span>
                      <button
                        disabled={saving}
                        onClick={() => setFolders((current) => current.filter((item) => item.path !== folder.path))}
                        type="button"
                      >
                        移除
                      </button>
                    </li>
                  ))}
                </ul>
              )}
              <button
                className="form-folders__pick"
                disabled={saving || !project || handled}
                onClick={() => setPickerOpen(true)}
                type="button"
              >
                ＋ {folders.length > 0 ? "再选一个文件夹" : "选择文件夹"}
              </button>
            </div>
          )}
        </form>

        <aside className="form-preview">
          <p className="form-preview__label">墙上预览</p>
          <PosterCard canWrite={false} item={preview} preview />
        </aside>
      </div>

      <footer className="form-actions">
        {/* 失败原因写在固定的操作栏里：表单长、窗口矮的时候也看得见 */}
        {error && (
          <p className="form-actions__error" role="alert">
            {error}
          </p>
        )}
        <button className="form-actions__cancel" disabled={saving} onClick={onCancel} type="button">
          取消
        </button>
        <button className="form-actions__submit" disabled={!canSave} onClick={() => void submit()} type="button">
          {saving ? "保存中…" : claiming ? "认领" : editing ? "保存" : "创建"}
        </button>
      </footer>

      {pickingSource && (
        <SourcePickerDialog
          apiClient={apiClient}
          onClose={() => setPickingSource(false)}
          onPicked={(picked) => {
            setSource(picked);
            setPickingSource(false);
          }}
          projectId={projectId || null}
          projectName={project?.name ?? null}
        />
      )}

      {pickerOpen && project && (
        <MaterialFolderPickerModal
          apiClient={apiClient}
          onCancel={() => setPickerOpen(false)}
          onConfirm={(picked) => {
            setFolders(picked);
            setPickerOpen(false);
          }}
          onOpenProject={(pid) => {
            setPickerOpen(false);
            onOpenProject?.(pid);
          }}
          projectId={project.id}
          projectName={project.name}
          selectedFolders={folders}
        />
      )}
    </section>
  );
}
