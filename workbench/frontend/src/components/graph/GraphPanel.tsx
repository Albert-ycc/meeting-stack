import { useCallback, useEffect, useState, type ReactNode } from "react";

import type { ApiClient } from "../../api";
import { copyText } from "../../clipboard";
import { formatTime } from "../../format";
import type { MeetingCard, Project, RequirementFilesPayload, ProjectSubfoldersPayload } from "../../types";
import { AttributionBar } from "../AttributionBar";
import { MeetingCardStatus } from "../MeetingCardStatus";
import type {
  CollapsedPayload,
  CueTermDetail,
  GraphCue,
  GraphDoorstep,
  GraphEdge,
  GraphPayload,
  GraphRootsPayload,
  MeetingBrief,
  QuotesPayload,
} from "./graphTypes";
import { meetingDateLabel, type LaidNode, type StarLayout } from "./layout";
import type { MiniPlayerHandle } from "./MiniPlayer";
import "./GraphPanel.css";

export const TASK_STATUS: Record<string, string> = {
  pending_confirm: "待确认",
  confirmed: "已确认",
  in_progress: "进行中",
  done: "已完成",
  cancelled: "已取消",
  expired: "已过期",
};

const EDGE_KIND: Record<GraphEdge["kind"], string> = {
  attribution: "归属",
  cue: "线索",
  discussion: "讨论",
  folder: "文件夹",
  write: "写入会议卡片",
  cross: "跨项目",
};

const KIND_LABEL: Record<LaidNode["kind"], string> = {
  project: "项目",
  meeting: "会议",
  collapsed: "折叠的会",
  doorstep: "门口的会",
  doorstep_more: "门口",
  requirement: "需求",
  requirement_more: "需求",
  folder: "文件夹",
  folder_more: "文件夹",
  loose: "散放文件",
  cue: "线索词",
  beacon: "跨项目",
  ghost: "刚改走的会",
};

// 同一场会的简报在面板之间复用；图数据一变就清掉。
const briefCache = new Map<string, Promise<MeetingBrief>>();

export function clearBriefCache() {
  briefCache.clear();
}

function loadBrief(apiClient: ApiClient, meetingId: string): Promise<MeetingBrief> {
  let pending = briefCache.get(meetingId);
  if (!pending) {
    pending = apiClient.meetingBrief(meetingId);
    pending.catch(() => briefCache.delete(meetingId));
    briefCache.set(meetingId, pending);
  }
  return pending;
}

function useBrief(apiClient: ApiClient, meetingId: string | null, version: number) {
  const [brief, setBrief] = useState<MeetingBrief | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    if (!meetingId) return;
    let active = true;
    setError("");
    loadBrief(apiClient, meetingId)
      .then((value) => active && setBrief(value))
      .catch((reason: unknown) => active && setError(reason instanceof Error ? reason.message : "读取失败"));
    return () => {
      active = false;
    };
  }, [apiClient, meetingId, version]);
  return { brief: brief && brief.meeting.id === meetingId ? brief : null, error, setBrief };
}

/**
 * 画布上能撤销的一步：带［撤销］的提示、⌘Z、残影都走这里。改归属的撤销期由服务器定（10 分钟）；
 * 关联需求、搬任务由前端记下原样，同样只留 10 分钟。
 */
export type GraphNoticeUndo =
  | { kind: "project"; meetingId: string; until: string }
  | { kind: "link"; requirementId: string; meetingId: string; title: string; until: string }
  | {
      kind: "task";
      taskId: string;
      title: string;
      /** move：搬到别的项目或需求；edit：改了标题、说明。before 是改之前的原样 */
      what: "move" | "edit";
      before: { project_id?: string | null; requirement_id?: string | null; title?: string; detail?: string };
      until: string;
    };

/** 前端自己记的撤销（关联需求、搬任务）也只留 10 分钟 */
export const LOCAL_UNDO_MS = 10 * 60_000;

export function localUndoUntil(now = Date.now()) {
  return new Date(now + LOCAL_UNDO_MS).toISOString();
}

export interface GraphPanelProps {
  apiClient: ApiClient;
  graph: GraphPayload;
  layout: StarLayout;
  roots: GraphRootsPayload | null;
  selectedId: string;
  projects: Project[];
  canGoBack: boolean;
  version: number;
  player: MiniPlayerHandle;
  playerNode: ReactNode;
  onBack: () => void;
  onClose: () => void;
  onSelect: (id: string) => void;
  onHighlight: (ids: string[] | null) => void;
  onChanged: () => void | Promise<void>;
  onNotice: (message: string, undo?: GraphNoticeUndo, tone?: "success" | "warning" | "error") => void;
  onAnswerDoorstep: (meetingId: string, projectId: string | null) => void;
  onOpenMeeting: (meetingId: string) => void;
  /** 会议面板底部的［展开这场会］ */
  onExpandMeeting?: (meetingId: string) => void;
  onOpenRequirement: (requirementId: string) => void;
  onOpenGlossary: (projectId: string) => void;
  onOpenProject: (projectId: string) => void;
  onOpenAttributionReview?: () => void;
}

export function PlayButton({
  audioUrl,
  atMs,
  label,
  player,
}: {
  audioUrl: string | null | undefined;
  atMs: number | null | undefined;
  label: string;
  player: MiniPlayerHandle;
}) {
  if (atMs === null || atMs === undefined) return null;
  return (
    <button
      aria-label={`从 ${formatTime(atMs)} 播放`}
      className="graph-play"
      disabled={!audioUrl}
      onClick={() => audioUrl && player.play(audioUrl, atMs, label)}
      title={audioUrl ? undefined : "这场会没有录音文件"}
      type="button"
    >
      ▶ {formatTime(atMs)}
    </button>
  );
}

export function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="graph-panel__section">
      <h3>{title}</h3>
      {children}
    </section>
  );
}

// ------------------------------------------------------------------ 会议

function MeetingPanelBody({
  props,
  meetingId,
}: {
  props: GraphPanelProps;
  meetingId: string;
}) {
  const { apiClient, graph, player, projects } = props;
  const { brief, error, setBrief } = useBrief(apiClient, meetingId, props.version);
  const [busy, setBusy] = useState(false);
  const [adding, setAdding] = useState(false);

  const run = async (work: () => Promise<unknown>, message: string) => {
    setBusy(true);
    try {
      await work();
      briefCache.delete(meetingId);
      props.onNotice(message);
      await props.onChanged();
    } catch (reason) {
      props.onNotice(reason instanceof Error ? reason.message : "操作失败", undefined, "error");
    } finally {
      setBusy(false);
    }
  };

  if (error) return <p className="graph-panel__error">{error}</p>;
  if (!brief) return <p className="graph-panel__muted">正在读取这场会…</p>;
  const audio = brief.meeting.audio_url;
  const title = brief.meeting.title;
  const linked = new Set(brief.requirements.map((item) => item.id));
  const addable = graph.requirements.filter((item) => !linked.has(item.requirement_id));

  return (
    <>
      {brief.attribution.state === "ai_pending" ? (
        <p className="graph-panel__muted">归属：等 AI 判断</p>
      ) : (
        <AttributionBar
          apiClient={apiClient}
          attribution={brief.attribution}
          meetingId={meetingId}
          onChange={(change) => {
            briefCache.delete(meetingId);
            setBrief({ ...brief, attribution: change.attribution, card: change.card ?? brief.card });
            void props.onChanged();
          }}
          onNotice={(message, undoUntil, tone) =>
            props.onNotice(message, undoUntil ? { kind: "project", meetingId, until: undoUntil } : undefined, tone)
          }
          onProjectsChanged={props.onChanged}
          onSeek={(ms) => audio && player.play(audio, ms, title)}
          projects={projects}
        />
      )}
      {brief.evidence_quotes.length > 0 && (
        <Section title="证据原话">
          <ul className="graph-panel__quotes">
            {brief.evidence_quotes.map((entry) => (
              <li key={`${entry.source}-${entry.cue}`}>
                <strong>「{entry.cue}」{entry.count} 次</strong>
                {entry.quotes.map((quote) => (
                  <span className="graph-panel__quote" key={quote.start_ms}>
                    <PlayButton atMs={quote.start_ms} audioUrl={audio} label={title} player={player} />
                    {quote.text}
                  </span>
                ))}
              </li>
            ))}
          </ul>
        </Section>
      )}
      <Section title="一分钟摘要">
        <p>{brief.summary || (brief.meeting.has_minutes ? "纪要里没有摘要段" : "纪要还没写好")}</p>
      </Section>
      <Section title="定了什么">
        {brief.decisions.length ? (
          <ol className="graph-panel__list">
            {brief.decisions.map((item) => (
              <li key={item.text}>
                <PlayButton atMs={item.start_ms} audioUrl={audio} label={title} player={player} />
                {item.text}
              </li>
            ))}
          </ol>
        ) : (
          <p className="graph-panel__muted">{brief.decisions_note ?? "没有记下决议"}</p>
        )}
      </Section>
      <Section title={`任务${brief.tasks.length ? ` ${brief.tasks.length + brief.tasks_more}` : ""}`}>
        {brief.tasks.length ? (
          <ul className="graph-panel__list">
            {brief.tasks.map((task) => (
              <li className="graph-panel__task" key={task.id}>
                <span>
                  {task.title}
                  <small>{TASK_STATUS[task.status] ?? task.status}</small>
                </span>
                <PlayButton atMs={task.anchor_ms} audioUrl={audio} label={title} player={player} />
                {task.status === "pending_confirm" && (
                  <span className="graph-panel__actions">
                    <button
                      className="ghost-button"
                      disabled={busy}
                      onClick={() => void run(() => apiClient.confirmTask(task.id), `已确认「${task.title}」`)}
                      type="button"
                    >
                      确认
                    </button>
                    <button
                      className="text-button"
                      disabled={busy}
                      onClick={() => void run(() => apiClient.rejectTask(task.id), `已不要「${task.title}」`)}
                      type="button"
                    >
                      不要
                    </button>
                  </span>
                )}
              </li>
            ))}
            {brief.tasks_more > 0 && <li className="graph-panel__muted">还有 {brief.tasks_more} 条，去会议页看</li>}
          </ul>
        ) : (
          <p className="graph-panel__muted">没有未完成的任务</p>
        )}
      </Section>
      <Section title="关联的需求">
        <ul className="graph-panel__list">
          {brief.requirements.map((item) => (
            <li key={item.id}>
              <button className="text-button" onClick={() => props.onSelect(`r:${item.id}`)} type="button">
                {item.title}
              </button>
              {item.project_id !== brief.meeting.project_id && item.project_name && <small>在 {item.project_name}</small>}
              <button
                className="text-button"
                disabled={busy}
                onClick={() =>
                  void run(() => apiClient.removeRequirementMeeting(item.id, meetingId), `已解除和「${item.title}」的关联`)
                }
                type="button"
              >
                解除
              </button>
            </li>
          ))}
        </ul>
        {brief.meeting.project_id === graph.project.id &&
          (adding ? (
            <select
              aria-label="关联需求"
              autoFocus
              disabled={busy}
              onBlur={() => setAdding(false)}
              onChange={(event) => {
                const requirementId = event.target.value;
                const requirement = addable.find((item) => item.requirement_id === requirementId);
                setAdding(false);
                if (requirement) {
                  void run(
                    () => apiClient.addRequirementMeeting(requirementId, meetingId),
                    `已关联到「${requirement.title}」`,
                  );
                }
              }}
              value=""
            >
              <option value="">选一个需求 ▾</option>
              {addable.map((item) => (
                <option key={item.requirement_id} value={item.requirement_id}>
                  {item.title}
                </option>
              ))}
            </select>
          ) : (
            addable.length > 0 && (
              <button className="ghost-button" onClick={() => setAdding(true)} type="button">
                ＋ 关联需求
              </button>
            )
          ))}
      </Section>
      {brief.card && (
        <Section title="会议卡片">
          <MeetingCardStatus
            apiClient={apiClient}
            card={brief.card}
            meetingId={meetingId}
            onCardChange={(card: MeetingCard) => {
              briefCache.delete(meetingId);
              setBrief({ ...brief, card });
              void props.onChanged();
            }}
            onOpenProject={props.onOpenProject}
            projectId={brief.meeting.project_id}
            projectName={brief.meeting.project_name}
          />
        </Section>
      )}
      <p className="graph-panel__muted">{brief.files_note}</p>
    </>
  );
}

// ------------------------------------------------------------------ 门口

function DoorstepPanelBody({ props, item }: { props: GraphPanelProps; item: GraphDoorstep }) {
  const { apiClient, player } = props;
  const { brief, error } = useBrief(apiClient, item.meeting_id, props.version);
  const [first, second] = item.candidates;
  return (
    <>
      <p className="graph-panel__question">
        {second ? `${first.project_name} 还是 ${second.project_name}？` : `是 ${first.project_name} 的会吗？`}
      </p>
      <div className="graph-panel__candidates">
        {item.candidates.map((candidate) => {
          const cues = (brief?.attribution.evidence ?? []).filter(
            (entry) => entry.kind === "literal" && entry.project_id === candidate.project_id,
          );
          return (
            <div className="graph-panel__candidate" key={candidate.project_id}>
              <h3>
                <i style={{ background: candidate.project_color }} />
                {candidate.project_name}
              </h3>
              {cues.length ? (
                <ul>
                  {cues.slice(0, 3).map((entry) => (
                    <li key={`${entry.source}-${entry.cue}`}>
                      「{entry.cue}」{entry.count} 次
                      {(entry.anchors_ms ?? []).slice(0, 2).map((ms) => (
                        <PlayButton atMs={ms} audioUrl={brief?.meeting.audio_url} key={ms} label={item.title} player={player} />
                      ))}
                    </li>
                  ))}
                </ul>
              ) : (
                <p className="graph-panel__muted">{brief ? "没有字面线索，是 AI 读纪要猜的" : error || "正在读取…"}</p>
              )}
              <button
                className="primary-button"
                onClick={() => props.onAnswerDoorstep(item.meeting_id, candidate.project_id)}
                type="button"
              >
                归 {candidate.project_name}
              </button>
            </div>
          );
        })}
      </div>
      {item.reason && <p className="graph-panel__reason">AI：{item.reason}</p>}
      <button className="text-button" onClick={() => props.onAnswerDoorstep(item.meeting_id, null)} type="button">
        都不是
      </button>
    </>
  );
}

// ------------------------------------------------------------------ 需求

function RequirementPanelBody({ props, node }: { props: GraphPanelProps; node: Extract<LaidNode, { kind: "requirement" }> }) {
  const { apiClient, graph } = props;
  const requirement = node.data;
  const [files, setFiles] = useState<Record<number, RequirementFilesPayload | "error">>({});
  const [adding, setAdding] = useState(false);
  const [busy, setBusy] = useState(false);
  const meetingIds = graph.edges
    .filter((edge) => edge.kind === "discussion" && edge.requirement_id === requirement.requirement_id)
    .map((edge) => edge.from);
  const folders = graph.folders.filter(
    (folder) => folder.kind === "requirement_folder" && folder.requirement_id === requirement.requirement_id,
  );
  useEffect(() => {
    let active = true;
    for (const folder of folders) {
      if (folder.folder_id === undefined) continue;
      apiClient
        .requirementFolderFiles(requirement.requirement_id, folder.folder_id, { limit: 6 })
        .then((payload) => active && setFiles((current) => ({ ...current, [folder.folder_id!]: payload })))
        .catch(() => active && setFiles((current) => ({ ...current, [folder.folder_id!]: "error" })));
    }
    return () => {
      active = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [apiClient, requirement.requirement_id]);
  const linkedMeetings = new Set(
    graph.edges
      .filter((edge) => edge.kind === "discussion" && edge.requirement_id === requirement.requirement_id)
      .map((edge) => edge.meeting_id),
  );
  const addable = graph.meetings.filter((meeting) => !linkedMeetings.has(meeting.meeting_id));

  return (
    <>
      <p className="graph-panel__meta">
        {requirement.priority} · 进行中 · {requirement.open_tasks} 条未完成任务
        {requirement.stale_text ? ` · ${requirement.stale_text}` : ""}
      </p>
      <Section title="最近在哪些会上出现">
        {meetingIds.length ? (
          <ul className="graph-panel__list">
            {meetingIds.map((id) => {
              const target = props.layout.byId.get(id);
              const label =
                target?.kind === "meeting"
                  ? `${meetingDateLabel(target.data.date, graph.today)} ${target.data.title}`
                  : target?.kind === "collapsed"
                    ? target.data.label
                    : id;
              return (
                <li key={id}>
                  <button className="text-button" onClick={() => props.onSelect(id)} type="button">
                    {label}
                  </button>
                </li>
              );
            })}
          </ul>
        ) : (
          <p className="graph-panel__muted">还没有关联的会</p>
        )}
        {adding ? (
          <select
            aria-label="关联一场会"
            autoFocus
            disabled={busy}
            onBlur={() => setAdding(false)}
            onChange={async (event) => {
              const meetingId = event.target.value;
              setAdding(false);
              if (!meetingId) return;
              setBusy(true);
              try {
                await apiClient.addRequirementMeeting(requirement.requirement_id, meetingId);
                clearBriefCache();
                props.onNotice("已关联这场会");
                await props.onChanged();
              } catch (reason) {
                props.onNotice(reason instanceof Error ? reason.message : "关联失败", undefined, "error");
              } finally {
                setBusy(false);
              }
            }}
            value=""
          >
            <option value="">选一场会 ▾</option>
            {addable.map((meeting) => (
              <option key={meeting.meeting_id} value={meeting.meeting_id}>
                {meetingDateLabel(meeting.date, graph.today)} {meeting.title}
              </option>
            ))}
          </select>
        ) : (
          addable.length > 0 && (
            <button className="ghost-button" onClick={() => setAdding(true)} type="button">
              ＋ 关联一场会
            </button>
          )
        )}
      </Section>
      <Section title="文件夹">
        {folders.length ? (
          folders.map((folder) => {
            const payload = folder.folder_id !== undefined ? files[folder.folder_id] : undefined;
            return (
              <div className="graph-panel__folder" key={folder.id}>
                <strong>{folder.name}/</strong>
                {payload === "error" ? (
                  <p className="graph-panel__muted">读不了这个文件夹</p>
                ) : payload ? (
                  <ul className="graph-panel__files">
                    {payload.items.slice(0, 6).map((file) => (
                      <li key={file.relative_path}>{file.relative_path}</li>
                    ))}
                    {payload.total > 6 && <li className="graph-panel__muted">共 {payload.total} 个文件</li>}
                  </ul>
                ) : (
                  <p className="graph-panel__muted">正在读文件夹…</p>
                )}
              </div>
            );
          })
        ) : (
          <p className="graph-panel__muted">没有挂文件夹</p>
        )}
      </Section>
      <button className="ghost-button" onClick={() => props.onHighlight([node.id, ...meetingIds, ...folders.map((f) => f.id)])} type="button">
        只看这个需求
      </button>
    </>
  );
}

// ------------------------------------------------------------------ 线索词

function CueQuotes({
  props,
  meetingId,
  anchors,
  label,
}: {
  props: GraphPanelProps;
  meetingId: string;
  anchors: number[];
  label: string;
}) {
  const [quotes, setQuotes] = useState<QuotesPayload | null>(null);
  useEffect(() => {
    let active = true;
    if (anchors.length === 0) return;
    props.apiClient
      .meetingQuotes(meetingId, anchors.slice(0, 2))
      .then((value) => active && setQuotes(value))
      .catch(() => undefined);
    return () => {
      active = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [meetingId, anchors.join(",")]);
  const play = async (ms: number) => {
    try {
      const brief = await loadBrief(props.apiClient, meetingId);
      if (brief.meeting.audio_url) props.player.play(brief.meeting.audio_url, ms, label);
      else props.onNotice("这场会没有录音文件", undefined, "warning");
    } catch {
      props.onNotice("读不到这场会的录音", undefined, "error");
    }
  };
  return (
    <>
      {anchors.slice(0, 2).map((ms, index) => {
        const segment = quotes?.quotes[index]?.segments.find((item) => item.start_ms <= ms && item.end_ms >= ms)
          ?? quotes?.quotes[index]?.segments[0];
        return (
          <span className="graph-panel__quote" key={ms}>
            <button aria-label={`从 ${formatTime(ms)} 播放`} className="graph-play" onClick={() => void play(ms)} type="button">
              ▶ {formatTime(ms)}
            </button>
            {segment?.text ?? ""}
          </span>
        );
      })}
    </>
  );
}

function CuePanelBody({ props, cue }: { props: GraphPanelProps; cue: GraphCue }) {
  const { apiClient, graph } = props;
  const [detail, setDetail] = useState<CueTermDetail | null>(null);
  const [turnedOff, setTurnedOff] = useState(false);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    if (!cue.term_id) return;
    let active = true;
    apiClient
      .glossaryTermDetail(cue.term_id)
      .then((value) => active && setDetail(value))
      .catch(() => undefined);
    return () => {
      active = false;
    };
  }, [apiClient, cue.term_id]);

  const rows = detail
    ? detail.cue_meetings.map((item) => ({ ...item, label: `${meetingDateLabel(item.date, graph.today)} ${item.title}` }))
    : cue.meetings.map((item) => {
        const node = props.layout.byId.get(`m:${item.meeting_id}`);
        const label = node?.kind === "meeting" ? `${meetingDateLabel(node.data.date, graph.today)} ${node.data.title}` : item.meeting_id;
        return { ...item, label, only_cue: false, title: label, origin: null };
      });

  const turnOff = async () => {
    if (!cue.term_id) return;
    setBusy(true);
    try {
      await apiClient.updateGlossaryTerm(cue.term_id, { is_cue: false });
      setTurnedOff(true);
      props.onNotice(`以后不再用「${cue.text}」判断项目，已归好的会不动`);
      await props.onChanged();
    } catch (reason) {
      props.onNotice(reason instanceof Error ? reason.message : "操作失败", undefined, "error");
    } finally {
      setBusy(false);
    }
  };

  const onlyByThis = rows.filter((row) => row.only_cue);

  return (
    <>
      <p className="graph-panel__meta">
        来源：{cue.source === "term" ? "项目词" : "项目文件夹名"} · 这个词让 {rows.length} 场会归到这里
      </p>
      <ul className="graph-panel__list">
        {rows.map((row) => (
          <li className="graph-panel__cue-row" key={row.meeting_id}>
            <button className="text-button" onClick={() => props.onSelect(`m:${row.meeting_id}`)} type="button">
              {row.label}
            </button>
            <small>{row.count} 次</small>
            <CueQuotes anchors={row.anchors_ms} label={row.label} meetingId={row.meeting_id} props={props} />
          </li>
        ))}
      </ul>
      {cue.source === "term" && cue.term_id ? (
        turnedOff ? (
          <Section title="只靠这个词归进来的会">
            {onlyByThis.length ? (
              <ul className="graph-panel__list">
                {onlyByThis.map((row) => (
                  <li key={row.meeting_id}>
                    <button className="text-button" onClick={() => props.onSelect(`m:${row.meeting_id}`)} type="button">
                      {row.label}
                    </button>
                    <small>点开后在归属条上［对的］或［改］</small>
                  </li>
                ))}
              </ul>
            ) : (
              <p className="graph-panel__muted">没有只靠它归进来的会</p>
            )}
          </Section>
        ) : (
          <div className="graph-panel__actions">
            <button className="ghost-button" disabled={busy} onClick={() => void turnOff()} type="button">
              这个词不当线索
            </button>
            <button className="text-button" onClick={() => props.onOpenGlossary(graph.project.id)} type="button">
              编辑词条 →
            </button>
          </div>
        )
      ) : (
        <p className="graph-panel__muted">这是项目文件夹的名字。改文件夹名或换文件夹才会变。</p>
      )}
    </>
  );
}

// ------------------------------------------------------------------ 连线

function EdgePanelBody({ props, edge }: { props: GraphPanelProps; edge: GraphEdge }) {
  const { apiClient, graph, player } = props;
  const { brief, setBrief } = useBrief(
    apiClient,
    edge.kind === "attribution" || edge.kind === "write" ? edge.meeting_id ?? edge.from.slice(2) : null,
    props.version,
  );
  const [busy, setBusy] = useState(false);
  const meetingId = edge.meeting_id ?? (edge.from.startsWith("m:") ? edge.from.slice(2) : null);
  const who =
    edge.kind === "attribution"
      ? edge.source === "confirmed"
        ? "你确认过"
        : edge.source === "manual"
          ? "你归的"
          : edge.source === "review"
            ? "AI 拿不准，等你复核"
            : "AI 归的"
      : edge.kind === "discussion"
        ? "你关联的"
        : edge.kind === "cue"
          ? "AI 判断归属时数出来的"
          : "声档自动记的";

  return (
    <>
      <p className="graph-panel__meta">
        {EDGE_KIND[edge.kind]} · {who}
      </p>
      {edge.label && <p>{edge.label}</p>}
      {edge.kind === "attribution" && brief && meetingId && (
        <AttributionBar
          apiClient={apiClient}
          attribution={brief.attribution}
          meetingId={meetingId}
          onChange={(change) => {
            briefCache.delete(meetingId);
            setBrief({ ...brief, attribution: change.attribution });
            void props.onChanged();
          }}
          onNotice={(message, undoUntil, tone) =>
            props.onNotice(message, undoUntil ? { kind: "project", meetingId, until: undoUntil } : undefined, tone)
          }
          onProjectsChanged={props.onChanged}
          onSeek={(ms) => brief.meeting.audio_url && player.play(brief.meeting.audio_url, ms, brief.meeting.title)}
          projects={props.projects}
        />
      )}
      {edge.kind === "cue" && edge.meeting_id && (
        <CueQuotes anchors={edge.anchors_ms ?? []} label={edge.meeting_id} meetingId={edge.meeting_id} props={props} />
      )}
      {edge.kind === "discussion" && edge.requirement_id && edge.meeting_id && (
        <button
          className="ghost-button"
          disabled={busy}
          onClick={async () => {
            setBusy(true);
            try {
              await apiClient.removeRequirementMeeting(edge.requirement_id!, edge.meeting_id!);
              clearBriefCache();
              props.onNotice("已解除这条关联");
              props.onClose();
              await props.onChanged();
            } catch (reason) {
              props.onNotice(reason instanceof Error ? reason.message : "解除失败", undefined, "error");
            } finally {
              setBusy(false);
            }
          }}
          type="button"
        >
          解除
        </button>
      )}
      {graph && <p className="graph-panel__muted">两头：{describe(props.layout, edge.from)} ↔ {describe(props.layout, edge.to)}</p>}
    </>
  );
}

function describe(layout: StarLayout, id: string): string {
  const node = layout.byId.get(id);
  if (!node) return id;
  if (node.kind === "meeting") return node.data.title;
  if (node.kind === "requirement") return node.data.title;
  if (node.kind === "cue") return node.data.text;
  if (node.kind === "project") return node.data.name;
  if (node.kind === "folder") return node.data.name;
  if (node.kind === "beacon") return node.data.project_name;
  return node.label;
}

// ------------------------------------------------------------------ 简单列表

function CollapsedBody({ props, groupId }: { props: GraphPanelProps; groupId: string }) {
  const [payload, setPayload] = useState<CollapsedPayload | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    let active = true;
    props.apiClient
      .graphCollapsed(props.graph.project.id, groupId, props.graph.window.effective)
      .then((value) => active && setPayload(value))
      .catch((reason: unknown) => active && setError(reason instanceof Error ? reason.message : "读取失败"));
    return () => {
      active = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [groupId, props.version]);
  if (error) return <p className="graph-panel__error">{error}</p>;
  if (!payload) return <p className="graph-panel__muted">正在读取…</p>;
  return (
    <>
      {payload.months.map((month) => (
        <Section key={month.month} title={`${month.label} · ${month.meetings.length} 场`}>
          <ul className="graph-panel__list">
            {month.meetings.map((meeting) => (
              <li key={meeting.meeting_id}>
                <button className="text-button" onClick={() => props.onOpenMeeting(meeting.meeting_id)} type="button">
                  {meetingDateLabel(meeting.date, props.graph.today)} {meeting.title}
                </button>
                {meeting.open_tasks > 0 && <small>{meeting.open_tasks} 条未完成</small>}
              </li>
            ))}
          </ul>
        </Section>
      ))}
    </>
  );
}

function RootFolderBody({ props, rootId, path }: { props: GraphPanelProps; rootId: number; path: string }) {
  const [payload, setPayload] = useState<ProjectSubfoldersPayload | null>(null);
  useEffect(() => {
    let active = true;
    props.apiClient
      .projectMaterialSubfolders(props.graph.project.id)
      .then((value) => active && setPayload(value))
      .catch(() => undefined);
    return () => {
      active = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [rootId]);
  const root = payload?.roots.find((item) => item.root_id === rootId);
  return (
    <>
      <CopyPath path={path} props={props} />
      {root ? (
        root.exists ? (
          <ul className="graph-panel__files">
            {root.folders.slice(0, 12).map((folder) => (
              <li key={folder.path}>
                {folder.name}/ <small>{folder.file_count} 个文件</small>
              </li>
            ))}
          </ul>
        ) : (
          <p className="graph-panel__muted">这个文件夹现在找不到（资料盘未连接或被移走了）</p>
        )
      ) : (
        <p className="graph-panel__muted">正在读文件夹…</p>
      )}
    </>
  );
}

function CopyPath({ props, path }: { props: GraphPanelProps; path: string }) {
  return (
    <p className="graph-panel__path">
      <code>{path}</code>
      <button
        className="text-button"
        onClick={async () => {
          try {
            await copyText(path);
            props.onNotice("已复制路径");
          } catch {
            props.onNotice("复制失败，请手动选中路径", undefined, "error");
          }
        }}
        type="button"
      >
        复制路径
      </button>
    </p>
  );
}

// ------------------------------------------------------------------ 外壳

function titleOf(node: LaidNode | undefined, edge: GraphEdge | undefined, props: GraphPanelProps): string {
  if (edge) return "为什么相连";
  if (!node) return "";
  switch (node.kind) {
    case "project":
      return node.data.name;
    case "meeting":
      return node.data.title;
    case "doorstep":
      return node.data.title;
    case "requirement":
      return node.data.title;
    case "cue":
      return node.data.text;
    case "folder":
      return node.data.name;
    case "beacon":
      return node.data.label;
    case "collapsed":
      return node.data.label;
    case "loose":
      return `散放 ${props.roots?.loose.count ?? "…"} 个文件`;
    default:
      return node.label;
  }
}

export function GraphPanel(props: GraphPanelProps) {
  const { layout, graph, selectedId } = props;
  const node = layout.byId.get(selectedId);
  const edge = node ? undefined : graph.edges.find((item) => item.id === selectedId);
  const primary = useCallback((): { label: string; run: () => void } | null => {
    if (!node) return null;
    if (node.kind === "meeting") return { label: "打开会议页 →", run: () => props.onOpenMeeting(node.data.meeting_id) };
    if (node.kind === "doorstep") return { label: "打开会议页 →", run: () => props.onOpenMeeting(node.data.meeting_id) };
    if (node.kind === "requirement")
      return { label: "打开需求页 →", run: () => props.onOpenRequirement(node.data.requirement_id) };
    if (node.kind === "doorstep_more" && props.onOpenAttributionReview)
      return { label: "去资料库看 →", run: props.onOpenAttributionReview };
    return null;
  }, [node, props]);
  const action = primary();

  let body: ReactNode = null;
  if (edge) body = <EdgePanelBody edge={edge} props={props} />;
  else if (node) {
    switch (node.kind) {
      case "meeting":
        body = <MeetingPanelBody meetingId={node.data.meeting_id} props={props} />;
        break;
      case "doorstep":
        body = <DoorstepPanelBody item={node.data} props={props} />;
        break;
      case "requirement":
        body = <RequirementPanelBody node={node} props={props} />;
        break;
      case "cue":
        body = <CuePanelBody cue={node.data} props={props} />;
        break;
      case "collapsed":
        body = <CollapsedBody groupId={node.id} props={props} />;
        break;
      case "project":
        body = (
          <>
            <p className="graph-panel__meta">
              {graph.window.days ? `最近 ${graph.window.days} 天` : "全部"} {graph.project.meeting_count} 场会 · 进行中的需求{" "}
              {graph.requirements.length + (graph.requirements_more?.count ?? 0)} 个
            </p>
            <p className="graph-panel__muted">位置按类型和时间排：左边是会议，右边是材料，上面是进行中的需求，下面是线索词；离中心越近越新。</p>
          </>
        );
        break;
      case "folder":
        body =
          node.data.kind === "root" && node.data.root_id !== undefined ? (
            <RootFolderBody path={node.data.path} props={props} rootId={node.data.root_id} />
          ) : node.data.kind === "cards" ? (
            <>
              <p className="graph-panel__meta">
                已写 {node.data.written ?? 0} 张 · 停了 {node.data.stopped ?? 0} 张 · 在等 {node.data.waiting ?? 0} 张
              </p>
              <CopyPath path={node.data.path} props={props} />
            </>
          ) : (
            <>
              <CopyPath path={node.data.path} props={props} />
              {node.data.requirement_id && (
                <button className="text-button" onClick={() => props.onSelect(`r:${node.data.requirement_id}`)} type="button">
                  看它所属的需求
                </button>
              )}
            </>
          );
        break;
      case "loose":
        body = props.roots ? (
          <ul className="graph-panel__files">
            {props.roots.loose.recent.map((file) => (
              <li key={file.path}>
                {file.name} <small>{file.mtime.slice(0, 10)}</small>
              </li>
            ))}
          </ul>
        ) : (
          <p className="graph-panel__muted">正在读资料盘…</p>
        );
        break;
      case "beacon":
        body = (
          <ul className="graph-panel__list">
            {node.data.items.map((item, index) => (
              <li key={`${item.kind}-${index}`}>
                <span>{item.text}</span>
                <span className="graph-panel__actions">
                  {(item.kind === "meeting_requirement" || item.kind === "requirement_meeting") && item.requirement_id && item.meeting_id && (
                    <button
                      className="text-button"
                      onClick={async () => {
                        try {
                          await props.apiClient.removeRequirementMeeting(item.requirement_id!, item.meeting_id!);
                          clearBriefCache();
                          props.onNotice("已解除");
                          await props.onChanged();
                        } catch (reason) {
                          props.onNotice(reason instanceof Error ? reason.message : "解除失败", undefined, "error");
                        }
                      }}
                      type="button"
                    >
                      解除
                    </button>
                  )}
                  {item.meeting_id && (
                    <button className="text-button" onClick={() => props.onOpenMeeting(item.meeting_id!)} type="button">
                      去看
                    </button>
                  )}
                </span>
              </li>
            ))}
          </ul>
        );
        break;
      case "requirement_more":
        body = <p className="graph-panel__muted">还有 {node.data.count} 个进行中的需求放不下，去项目的清单视图看。</p>;
        break;
      case "folder_more":
        body = (
          <ul className="graph-panel__files">
            {node.data.paths.map((path) => (
              <li key={path}>{path}</li>
            ))}
          </ul>
        );
        break;
      case "doorstep_more":
        body = <p className="graph-panel__muted">还有 {node.data.count} 场可能是这个项目的会，在资料库「待你选」里。</p>;
        break;
      default:
        body = null;
    }
  }

  return (
    <aside aria-label="详情面板" className="graph-panel">
      <header className="graph-panel__head">
        <div>
          <span className="graph-panel__kind">{edge ? `连线 · ${EDGE_KIND[edge.kind]}` : node ? KIND_LABEL[node.kind] : ""}</span>
          <h2>{titleOf(node, edge, props)}</h2>
        </div>
        <div className="graph-panel__nav">
          {props.canGoBack && (
            <button className="text-button" onClick={props.onBack} type="button">
              ← 上一个
            </button>
          )}
          <button aria-label="关闭面板" className="graph-panel__close" onClick={props.onClose} type="button">
            ✕
          </button>
        </div>
      </header>
      {(node?.kind === "meeting" || node?.kind === "doorstep" || node?.kind === "cue" || edge?.kind === "attribution" || edge?.kind === "cue") &&
        props.playerNode}
      <div className="graph-panel__body">{body}</div>
      {action && (
        <footer className="graph-panel__foot">
          {node?.kind === "meeting" && props.onExpandMeeting && (
            <button
              className="ghost-button"
              onClick={() => props.onExpandMeeting?.(node.data.meeting_id)}
              title="在画布上展开：录音条、决议和任务按时间点对齐（也可以双击会议）"
              type="button"
            >
              展开这场会
            </button>
          )}
          <button className="primary-button" onClick={action.run} type="button">
            {action.label}
          </button>
        </footer>
      )}
    </aside>
  );
}
