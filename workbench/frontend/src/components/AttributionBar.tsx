import { useState } from "react";

import { similarProjectFrom, type ApiClient } from "../api";
import { reassignNote } from "../cardCopy";
import type {
  AttributionEvidence,
  MeetingAttribution,
  MeetingCard,
  MeetingDetail,
  Project,
  SimilarProjectSuggestion,
} from "../types";
import "./AttributionBar.css";

/** 归属条上的操作改动了会议的归属：带回新的归属对象，以及（能拿到时）会议的项目字段。 */
export interface AttributionChange {
  attribution: MeetingAttribution;
  project?: { id: string | null; name: string | null; color: string | null; origin: "manual" | "ai" | null };
  /** 改归属、撤销之后卡片的新状态（接口带回时） */
  card?: MeetingCard;
}

interface AttributionBarProps {
  apiClient: ApiClient;
  meetingId: string;
  attribution: MeetingAttribution;
  projects: Project[];
  /** 检查器里有没保存的项目改动时，归属条的按钮都置灰并说明原因 */
  lockedReason?: string;
  onSeek?: (ms: number) => void;
  onChange: (change: AttributionChange) => void;
  /** 结果提示；undoUntil 有值时提示里带［撤销］ */
  onNotice: (message: string, undoUntil?: string) => void;
  onProjectsChanged?: () => Promise<void> | void;
}

const DEFAULT_PROJECT_COLOR = "#667085";
const RECENT_CHANGE_DAYS = 30;

function formatClock(ms: number) {
  const total = Math.max(0, Math.floor(ms / 1000));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const seconds = total % 60;
  const pad = (value: number) => String(value).padStart(2, "0");
  return hours ? `${hours}:${pad(minutes)}:${pad(seconds)}` : `${pad(minutes)}:${pad(seconds)}`;
}

function formatMonthDay(iso: string) {
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? "" : `${date.getMonth() + 1}/${date.getDate()}`;
}

function changeFromDetail(
  detail: Pick<MeetingDetail, "project_id" | "project_name" | "project_color" | "project_origin" | "attribution" | "card">,
): AttributionChange | null {
  if (!detail.attribution) return null;
  return {
    attribution: detail.attribution,
    project: {
      id: detail.project_id ?? null,
      name: detail.project_name ?? null,
      color: detail.project_color ?? null,
      origin: detail.project_origin ?? null,
    },
    card: detail.card,
  };
}

function PickProject({
  disabled,
  exclude,
  label,
  onPick,
  projects,
}: {
  disabled: boolean;
  exclude: (string | null)[];
  label: string;
  onPick: (projectId: string) => void;
  projects: Project[];
}) {
  return (
    <select
      aria-label={label}
      className="attribution-bar__pick"
      disabled={disabled}
      onChange={(event) => {
        if (event.target.value) onPick(event.target.value);
        event.target.value = "";
      }}
      value=""
    >
      <option value="">{label} ▾</option>
      {projects
        .filter((project) => !exclude.includes(project.id))
        .map((project) => (
          <option key={project.id} value={project.id}>{project.name}</option>
        ))}
    </select>
  );
}

function EvidenceDetail({ evidence, onSeek }: { evidence: AttributionEvidence; onSeek?: (ms: number) => void }) {
  const where = evidence.where;
  const parts = where
    ? [
        where.title ? `标题 ${where.title} 次` : "",
        where.transcript ? `逐字稿 ${where.transcript} 次` : "",
        where.minutes ? `纪要 ${where.minutes} 次` : "",
      ].filter(Boolean)
    : [];
  return (
    <li>
      <span className="attribution-bar__cue">「{evidence.cue}」</span>
      {parts.length > 0 && <span className="attribution-bar__where">{parts.join(" · ")}</span>}
      {(evidence.anchors_ms ?? []).map((ms) => (
        <button
          aria-label={`跳到 ${formatClock(ms)}`}
          className="attribution-bar__anchor"
          key={ms}
          onClick={() => onSeek?.(ms)}
          type="button"
        >
          {formatClock(ms)}
        </button>
      ))}
    </li>
  );
}

export function AttributionBar({
  apiClient,
  meetingId,
  attribution,
  projects,
  lockedReason,
  onSeek,
  onChange,
  onNotice,
  onProjectsChanged,
}: AttributionBarProps) {
  const [busy, setBusy] = useState(false);
  const [expanded, setExpanded] = useState(false);
  const [changing, setChanging] = useState(false);
  const [suggestion, setSuggestion] = useState<SimilarProjectSuggestion | null>(null);
  const disabled = busy || Boolean(lockedReason);
  const projectName = (projectId: string | null) =>
    projects.find((project) => project.id === projectId)?.name ?? "";

  const run = async (action: () => Promise<void>) => {
    setBusy(true);
    try {
      await action();
    } catch (error) {
      onNotice(error instanceof Error ? error.message : "操作失败");
    } finally {
      setBusy(false);
    }
  };

  const assign = (projectId: string) =>
    run(async () => {
      const detail = await apiClient.updateMeeting(meetingId, { project_id: projectId });
      const change = changeFromDetail(detail);
      if (change) onChange(change);
      setChanging(false);
      setSuggestion(null);
      const target = projectId && projectId !== "__ai__" ? projectName(projectId) || detail.project_name || "" : "";
      const effects = detail.effects;
      const note = effects ? reassignNote(effects.tasks_moved, effects.tasks_left.length, effects.card) : "";
      const head =
        projectId === "__ai__" ? "已交给 AI 重新判断" : target ? `已改到 ${target}` : "已标为不归项目";
      onNotice(note ? `${head}：${note}` : head, effects?.undo_until);
      await onProjectsChanged?.();
    });

  const confirm = () =>
    run(async () => {
      const next = await apiClient.confirmMeetingProject(meetingId);
      onChange({ attribution: next });
      onNotice(`已确认归到 ${projectName(next.project_id)}`);
    });

  const undo = () =>
    run(async () => {
      const detail = await apiClient.undoMeetingProject(meetingId);
      const change = changeFromDetail(detail);
      if (change) onChange(change);
      onNotice(detail.effects?.card?.action === "moved" ? "已撤销刚才的改动，会议卡片也搬回去了" : "已撤销刚才的改动");
      await onProjectsChanged?.();
    });

  const buildProject = (name: string, force = false) =>
    run(async () => {
      try {
        const created = await apiClient.createProjectWith({
          name,
          color: DEFAULT_PROJECT_COLOR,
          meeting_ids: [meetingId],
          force,
        });
        setSuggestion(null);
        const detail = await apiClient.meeting(meetingId);
        const change = changeFromDetail(detail);
        if (change) onChange(change);
        onNotice(`已建成项目「${created.name}」，这场会归进去了`);
        await onProjectsChanged?.();
      } catch (error) {
        const similar = similarProjectFrom(error);
        if (!similar) throw error;
        setSuggestion(similar);
      }
    });

  const ignoreName = (name: string) =>
    run(async () => {
      await apiClient.ignoreProjectName(name);
      onChange({ attribution: { ...attribution, state: "none", new_project_name: null } });
      onNotice(`以后不再把「${name}」当成新项目`);
    });

  const moveLeftTasks = (projectId: string) =>
    run(async () => {
      const left = attribution.reassigned_from?.tasks_left ?? [];
      for (const task of left) {
        await apiClient.updateTask(task.id, { project_id: projectId, requirement_id: null });
      }
      onChange({
        attribution: {
          ...attribution,
          reassigned_from: attribution.reassigned_from && { ...attribution.reassigned_from, tasks_left: [] },
        },
      });
      onNotice(`${left.length} 条任务也移过来了，已移出原来的需求`);
    });

  const dismissCue = (termId: string, term: string) =>
    run(async () => {
      await apiClient.updateGlossaryTerm(termId, { is_cue: false });
      onChange({
        attribution: {
          ...attribution,
          reassigned_from: attribution.reassigned_from && { ...attribution.reassigned_from, cue_hint: null },
        },
      });
      onNotice(`以后不再用「${term}」判断项目`);
    });

  const lockNote = lockedReason ? <span className="attribution-bar__lock">{lockedReason}</span> : null;
  const changeControls = (exclude: (string | null)[]) => (
    <>
      <PickProject disabled={disabled} exclude={exclude} label="选别的" onPick={(id) => void assign(id)} projects={projects} />
      <button className="text-button" disabled={disabled} onClick={() => void assign("")} type="button">不归项目</button>
    </>
  );

  const { state } = attribution;

  if (state === "auto") {
    const literal = attribution.evidence.filter(
      (entry) => entry.kind === "literal" && entry.project_id === attribution.project_id,
    );
    const llm = attribution.evidence.find((entry) => entry.kind === "llm");
    const top = literal[0];
    const reason = llm?.reason || attribution.reason;
    return (
      <div className="attribution-bar attribution-bar--auto" role="group" aria-label="项目归属">
        <div className="attribution-bar__row">
          <strong>{projectName(attribution.project_id) || "已归属"}</strong>
          <span className="attribution-bar__tag">自动</span>
          {top ? (
            <button
              aria-expanded={expanded}
              className="attribution-bar__why"
              onClick={() => setExpanded((value) => !value)}
              type="button"
            >
              提到「{top.cue}」{top.count} 次
            </button>
          ) : (
            reason && (
              <button
                aria-expanded={expanded}
                className="attribution-bar__why"
                onClick={() => setExpanded((value) => !value)}
                type="button"
              >
                AI 判断
              </button>
            )
          )}
          <button className="ghost-button" disabled={disabled} onClick={() => void confirm()} type="button">对的</button>
          {changing ? (
            changeControls([attribution.project_id])
          ) : (
            <button className="text-button" disabled={disabled} onClick={() => setChanging(true)} type="button">改</button>
          )}
          {lockNote}
        </div>
        {expanded && (
          <div className="attribution-bar__detail">
            {literal.length > 0 && (
              <ul>
                {literal.map((entry) => (
                  <EvidenceDetail evidence={entry} key={`${entry.source}-${entry.cue}`} onSeek={onSeek} />
                ))}
              </ul>
            )}
            {reason && <p className="attribution-bar__reason">AI：{reason}</p>}
          </div>
        )}
      </div>
    );
  }

  if (state === "needs_review") {
    const candidates = attribution.candidates;
    const question =
      candidates.length >= 2
        ? `${candidates[0].project_name} 还是 ${candidates[1].project_name}？`
        : candidates.length === 1
          ? `像是 ${candidates[0].project_name}？`
          : "这场会属于哪个项目？";
    return (
      <div className="attribution-bar attribution-bar--review" role="group" aria-label="项目归属">
        <div className="attribution-bar__row">
          <strong>{question}</strong>
          {candidates.map((candidate) => (
            <button
              className="ghost-button"
              disabled={disabled}
              key={candidate.project_id}
              onClick={() => void (candidate.current ? confirm() : assign(candidate.project_id))}
              type="button"
            >
              {candidate.project_name}
              {candidate.current ? "（原来的）" : ""}
            </button>
          ))}
          {changeControls(candidates.map((candidate) => candidate.project_id))}
          {lockNote}
        </div>
        {attribution.reason && <p className="attribution-bar__reason">{attribution.reason}</p>}
      </div>
    );
  }

  if (state === "ai_pending" || state === "none") {
    const text =
      state === "none"
        ? "AI 没认出这场会属于哪个项目"
        : attribution.ai_configured
          ? "正在判断属于哪个项目（通常一分钟内）"
          : "没配置 AI，请在这里选项目";
    return (
      <div className="attribution-bar attribution-bar--quiet" role="group" aria-label="项目归属">
        <div className="attribution-bar__row">
          <span>{text}</span>
          <PickProject disabled={disabled} exclude={[]} label="选项目" onPick={(id) => void assign(id)} projects={projects} />
          {lockNote}
        </div>
      </div>
    );
  }

  if (state === "new_project" && attribution.new_project_name) {
    const name = attribution.new_project_name;
    return (
      <div className="attribution-bar attribution-bar--new" role="group" aria-label="项目归属">
        <div className="attribution-bar__row">
          <span>像是一个新项目「{name}」</span>
          <button className="ghost-button" disabled={disabled} onClick={() => void buildProject(name)} type="button">建成项目</button>
          <button className="text-button" disabled={disabled} onClick={() => void ignoreName(name)} type="button">不是新项目</button>
          <PickProject disabled={disabled} exclude={[]} label="选已有项目" onPick={(id) => void assign(id)} projects={projects} />
          {lockNote}
        </div>
        {suggestion && (
          <div className="attribution-bar__row attribution-bar__question">
            <span>
              已有「{suggestion.name}」
              {suggestion.also_names.length > 0 && `（又称 ${suggestion.also_names.slice(0, 3).join("、")}）`}
              ，是不是它？
            </span>
            <button className="ghost-button" disabled={disabled} onClick={() => void assign(suggestion.project_id)} type="button">用它</button>
            <button className="text-button" disabled={disabled} onClick={() => void buildProject(name, true)} type="button">仍然新建</button>
          </div>
        )}
      </div>
    );
  }

  const came = attribution.reassigned_from;
  if (state === "manual" && came) {
    const at = new Date(came.at).getTime();
    const recent = !Number.isNaN(at) && Date.now() - at < RECENT_CHANGE_DAYS * 24 * 3600 * 1000;
    if (!recent) return null;
    const revert = () =>
      came.can_undo ? void undo() : void assign(came.project_id ?? "__ai__");
    const firstLeft = came.tasks_left[0];
    const cueHint = came.cue_hint;
    const fromLabel =
      came.project_name ?? (came.origin_before === "manual" ? "不归项目" : "未归项目");
    return (
      <div className="attribution-bar attribution-bar--changed" role="group" aria-label="项目归属">
        <div className="attribution-bar__row attribution-bar__muted">
          <span>
            由 {fromLabel} 改来 · {formatMonthDay(came.at)}
          </span>
          <button className="text-button" disabled={disabled} onClick={revert} type="button">改回</button>
          {lockNote}
        </div>
        {firstLeft && came.project_name && attribution.project_id && (
          <div className="attribution-bar__row attribution-bar__muted">
            <span>
              {came.tasks_left.length} 条任务挂在 {came.project_name} 的需求「{firstLeft.requirement_title}」
              {came.tasks_left.length > 1 ? " 等" : ""}上
            </span>
            <button
              className="text-button"
              disabled={disabled}
              onClick={() => void moveLeftTasks(attribution.project_id as string)}
              type="button"
            >
              也移过去
            </button>
          </div>
        )}
        {cueHint && (
          <div className="attribution-bar__row attribution-bar__muted">
            <button
              className="text-button"
              disabled={disabled}
              onClick={() => void dismissCue(cueHint.term_id, cueHint.term)}
              type="button"
            >
              以后不再用「{cueHint.term}」判断项目
            </button>
          </div>
        )}
      </div>
    );
  }

  return null;
}
