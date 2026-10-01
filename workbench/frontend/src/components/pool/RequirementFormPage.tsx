import { useEffect, useMemo, useRef, useState } from "react";

import { ApiError, type ApiClient } from "../../api";
import { formatDurationText, formatMonthDayClock } from "../../format";
import type {
  CandidateDetail,
  MaterialFolderStat,
  PoolItem,
  Project,
  RequirementDetail,
  RequirementPriority,
  TitleConflict,
} from "../../types";
import { MaterialFolderPickerModal } from "../MaterialFolderPickerModal";
import { REQUIREMENT_PRIORITIES, REQUIREMENT_STATUS_LABELS } from "../RequirementBadges";
import { anchorLabel, PosterCard } from "./PosterCard";
import { PosterWaveform } from "./PosterWaveform";
import "./RequirementFormPage.css";

export const SUMMARY_MAX = 70;

export type RequirementFormResult = {
  kind: "claimed" | "merged" | "created";
  requirement: RequirementDetail;
};

interface RequirementFormPageProps {
  apiClient: ApiClient;
  /** claim：认领候选（S02）；create：需求池里新建（S09） */
  mode: "claim" | "create";
  candidateId?: string;
  projects: Project[];
  canPickFolders: boolean;
  onCancel: () => void;
  onDone: (result: RequirementFormResult) => void;
  onOpenProject?: (projectId: string) => void;
}

/** 说明按字数算（一个表情也是一个字），和后端的 70 字一致 */
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

/**
 * 新增、认领需求的二级页（R04-1：不是弹窗；保存或放弃后回到进入前的页面）。
 * 左边表单、右边「墙上预览」，底部一条固定的操作栏。认领时需求名和说明由 AI 预填，
 * 来源出自会议纪要、不可改；撞上同项目的同名需求时不新建，提示改名或改为合并到那一条（R01 异常）。
 */
export function RequirementFormPage({
  apiClient,
  mode,
  candidateId,
  projects,
  canPickFolders,
  onCancel,
  onDone,
  onOpenProject,
}: RequirementFormPageProps) {
  const claiming = mode === "claim";
  const [candidate, setCandidate] = useState<CandidateDetail | null>(null);
  const [loadError, setLoadError] = useState("");
  const [title, setTitle] = useState("");
  const [summary, setSummary] = useState("");
  const [projectId, setProjectId] = useState("");
  const [priority, setPriority] = useState<RequirementPriority>("P2");
  const [folders, setFolders] = useState<MaterialFolderStat[]>([]);
  const [pickerOpen, setPickerOpen] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [conflict, setConflict] = useState<TitleConflict | null>(null);
  const titleRef = useRef<HTMLInputElement>(null);
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
  const ready = !claiming || candidate !== null;
  const canSave = ready && !handled && title.trim() !== "" && projectId !== "" && chars <= SUMMARY_MAX && !saving;

  const preview: PoolItem = {
    kind: "requirement",
    id: "preview",
    title: title.trim(),
    summary: summary.trim(),
    status: "active",
    priority,
    project_id: projectId || null,
    project_name: project?.name ?? null,
    project_color: project?.color ?? null,
    project_seat: project?.seat ?? null,
    open_task_count: candidate?.open_task_count ?? 0,
    meeting_count: candidate?.meeting_count ?? 0,
    folder_count: folders.length,
    latest_meeting_date: candidate?.latest_meeting_date ?? null,
    source: candidate?.source ?? null,
    follow_up_count: candidate?.follow_up_count ?? 0,
    similar_requirement: null,
    default_action: null,
    can_merge: false,
    created_at: candidate?.created_at ?? new Date().toISOString(),
    updated_at: candidate?.updated_at ?? new Date().toISOString(),
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
    try {
      if (claiming && candidateId) {
        onDone({ kind: "claimed", requirement: await apiClient.claimCandidate(candidateId, body) });
      } else {
        onDone({ kind: "created", requirement: await apiClient.createRequirement(body) });
      }
    } catch (err) {
      const data = err instanceof ApiError ? (err.data as TitleConflict | null) : null;
      if (err instanceof ApiError && err.status === 409 && data?.existing) {
        setConflict(data);
      } else {
        setError(err instanceof Error ? err.message : "保存失败，请稍后重试");
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

  const source = candidate?.source ?? null;
  const extraSources = (candidate?.sources ?? []).filter((item) => item.id !== source?.id);

  return (
    <section className="page-content form-page">
      <header className="form-page__head">
        <p className="form-page__crumb">
          <span>需求池 /</span> {claiming ? "认领候选" : "新增需求"}
        </p>
        <div className="form-page__title">
          <h1>{claiming ? "认领候选" : "新增需求"}</h1>
          <p>
            {claiming
              ? "AI 从会议纪要里抽出来的，改好再认领，认领后挂上「进行中」的墙。"
              : "从会上听到的一句话开始，或者直接写下要做的事。"}
          </p>
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
            <div className="form-conflict" role="alert">
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

          <div className="form-field__row">
            <label className="form-field">
              <span className="form-field__label">所属项目</span>
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
          </div>

          {claiming && (
            <div className="form-field">
              <span className="form-field__label form-field__label--split">
                来源
                <small>出自会议纪要，不可改</small>
              </span>
              {source ? (
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
                  {extraSources.length > 0 && (
                    <p className="form-source__more">另有 {extraSources.length} 句原话出自其他会议</p>
                  )}
                </div>
              ) : (
                <p className="form-source form-source--empty">{candidate ? "这条候选没有来源" : "正在读取…"}</p>
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

      {error && (
        <p className="form-page__alert" role="alert">
          {error}
        </p>
      )}

      <footer className="form-actions">
        <button className="form-actions__cancel" disabled={saving} onClick={onCancel} type="button">
          取消
        </button>
        <button className="form-actions__submit" disabled={!canSave} onClick={() => void submit()} type="button">
          {saving ? "保存中…" : claiming ? "认领" : "创建"}
        </button>
      </footer>

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
