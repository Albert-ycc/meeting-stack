import { useState, type ReactNode } from "react";

import type { ApiClient } from "../api";
import { reassignNote } from "../cardCopy";
import type {
  AttributionEvidence,
  MeetingAttribution,
  MeetingCard,
  MeetingDetail,
  NameHint,
  Project,
  RequirementRef,
} from "../types";
import "./AttributionBar.css";
import { NewNamePrompt, type PromptNotice } from "./NewNamePrompt";

/** 归属条上的操作改动了会议的归属：带回新的归属对象，以及（能拿到时）会议的项目字段。 */
export interface AttributionChange {
  attribution: MeetingAttribution;
  project?: { id: string | null; name: string | null; color: string | null; origin: "manual" | "ai" | null };
  /** 改归属、撤销之后卡片的新状态（接口带回时） */
  card?: MeetingCard;
  /** 读回的会议关联的需求（建成需求以后会多一个） */
  requirements?: RequirementRef[];
}

interface AttributionBarProps {
  apiClient: ApiClient;
  meetingId: string;
  attribution: MeetingAttribution;
  projects: Project[];
  /** 检查器里有没保存的项目改动时，归属条的按钮都置灰并说明原因 */
  lockedReason?: string;
  /** 手机上：「像是新项目 / 新需求」提示只读，不显示会动磁盘和建东西的按钮 */
  isMobile?: boolean;
  onSeek?: (ms: number) => void;
  /** 提示里同名的另一场会的 ▶；不给就只列会名和日期 */
  onPlayMeeting?: (meetingId: string, ms: number) => void;
  onChange: (change: AttributionChange) => void;
  /** 结果提示；undoUntil 有值时提示里带改归属的［撤销］，actions 是提示自带的按钮（［撤销］［打开需求］） */
  onNotice: PromptNotice;
  onProjectsChanged?: () => Promise<void> | void;
  /** 建成需求后提示里的［打开需求］ */
  onOpenRequirement?: (requirementId: string) => void;
  /** 已归项目的会上出「像是『P』里的一个新需求」。会议页不出：改由需求池的待认领候选承接（R01-11）；
   * 关系图的会议面板照旧出 */
  requirementHints?: boolean;
}

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
  detail: Pick<
    MeetingDetail,
    "project_id" | "project_name" | "project_color" | "project_origin" | "attribution" | "card" | "requirements"
  >,
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
    ...(detail.requirements ? { requirements: detail.requirements } : {}),
  };
}

/** 这场会的「像是新项目 / 新需求」提示；旧数据没有 name_hint 字段时按原来的 new_project_name 算。 */
function hintOf(attribution: MeetingAttribution): NameHint | null {
  if (attribution.name_hint !== undefined) return attribution.name_hint;
  if (attribution.state === "new_project" && attribution.new_project_name) {
    return { kind: "project", name: attribution.new_project_name, spoken: [] };
  }
  return null;
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
  isMobile = false,
  onSeek,
  onPlayMeeting,
  onChange,
  onNotice,
  onProjectsChanged,
  onOpenRequirement,
  requirementHints = false,
}: AttributionBarProps) {
  const [busy, setBusy] = useState(false);
  const [expanded, setExpanded] = useState(false);
  const [changing, setChanging] = useState(false);
  // 待你选的会：「也可能是一个新项目」点开后的提示
  const [promptOpen, setPromptOpen] = useState(false);
  const disabled = busy || Boolean(lockedReason);
  const projectName = (projectId: string | null) =>
    projects.find((project) => project.id === projectId)?.name ?? "";

  const run = async (action: () => Promise<void>) => {
    setBusy(true);
    try {
      await action();
    } catch (error) {
      onNotice(error instanceof Error ? error.message : "操作失败", undefined, "error");
    } finally {
      setBusy(false);
    }
  };

  /** 改归属；others 是同名的另几场会（提示里点［用它］时一起改），这时不给撤销：撤销只管这一场 */
  const assign = (projectId: string, others: string[] = []) =>
    run(async () => {
      const detail = await apiClient.updateMeeting(meetingId, { project_id: projectId });
      // 这场已经改成了，先反映到页面上；同名的另几场各改各的，一场失败不连累这场和后面几场
      const change = changeFromDetail(detail);
      if (change) onChange(change);
      setChanging(false);
      setPromptOpen(false);
      const failures: string[] = [];
      for (const other of others) {
        try {
          await apiClient.updateMeeting(other, { project_id: projectId });
        } catch (error) {
          failures.push(error instanceof Error ? error.message : "操作失败");
        }
      }
      const target = projectId && projectId !== "__ai__" ? projectName(projectId) || detail.project_name || "" : "";
      const effects = detail.effects;
      const note = effects ? reassignNote(effects.tasks_moved, effects.tasks_left.length, effects.card) : "";
      const head =
        projectId === "__ai__" ? "已交给 AI 重新判断" : target ? `已改到 ${target}` : "已标为不归项目";
      const tail = !others.length
        ? ""
        : failures.length
          ? `；同名的另 ${others.length} 场里 ${failures.length} 场没改成：${failures[0]}`
          : `；同名的另 ${others.length} 场会也改过去了`;
      const message = `${note ? `${head}：${note}` : head}${tail}`;
      if (failures.length) onNotice(message, undefined, "warning");
      else onNotice(message, others.length ? undefined : effects?.undo_until);
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

  /** 提示里建成、撤销以后读回这场会 */
  const reloadMeeting = async () => {
    const detail = await apiClient.meeting(meetingId);
    const change = changeFromDetail(detail);
    if (change) onChange(change);
  };

  /** ［不是新项目］［不算新需求］：提示就地清掉；像新项目的会变成没认出 */
  const dismissHint = () => {
    const wasNew = attribution.state === "new_project";
    onChange({
      attribution: {
        ...attribution,
        state: wasNew ? "none" : attribution.state,
        new_project_name: wasNew ? null : attribution.new_project_name,
        name_hint: null,
      },
    });
    setPromptOpen(false);
  };

  const moveLeftTasks = (projectId: string) =>
    run(async () => {
      const left = attribution.reassigned_from?.tasks_left ?? [];
      // 逐条移，移成的就从「挂在旧需求上」里去掉；没移成的留着，可以再点一次
      const stuck: typeof left = [];
      let reason = "";
      for (const task of left) {
        try {
          await apiClient.updateTask(task.id, { project_id: projectId, requirement_id: null });
        } catch (error) {
          stuck.push(task);
          reason ||= error instanceof Error ? error.message : "操作失败";
        }
      }
      onChange({
        attribution: {
          ...attribution,
          reassigned_from: attribution.reassigned_from && { ...attribution.reassigned_from, tasks_left: stuck },
        },
      });
      const moved = left.length - stuck.length;
      if (!stuck.length) onNotice(`${left.length} 条任务也移过来了，已移出原来的需求`);
      else if (moved) onNotice(`${moved} 条任务移过来了，${stuck.length} 条没移成：${reason}`, undefined, "warning");
      else onNotice(`任务没移过来：${reason}`, undefined, "error");
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
  const hint = hintOf(attribution);

  /** 「像是新项目 / 新需求」提示；nested：嵌在别的归属条里 */
  const namePrompt = (value: NameHint, options: { nested?: boolean; collapsible?: boolean; withLock?: boolean } = {}): ReactNode => (
    <NewNamePrompt
      apiClient={apiClient}
      disabled={disabled}
      hint={value}
      key={`${meetingId}:${value.kind}:${value.name}`}
      lockNote={options.withLock ? lockNote : undefined}
      meetingId={meetingId}
      nested={options.nested}
      onAssign={(projectId, others) => assign(projectId, others)}
      onCollapse={options.collapsible ? () => setPromptOpen(false) : undefined}
      onDismissed={dismissHint}
      onNotice={onNotice}
      onOpenRequirement={onOpenRequirement}
      onPlayMeeting={onPlayMeeting}
      onProjectsChanged={onProjectsChanged}
      onReload={reloadMeeting}
      onSeek={onSeek}
      pickExisting={
        value.kind === "project" && (
          <PickProject disabled={disabled} exclude={[]} label="选已有项目" onPick={(id) => void assign(id)} projects={projects} />
        )
      }
      projects={projects}
      readOnly={isMobile}
    />
  );
  // 已归项目的会（自动或你归的）：「像是『P』里的一个新需求」
  const requirementHint =
    requirementHints && hint?.kind === "requirement" && (state === "auto" || state === "manual") ? hint : null;

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
        {requirementHint && namePrompt(requirementHint, { nested: true })}
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
    // AI 没选项目、又提了新项目名：候选按钮下面加一行「也可能是一个新项目」，点了展开同一个提示
    const alsoNew = hint?.kind === "project" ? hint : null;
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
        {alsoNew &&
          (promptOpen && !isMobile ? (
            namePrompt(alsoNew, { nested: true, collapsible: true })
          ) : (
            <div className="attribution-bar__row attribution-bar__also">
              <span>也可能是一个新项目『{alsoNew.name}』</span>
              {isMobile ? (
                <span className="attribution-bar__muted">在电脑上打开可以建项目并建文件夹</span>
              ) : (
                <button className="ghost-button" disabled={disabled} onClick={() => setPromptOpen(true)} type="button">
                  建成项目
                </button>
              )}
            </div>
          ))}
      </div>
    );
  }

  if (state === "ai_pending" || state === "none" || (state === "new_project" && !hint)) {
    const text =
      state === "ai_pending"
        ? attribution.ai_configured
          ? "正在判断属于哪个项目（通常一分钟内）"
          : "没配置 AI，请在这里选项目"
        : "AI 没认出这场会属于哪个项目";
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

  if (state === "new_project" && hint) {
    return (
      <div className="attribution-bar attribution-bar--new" role="group" aria-label="项目归属">
        {namePrompt(hint, { withLock: true })}
      </div>
    );
  }

  if (state !== "manual") return null;
  const came = attribution.reassigned_from;
  const at = came ? new Date(came.at).getTime() : Number.NaN;
  const recent = !Number.isNaN(at) && Date.now() - at < RECENT_CHANGE_DAYS * 24 * 3600 * 1000;
  if (!came || !recent) {
    // 你归的会没有最近的改动：只在有「像是新需求」时出现
    return requirementHint ? (
      <div className="attribution-bar attribution-bar--new" role="group" aria-label="项目归属">
        {namePrompt(requirementHint, { withLock: true })}
      </div>
    ) : null;
  }
  // 过了撤销期就再改一次：原来在项目里就改回那个项目，原来是你标的不归项目就标回去，原来没归才交给 AI
  const revert = () =>
    came.can_undo
      ? void undo()
      : void assign(came.project_id ?? (came.origin_before === "manual" ? "" : "__ai__"));
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
      {requirementHint && namePrompt(requirementHint, { nested: true })}
    </div>
  );
}
