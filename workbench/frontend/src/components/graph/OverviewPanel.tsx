/* 全部项目概览的右侧面板：项目岛、港湾、幽灵岛、灰色文件夹岛各自的内容。问题在哪出现就在哪回答，作答后那一行消失。 */
import { useEffect, useState, type ReactNode } from "react";

import type { ApiClient } from "../../api";
import { reassignNote } from "../../cardCopy";
import { formatDate } from "../../format";
import type { ClaimItem, MeetingDetail, MeetingSummary, NameHint, Project, Task } from "../../types";
import { nestedHints } from "../ClaimFoldersDialog";
import { ReviewStrip } from "../LibraryPage";
import { NewNamePrompt } from "../NewNamePrompt";
import type { NoticeAction, NoticeTone } from "../Notice";
import type { DiskState, GraphRootsPayload } from "./graphTypes";
import { meetingDateLabel } from "./layout";
import { islandCountText, type OverviewNode } from "./layoutOverview";
import type { GraphOverview, HarbourMeeting, OverviewIsland, SuggestedProject } from "./overviewTypes";
import { CopyPath, Section } from "./panelParts";
import { harbourLines } from "./OverviewCanvas";
import "./GraphPanel.css";

export type OverviewNotice = (message: string, tone?: NoticeTone, actions?: NoticeAction[]) => void;

export interface OverviewPanelProps {
  apiClient: ApiClient;
  overview: GraphOverview;
  node: OverviewNode;
  projects: Project[];
  /** 作答、撤销后加一：面板里自己取的数据跟着重取 */
  version: number;
  /** 已经作答、等概览重取回来之前先藏起来的行（w:会议、t:任务、h:港湾里的会、np:名字、fd:路径） */
  hidden: Set<string>;
  onHide: (key: string) => void;
  onUnhide: (key: string) => void;
  onClose: () => void;
  onSelect: (id: string | null) => void;
  /** 重取概览、文件夹和项目列表 */
  onChanged: () => Promise<void>;
  onNotice: OverviewNotice;
  onOpenProjectGraph: (projectId: string) => void;
  onOpenProject: (projectId: string) => void;
  onOpenMeeting: (meetingId: string) => void;
  onOpenRequirement: (requirementId: string) => void;
  /** 资料库：none 是没归项目的会，new_project 是「像新项目」筛选 */
  onOpenLibrary: (filter: "none" | "new_project") => void;
  onPickParent: () => void;
  onUseSuggestedParent: (path: string) => void;
  parentBusy: boolean;
}

const DISK_TEXT: Record<DiskState, string> = {
  online: "在线",
  volume_offline: "资料盘未连接",
  missing: "找不到了",
  checking: "正在检查…",
};

function errorText(reason: unknown, fallback: string) {
  return reason instanceof Error && reason.message ? reason.message : fallback;
}

/** 提示里的［撤销］只能点一次 */
function once(action: () => Promise<void>): () => void {
  let used = false;
  return () => {
    if (used) return;
    used = true;
    void action();
  };
}

function ageDays(day: string, today: string) {
  return Math.max(0, Math.round((Date.parse(`${today}T00:00:00Z`) - Date.parse(`${day}T00:00:00Z`)) / 86_400_000));
}

function meetingDay(meeting: MeetingSummary) {
  return (meeting.recording_date ?? meeting.created_at ?? "").slice(0, 10);
}

/** 改归属以后的一句话，带改归属的［撤销］（服务器给了撤销期时） */
function useReassign(props: OverviewPanelProps) {
  return async (meetingId: string, projectId: string, hideKey: string, head: string) => {
    const detail: MeetingDetail = await props.apiClient.updateMeeting(meetingId, { project_id: projectId });
    const effects = detail.effects;
    const note = effects ? reassignNote(effects.tasks_moved, effects.tasks_left.length, effects.card) : "";
    props.onHide(hideKey);
    const actions: NoticeAction[] = effects?.undo_until
      ? [
          {
            label: "撤销",
            onClick: once(async () => {
              try {
                await props.apiClient.undoMeetingProject(meetingId);
                props.onUnhide(hideKey);
                props.onNotice("已撤销刚才的改动");
                await props.onChanged();
              } catch (reason) {
                props.onNotice(errorText(reason, "撤销失败"), "error");
              }
            }),
          },
        ]
      : [];
    props.onNotice(note ? `${head}；${note}` : head, "success", actions);
    await props.onChanged();
  };
}

/** ［选已有项目 ▾］［挂到已有项目 ▾］：选了就做，下拉回到空 */
function PickProject({
  label,
  projects,
  disabled,
  first,
  onPick,
}: {
  label: string;
  projects: Project[];
  disabled?: boolean;
  /** 排在最前的项目（和文件夹同名、相近的那个） */
  first?: string | null;
  onPick: (project: Project) => void;
}) {
  const ordered = first
    ? [...projects.filter((project) => project.id === first), ...projects.filter((project) => project.id !== first)]
    : projects;
  return (
    <select
      aria-label={label}
      className="overview-panel__pick"
      disabled={disabled}
      onChange={(event) => {
        const project = projects.find((item) => item.id === event.target.value);
        event.target.value = "";
        if (project) onPick(project);
      }}
      value=""
    >
      <option value="">{label} ▾</option>
      {ordered.map((project) => (
        <option key={project.id} value={project.id}>
          {project.name}
        </option>
      ))}
    </select>
  );
}

// ------------------------------------------------------------------ 「像是新项目」提示（港湾的行、幽灵岛）

function NamePrompt({
  props,
  meetingId,
  hint,
  hideKey,
  nested = false,
  onCollapse,
}: {
  props: OverviewPanelProps;
  meetingId: string;
  hint: NameHint;
  hideKey: string;
  nested?: boolean;
  onCollapse?: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const reassign = useReassign(props);
  const projectName = (projectId: string) => props.projects.find((project) => project.id === projectId)?.name ?? "所选项目";

  /** ［用它］［选已有项目］：这场会（和同名的另几场）改到已有项目 */
  const assign = async (projectId: string, others: string[] = []) => {
    setBusy(true);
    try {
      if (others.length === 0) {
        await reassign(meetingId, projectId, hideKey, `已归到 ${projectName(projectId)}`);
      } else {
        await props.apiClient.updateMeeting(meetingId, { project_id: projectId });
        for (const other of others) await props.apiClient.updateMeeting(other, { project_id: projectId });
        props.onHide(hideKey);
        props.onNotice(`已归到 ${projectName(projectId)}；同名的另 ${others.length} 场会也改过去了`);
        await props.onChanged();
      }
    } catch (reason) {
      props.onNotice(errorText(reason, "改归属失败"), "error");
    } finally {
      setBusy(false);
    }
  };

  return (
    <NewNamePrompt
      apiClient={props.apiClient}
      disabled={busy}
      hint={hint}
      key={`${meetingId}:${hint.name}`}
      meetingId={meetingId}
      nested={nested}
      onAssign={(projectId, others) => assign(projectId, others)}
      onCollapse={onCollapse}
      // 建成、撤销以后重取概览：建成了这组会就离开港湾，撤销了又回来
      onDismissed={() => void props.onChanged()}
      onNotice={(message, _undoUntil, tone, actions) => props.onNotice(message, tone, actions)}
      onOpenRequirement={props.onOpenRequirement}
      onProjectsChanged={props.onChanged}
      onReload={props.onChanged}
      pickExisting={
        <PickProject
          disabled={busy}
          label="选已有项目"
          onPick={(project) => void assign(project.id)}
          projects={props.projects}
        />
      }
      projects={props.projects}
    />
  );
}

// ------------------------------------------------------------------ 项目岛

interface WaitingData {
  meetings: MeetingSummary[];
  tasks: Task[];
}

function IslandBody({ props, island }: { props: OverviewPanelProps; island: OverviewIsland }) {
  const { apiClient, overview, hidden } = props;
  const [waiting, setWaiting] = useState<WaitingData | null>(null);
  const [waitingError, setWaitingError] = useState("");
  const [roots, setRoots] = useState<GraphRootsPayload | null>(null);
  const [busy, setBusy] = useState(false);
  const reassign = useReassign(props);
  const hasMeetings = island.waiting.review + island.waiting.doorstep > 0;
  const hasTasks = island.waiting.tasks > 0;

  // 在等你的会（待复核的、门口的）和待确认任务：点开岛时取
  useEffect(() => {
    let active = true;
    setWaitingError("");
    const meetings =
      hasMeetings && typeof apiClient.meetings === "function"
        ? apiClient.meetings({ attribution: "needs_review", limit: 100 }).then((payload) => payload.items)
        : Promise.resolve([] as MeetingSummary[]);
    const tasks =
      hasTasks && typeof apiClient.tasks === "function"
        ? apiClient.tasks({ status: "pending_confirm", project_id: island.id, limit: 50 }).then((payload) => payload.items)
        : Promise.resolve([] as Task[]);
    Promise.all([meetings, tasks])
      .then(([meetingItems, taskItems]) => active && setWaiting({ meetings: meetingItems, tasks: taskItems }))
      .catch((reason: unknown) => active && setWaitingError(errorText(reason, "读取失败")));
    return () => {
      active = false;
    };
  }, [apiClient, hasMeetings, hasTasks, island.id, props.version]);

  // 文件夹在不在线：读资料盘缓存，不读盘
  useEffect(() => {
    if (island.roots.length === 0 || typeof apiClient.graphRoots !== "function") return;
    let active = true;
    apiClient
      .graphRoots(island.id)
      .then((payload) => active && setRoots(payload))
      .catch(() => undefined);
    return () => {
      active = false;
    };
  }, [apiClient, island.id, island.roots.length]);

  const days = overview.window.days;
  const review = (waiting?.meetings ?? []).filter(
    (meeting) => meeting.project_id === island.id && !hidden.has(`w:${meeting.id}`),
  );
  // 门口的会和概览同一个口径：没归项目、候选里有它、在时间窗里
  const doorstep = (waiting?.meetings ?? []).filter(
    (meeting) =>
      !meeting.project_id &&
      (meeting.candidates ?? []).some((candidate) => candidate.project_id === island.id) &&
      (days === null || ageDays(meetingDay(meeting), overview.today) < days) &&
      !hidden.has(`w:${meeting.id}`),
  );
  const tasks = (waiting?.tasks ?? []).filter((task) => !hidden.has(`t:${task.id}`));

  const run = async (work: () => Promise<void>) => {
    setBusy(true);
    try {
      await work();
    } catch (reason) {
      props.onNotice(errorText(reason, "操作失败"), "error");
    } finally {
      setBusy(false);
    }
  };

  const answer = (meeting: MeetingSummary, target: "here" | "none" | { id: string; name: string }, isReview: boolean) =>
    run(async () => {
      const key = `w:${meeting.id}`;
      if (target === "here" && isReview) {
        // 已经在这个项目里：归这里就是确认
        await apiClient.confirmMeetingProject(meeting.id);
        props.onHide(key);
        props.onNotice(`已确认归到 ${island.name}`);
        await props.onChanged();
        return;
      }
      if (target === "here") await reassign(meeting.id, island.id, key, `已归到 ${island.name}`);
      else if (target === "none") await reassign(meeting.id, "", key, "已标为不归这些项目");
      else await reassign(meeting.id, target.id, key, `已归到 ${target.name}`);
    });

  const answerTask = (task: Task, confirm: boolean) =>
    run(async () => {
      if (confirm) await apiClient.confirmTask(task.id);
      else await apiClient.rejectTask(task.id);
      props.onHide(`t:${task.id}`);
      props.onNotice(confirm ? `已确认「${task.title}」` : `已不要「${task.title}」`);
      await props.onChanged();
    });

  const meetingRow = (meeting: MeetingSummary, isReview: boolean) => {
    const other = (meeting.candidates ?? []).find((candidate) => candidate.project_id !== island.id);
    const day = meetingDay(meeting);
    return (
      <li className="overview-panel__row" key={meeting.id}>
        <span className="overview-panel__row-title">
          <button className="text-button" onClick={() => props.onOpenMeeting(meeting.id)} type="button">
            {day ? `${meetingDateLabel(day, overview.today)} ` : ""}
            {meeting.title}
          </button>
          <small>{isReview ? "归属待复核" : "可能是这个项目的"}</small>
        </span>
        <span aria-label={`${meeting.title} 归哪个项目`} className="graph-doorstep__actions" role="group">
          <button disabled={busy} onClick={() => void answer(meeting, "here", isReview)} type="button">
            归这里
          </button>
          {other && (
            <button
              disabled={busy}
              onClick={() => void answer(meeting, { id: other.project_id, name: other.project_name }, isReview)}
              type="button"
            >
              归 {other.project_name}
            </button>
          )}
          <button disabled={busy} onClick={() => void answer(meeting, "none", isReview)} type="button">
            都不是
          </button>
        </span>
      </li>
    );
  };

  const waitingCount = island.waiting.review + island.waiting.doorstep + island.waiting.tasks;
  let waitingBody: ReactNode;
  if (waitingCount === 0) waitingBody = <p className="graph-panel__muted">没有在等你的</p>;
  else if (waitingError) waitingBody = <p className="graph-panel__error">{waitingError}</p>;
  else if (!waiting) waitingBody = <p className="graph-panel__muted">正在读取…</p>;
  else if (review.length + doorstep.length + tasks.length === 0) waitingBody = <p className="graph-panel__muted">都处理好了</p>;
  else
    waitingBody = (
      <ul className="graph-panel__list">
        {review.map((meeting) => meetingRow(meeting, true))}
        {doorstep.map((meeting) => meetingRow(meeting, false))}
        {tasks.map((task) => (
          <li className="graph-panel__task" key={task.id}>
            <span>
              {task.title}
              <small>待确认{task.meeting_title ? ` · ${task.meeting_title}` : ""}</small>
            </span>
            <span className="graph-panel__actions">
              <button className="ghost-button" disabled={busy} onClick={() => void answerTask(task, true)} type="button">
                确认
              </button>
              <button className="text-button" disabled={busy} onClick={() => void answerTask(task, false)} type="button">
                不要
              </button>
            </span>
          </li>
        ))}
      </ul>
    );

  const pending = island.pending_folder;
  return (
    <>
      <dl className="graph-panel__counts">
        <div>
          <dt>会</dt>
          <dd>{island.meetings} 场</dd>
          <small>{days ? `最近 ${days} 天` : "全部"}</small>
        </div>
        <div>
          <dt>进行中的需求</dt>
          <dd>{island.requirements_active} 个</dd>
        </div>
      </dl>
      <Section title="文件夹">
        {island.roots.length === 0 && !pending ? (
          <p className="graph-panel__muted">还没挂文件夹</p>
        ) : (
          <ul className="graph-panel__list">
            {island.roots.map((root) => {
              const state = roots?.roots.find((item) => item.root_id === root.id)?.state;
              return (
                <li key={root.id}>
                  <span>{root.name}/</span>
                  <small className={state && state !== "online" ? "graph-panel__warn" : undefined}>
                    {state ? DISK_TEXT[state] : "正在检查…"}
                  </small>
                </li>
              );
            })}
            {pending && (
              <li>
                <small className="graph-panel__warn">
                  {pending.state === "waiting"
                    ? `资料盘未连接，插上后自动建 ${pending.path}`
                    : "要放新文件夹的位置不存在了"}
                </small>
              </li>
            )}
          </ul>
        )}
      </Section>
      <Section title={`在等你${waitingCount ? ` ${waitingCount}` : ""}`}>{waitingBody}</Section>
      {island.stopped_cards > 0 && (
        <Section title="停了">
          <p>{island.stopped_cards} 张卡片停了，打开项目图看是哪几场会</p>
        </Section>
      )}
    </>
  );
}

// ------------------------------------------------------------------ 港湾

const HARBOUR_STATE_TEXT: Record<HarbourMeeting["state"], string> = {
  ai_pending: "等 AI 判断",
  needs_review: "待你选",
  none: "AI 没认出",
  new_project: "像新项目",
};

function HarbourRow({ props, row }: { props: OverviewPanelProps; row: HarbourMeeting }) {
  const [promptOpen, setPromptOpen] = useState(false);
  const reassign = useReassign(props);
  const hint = row.name_hint?.kind === "project" ? row.name_hint : null;
  const projectName = (projectId: string) => props.projects.find((project) => project.id === projectId)?.name;
  const key = `h:${row.id}`;
  return (
    <li className="overview-panel__row overview-panel__harbour-row">
      <span className="overview-panel__row-title">
        <button className="text-button" onClick={() => props.onOpenMeeting(row.id)} type="button">
          {meetingDateLabel(row.day, props.overview.today)} {row.title}
        </button>
        {row.state === "new_project" && hint ? (
          <button
            aria-expanded={promptOpen}
            className="text-button text-button--accent"
            onClick={() => setPromptOpen((open) => !open)}
            type="button"
          >
            像新项目『{hint.name}』
          </button>
        ) : (
          <small>{HARBOUR_STATE_TEXT[row.state]}</small>
        )}
      </span>
      {row.state === "needs_review" && (
        <ReviewStrip
          meeting={{ id: row.id, title: row.title, candidates: row.candidates }}
          onAssignProject={(meetingId, projectId) =>
            reassign(
              meetingId,
              projectId,
              key,
              projectId ? `已归到 ${projectName(projectId) ?? row.candidates.find((c) => c.project_id === projectId)?.project_name ?? "所选项目"}` : "已标为不归项目",
            )
          }
          onConfirmProject={async (meetingId) => {
            await props.apiClient.confirmMeetingProject(meetingId);
            props.onHide(key);
            await props.onChanged();
          }}
        />
      )}
      {row.state === "needs_review" && hint && !promptOpen && (
        <p className="overview-panel__also">
          也可能是一个新项目『{hint.name}』
          <button className="text-button" onClick={() => setPromptOpen(true)} type="button">
            建成项目
          </button>
        </p>
      )}
      {promptOpen && hint && (
        <NamePrompt hideKey={key} hint={hint} meetingId={row.id} nested onCollapse={() => setPromptOpen(false)} props={props} />
      )}
    </li>
  );
}

function HarbourBody({ props }: { props: OverviewPanelProps }) {
  const harbour = props.overview.harbour;
  const lines = harbourLines(harbour);
  const rows = harbour.recent.filter((row) => !props.hidden.has(`h:${row.id}`));
  return (
    <>
      {lines.counts && <p className="graph-panel__meta">{lines.counts}</p>}
      <Section title={`最近 ${rows.length} 场`}>
        {rows.length ? (
          <ul className="graph-panel__list">
            {rows.map((row) => (
              <HarbourRow key={row.id} props={props} row={row} />
            ))}
          </ul>
        ) : (
          <p className="graph-panel__muted">这段时间没有没归项目的会</p>
        )}
      </Section>
    </>
  );
}

// ------------------------------------------------------------------ 幽灵岛

function GhostBody({ props, item }: { props: OverviewPanelProps; item: SuggestedProject }) {
  const meetingId = item.meeting_ids[0];
  if (!meetingId) return <p className="graph-panel__muted">这组会已经处理好了</p>;
  return <NamePrompt hideKey={`np:${item.key}`} hint={{ kind: "project", name: item.name, spoken: [] }} meetingId={meetingId} props={props} />;
}

// ------------------------------------------------------------------ 灰色文件夹岛

type FolderQuestion = { id: string; name: string; exact: boolean } | null;

function FolderBody({ props, node }: { props: OverviewPanelProps; node: Extract<OverviewNode, { kind: "folder" }> }) {
  const folder = node.data;
  const { apiClient } = props;
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [question, setQuestion] = useState<FolderQuestion>(null);
  const canClaim = typeof apiClient.claimFolders === "function";
  const key = `fd:${folder.path}`;

  const claim = async (item: ClaimItem) => {
    if (!canClaim || busy) return;
    setBusy(true);
    setError("");
    try {
      const result = await apiClient.claimFolders([item]);
      const entry = result.items.find((row) => row.path === folder.path) ?? result.items[0];
      if (!entry?.ok) {
        const suggested = entry?.suggestion ? entry.suggestion.id ?? entry.suggestion.project_id : undefined;
        setQuestion(
          entry?.suggestion && suggested
            ? { id: suggested, name: entry.suggestion.name, exact: entry.suggestion.exact === true }
            : null,
        );
        setError(entry?.error ?? "没处理成，请稍后重试");
        return;
      }
      const name = entry.project_name ?? folder.name;
      const head = item.action === "create" ? `已建成项目『${name}』，挂上了 ${folder.path}` : `已把 ${folder.path} 挂到『${name}』`;
      const tail = [
        entry.cards_written ? `补写了 ${entry.cards_written} 张会议卡片` : "",
        result.needs_review ? `另有 ${result.needs_review} 场没认出的会提到了它，已放进待你选` : "",
        ...nestedHints(folder.path, entry.nested),
      ].filter(Boolean);
      props.onHide(key);
      props.onSelect(null);
      props.onNotice(tail.length ? `${head}；${tail.join("；")}` : head);
      await props.onChanged();
    } catch (reason) {
      setError(errorText(reason, "没处理成，请稍后重试"));
    } finally {
      setBusy(false);
    }
  };

  const decline = async () => {
    if (busy) return;
    setBusy(true);
    try {
      await apiClient.declineFolder(folder.path);
      props.onHide(key);
      props.onSelect(null);
      props.onNotice(`『${folder.name}』不算项目，以后不再列出`, "success", [
        {
          label: "撤销",
          onClick: once(async () => {
            try {
              await apiClient.undeclineFolder(folder.path);
              props.onUnhide(key);
              props.onNotice("已撤销");
              await props.onChanged();
            } catch (reason) {
              props.onNotice(errorText(reason, "没撤销成，请稍后重试"), "error");
            }
          }),
        },
      ]);
      await props.onChanged();
    } catch (reason) {
      setError(errorText(reason, "没记上，请稍后重试"));
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <CopyPath onNotice={(message, _undo, tone) => props.onNotice(message, tone)} path={folder.path} />
      <p className="graph-panel__meta">修改时间 {formatDate(folder.modified_at)}</p>
      <div className="overview-panel__folder-actions">
        <button className="primary-button" disabled={busy || !canClaim} onClick={() => void claim({ path: folder.path, action: "create" })} type="button">
          建成项目
        </button>
        <PickProject
          disabled={busy || !canClaim}
          first={folder.project_id}
          label="挂到已有项目"
          onPick={(project) => void claim({ path: folder.path, action: "mount", project_id: project.id })}
          projects={props.projects}
        />
        <button className="text-button" disabled={busy} onClick={() => void decline()} type="button">
          不是项目
        </button>
      </div>
      {error &&
        (question ? (
          <p className="overview-panel__question" role="alert">
            已有『{question.name}』，是不是它？
            <button
              className="ghost-button"
              disabled={busy}
              onClick={() => void claim({ path: folder.path, action: "mount", project_id: question.id })}
              type="button"
            >
              挂到它
            </button>
            {!question.exact && (
              <button
                className="text-button"
                disabled={busy}
                onClick={() => void claim({ path: folder.path, action: "create", force: true })}
                type="button"
              >
                仍然新建
              </button>
            )}
          </p>
        ) : (
          <p className="graph-panel__error" role="alert">
            {error}
          </p>
        ))}
    </>
  );
}

// ------------------------------------------------------------------ 外壳

const KIND_LABEL: Partial<Record<OverviewNode["kind"], string>> = {
  island: "项目",
  island_more: "项目",
  harbour: "港湾",
  ghost: "像新项目",
  folder: "没挂到项目的文件夹",
  folder_hint: "项目总文件夹",
};

function titleOf(node: OverviewNode, overview: GraphOverview): string {
  switch (node.kind) {
    case "island":
      return node.data.name;
    case "island_more":
      return `其余 ${node.data.count} 个项目`;
    case "harbour":
      return harbourLines(overview.harbour).head;
    case "ghost":
      return `『${node.data.name}』`;
    case "folder":
      return node.data.name;
    case "folder_hint":
      return "还没设项目总文件夹";
    default:
      return node.label;
  }
}

export function OverviewPanel(props: OverviewPanelProps) {
  const { node, overview } = props;
  let body: ReactNode = null;
  let foot: ReactNode = null;
  switch (node.kind) {
    case "island":
      body = <IslandBody island={node.data} key={node.id} props={props} />;
      foot = (
        <>
          <button className="ghost-button" onClick={() => props.onOpenProject(node.data.id)} type="button">
            打开项目页
          </button>
          <button className="primary-button" onClick={() => props.onOpenProjectGraph(node.data.id)} type="button">
            打开项目图 →
          </button>
        </>
      );
      break;
    case "island_more": {
      const folded = node.data.project_ids
        .map((id) => props.projects.find((project) => project.id === id))
        .filter((project): project is Project => Boolean(project));
      body = (
        <>
          <p className="graph-panel__muted">这段时间没有会的项目收在这里（按建立先后）。</p>
          <ul className="graph-panel__list">
            {folded.map((project) => (
              <li key={project.id}>
                <button className="text-button" onClick={() => props.onOpenProjectGraph(project.id)} type="button">
                  {project.name}
                </button>
              </li>
            ))}
          </ul>
        </>
      );
      break;
    }
    case "harbour":
      body = <HarbourBody props={props} />;
      foot = (
        <button className="primary-button" onClick={() => props.onOpenLibrary("none")} type="button">
          在资料库里看全部
        </button>
      );
      break;
    case "ghost":
      body = <GhostBody item={node.data} key={node.id} props={props} />;
      if (node.data.meeting_ids[0]) {
        const meetingId = node.data.meeting_ids[0];
        foot = (
          <button className="primary-button" onClick={() => props.onOpenMeeting(meetingId)} type="button">
            打开会议页 →
          </button>
        );
      }
      break;
    case "folder":
      body = <FolderBody key={node.id} node={node} props={props} />;
      break;
    case "folder_hint": {
      const suggested = node.data;
      body = (
        <>
          <p>设项目总文件夹后，这里会列出还没挂的文件夹。</p>
          {suggested && (
            <p className="overview-panel__also">
              你挂过的 {suggested.total} 个项目文件夹里有 {suggested.count} 个在 <code>{suggested.path}</code> 下面
              <button
                className="text-button"
                disabled={props.parentBusy}
                onClick={() => props.onUseSuggestedParent(suggested.path)}
                type="button"
              >
                用这个
              </button>
            </p>
          )}
          <button className="ghost-button" disabled={props.parentBusy} onClick={props.onPickParent} type="button">
            选项目总文件夹…
          </button>
        </>
      );
      break;
    }
    default:
      body = null;
  }

  return (
    <aside aria-label="详情面板" className="graph-panel overview-panel">
      <header className="graph-panel__head">
        <div>
          <span className="graph-panel__kind">{KIND_LABEL[node.kind] ?? ""}</span>
          <h2>{titleOf(node, overview)}</h2>
          {node.kind === "island" && (
            <span className="graph-panel__kind">{islandCountText(overview.window.days, node.data.meetings)}</span>
          )}
        </div>
        <div className="graph-panel__nav">
          <button aria-label="关闭面板" className="graph-panel__close" onClick={props.onClose} type="button">
            ✕
          </button>
        </div>
      </header>
      <div className="graph-panel__body">{body}</div>
      {foot && <footer className="graph-panel__foot">{foot}</footer>}
    </aside>
  );
}
