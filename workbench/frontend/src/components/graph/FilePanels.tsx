/* 关系图面板里第二期的几类：会上提到的文件（2d）、「像是新需求」和等补建的文件夹（2c）；
   第三期文件面板加预览、交付物、［标为交付物 ▾］（3g） */
import { useEffect, useId, useState } from "react";

import { formatMonthDayClock, formatTime } from "../../format";
import type { MaterialFilePreview, NameCandidatesPayload, Task } from "../../types";
import { PreviewBlock } from "../MaterialPreview";
import { NewNamePrompt } from "../NewNamePrompt";
import type { GraphPanelProps } from "./GraphPanel";
import type { FilesState, GraphEdge, GraphFileDetail, GraphFolder, GraphSuggestedRequirement, MeetingBrief } from "./graphTypes";
import { meetingDateLabel } from "./layout";
import { CopyPath, PlayButton, Section, TASK_STATUS, loadBrief, localUndoUntil, playMeetingAt } from "./panelParts";

/** ［标为交付物 ▾］最多列这么多个任务 */
export const DELIVERABLE_TASKS_MAX = 30;
const OPEN_TASK_STATUSES = "pending_confirm,confirmed,in_progress";

/** 会议面板里文件列表为空时的说法（盘没插时列表照常显示，这句写在上面） */
export const FILES_STATE_TEXT: Record<FilesState, string> = {
  done: "这场会没提到项目文件夹里的文件名",
  indexing: "文件名还在认，认完后这里列出会上提到的文件",
  offline: "资料盘未连接，先按上次认得的算",
  no_project: "这场会还没归项目",
  no_root: "这个项目还没挂文件夹",
};

type MentionRow = GraphFileDetail["meetings"][number];

function errorText(reason: unknown, fallback: string) {
  return reason instanceof Error && reason.message ? reason.message : fallback;
}

/** 「会上说『报价单』3 次」；只在纪要里写到的是「纪要里写到『报价单』」 */
function saidText(item: { needle: string; count: number; source: string }) {
  return item.source === "minutes" ? `纪要里写到『${item.needle}』` : `会上说『${item.needle}』${item.count} 次`;
}

/**
 * ［不是这份文件］：立即生效，提示里有［撤销］（走画布的撤销栈，⌘Z 也能撤）。
 * 这份文件可能因此从图上下去，所以先回到那场会再刷新。
 */
async function rejectMention(props: GraphPanelProps, meetingId: string, stemKey: string, fileName: string) {
  await props.apiClient.rejectFileMention(meetingId, stemKey);
  props.onNotice("已标成不是这份文件", { kind: "mention", meetingId, stemKey, name: fileName, until: localUndoUntil() });
  if (props.layout.byId.has(`m:${meetingId}`)) props.onSelect(`m:${meetingId}`);
  else props.onClose();
  await props.onChanged();
}

// ------------------------------------------------------------------ 会议面板里的文件列表

export function MeetingFiles({ props, brief }: { props: GraphPanelProps; brief: MeetingBrief }) {
  const files = brief.files ?? [];
  const state = brief.files_state ?? "done";
  const audio = brief.meeting.audio_url;
  return (
    <Section title={files.length ? `会上提到的文件 ${files.length}` : "会上提到的文件"}>
      {(files.length === 0 || state === "offline") && <p className="graph-panel__muted">{FILES_STATE_TEXT[state]}</p>}
      {files.length > 0 && (
        <ul aria-label="会上提到的文件" className="graph-panel__list graph-panel__mentions">
          {files.map((file) => (
            <li className={file.generic ? "is-generic" : undefined} key={`${file.stem_key}-${file.file_id}`}>
              <button className="text-button" onClick={() => props.onSelect(`file:${file.file_id}`)} title={file.rel_path} type="button">
                {file.name}
              </button>
              <small>{saidText(file)}</small>
              <PlayButton atMs={file.first_ms} audioUrl={audio} label={brief.meeting.title} player={props.player} />
            </li>
          ))}
        </ul>
      )}
    </Section>
  );
}

// ------------------------------------------------------------------ 文件面板

function useFileDetail(props: GraphPanelProps, fileId: number) {
  const [state, setState] = useState<{ id: number; payload: GraphFileDetail | null; error: string }>({
    id: fileId,
    payload: null,
    error: "",
  });
  const canLoad = typeof props.apiClient.getGraphFile === "function";
  useEffect(() => {
    if (!canLoad) return;
    let active = true;
    props.apiClient
      .getGraphFile(fileId)
      .then((payload) => active && setState({ id: fileId, payload, error: "" }))
      .catch((reason: unknown) => active && setState({ id: fileId, payload: null, error: errorText(reason, "读不到这份文件") }));
    return () => {
      active = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [canLoad, fileId, props.version]);
  const current = state.id === fileId ? state : { payload: null, error: "" };
  return { payload: current.payload, error: canLoad ? current.error : "读不到这份文件" };
}

/** 一场会的那一行：会名、次数、第一次说到的原话 ▶ */
function MentionQuote({ props, row }: { props: GraphPanelProps; row: MentionRow }) {
  if (row.first_ms === null) return null;
  return (
    <span className="graph-panel__quote">
      <button
        aria-label={`从 ${formatClockHms(row.first_ms)} 播放「${row.title}」`}
        className="graph-play"
        onClick={() => void playMeetingAt(props, row.meeting_id, row.first_ms as number, row.title)}
        type="button"
      >
        ▶ {formatClockHms(row.first_ms)}
      </button>
      {row.quote}
    </span>
  );
}

/** 「提到」的时间用带小时的格式，和线上的字一致 */
function formatClockHms(ms: number) {
  return formatTime(ms, true);
}

/** 文件面板顶上的预览：状态那一句加预览（?parts=preview，和预览抽屉同一个 PreviewBlock）；旧后端没有时不显示 */
function FilePreview({ props, fileId }: { props: GraphPanelProps; fileId: number }) {
  const [state, setState] = useState<{ id: number; data: MaterialFilePreview | null }>({ id: fileId, data: null });
  const canLoad = typeof props.apiClient.getMaterialPreview === "function";
  useEffect(() => {
    if (!canLoad) return;
    let active = true;
    props.apiClient
      .getMaterialPreview(fileId, "preview")
      .then((data) => active && setState({ id: fileId, data }))
      .catch(() => active && setState({ id: fileId, data: null }));
    return () => {
      active = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [canLoad, fileId, props.version]);
  const data = state.id === fileId ? state.data : null;
  if (!data) return null;
  return <PreviewBlock data={data} onOpenMeeting={props.onOpenMeeting} player={props.player} />;
}

/** 最近的会的任务排前面（会的日期新的在前，没有会的按建的时间） */
export function tasksForPicker(tasks: Task[], query: string): Task[] {
  const needle = query.trim().toLocaleLowerCase();
  return [...tasks]
    .sort(
      (a, b) =>
        (b.meeting_recording_date ?? "").localeCompare(a.meeting_recording_date ?? "") ||
        b.created_at.localeCompare(a.created_at),
    )
    .filter((task) => !needle || task.title.toLocaleLowerCase().includes(needle))
    .slice(0, DELIVERABLE_TASKS_MAX);
}

/**
 * ［标为交付物 ▾］：列本项目没做完的任务（待确认、已确认、进行中），能打字筛；选了立即生效，
 * 不顺带把任务标成完成，提示里有［撤销］（10 分钟内，⌘Z 也能撤）。
 */
function MarkDeliverable({ props, fileId, fileName }: { props: GraphPanelProps; fileId: number; fileName: string }) {
  const [open, setOpen] = useState(false);
  const [tasks, setTasks] = useState<Task[] | null>(null);
  const [error, setError] = useState("");
  const [query, setQuery] = useState("");
  const [busy, setBusy] = useState(false);
  const listId = useId();

  useEffect(() => {
    if (!open || tasks) return;
    let active = true;
    props.apiClient
      .tasks({ project_id: props.graph.project.id, status: OPEN_TASK_STATUSES, limit: 500 })
      .then((payload) => active && setTasks(payload.items))
      .catch((reason: unknown) => active && setError(errorText(reason, "任务读取失败")));
    return () => {
      active = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, tasks]);

  const mark = async (task: Task) => {
    setBusy(true);
    try {
      const detail = await props.apiClient.addDeliverable(task.id, { file_id: fileId });
      setOpen(false);
      props.onNotice(
        `已标为『${task.title}』的交付物`,
        detail.deliverable_id
          ? {
              kind: "deliverable",
              taskId: task.id,
              deliverableId: detail.deliverable_id,
              taskTitle: task.title,
              name: fileName,
              until: localUndoUntil(),
            }
          : undefined,
      );
      await props.onChanged();
    } catch (reason) {
      props.onNotice(errorText(reason, "没标成"), undefined, "error");
    } finally {
      setBusy(false);
    }
  };

  const shown = tasks ? tasksForPicker(tasks, query) : [];
  return (
    <div className="graph-panel__mark">
      <button
        aria-controls={open ? listId : undefined}
        aria-expanded={open}
        className="ghost-button"
        disabled={busy}
        onClick={() => setOpen((value) => !value)}
        type="button"
      >
        标为交付物 ▾
      </button>
      {open && (
        <div className="graph-panel__picker" id={listId}>
          <input
            aria-label="筛选任务"
            autoFocus
            maxLength={100}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="筛选任务"
            type="search"
            value={query}
          />
          {error ? (
            <p className="graph-panel__error">{error}</p>
          ) : !tasks ? (
            <p className="graph-panel__muted">正在读任务…</p>
          ) : shown.length === 0 ? (
            <p className="graph-panel__muted">{tasks.length ? "没有对得上的任务" : "这个项目没有没做完的任务"}</p>
          ) : (
            <ul aria-label="选一个任务" className="graph-panel__list">
              {shown.map((task) => (
                <li key={task.id}>
                  <button className="text-button" disabled={busy} onClick={() => void mark(task)} type="button">
                    {task.title}
                  </button>
                  <small>
                    {TASK_STATUS[task.status] ?? task.status}
                    {task.meeting_title ? ` · ${task.meeting_title}` : ""}
                  </small>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </div>
  );
}

export function FilePanelBody({
  props,
  fileId,
  fromMeetingId,
}: {
  props: GraphPanelProps;
  fileId: number;
  /** 从哪场会点进来的：那一行给［不是这份文件］，［换成这份］只换这一场 */
  fromMeetingId: string | null;
}) {
  const { payload, error } = useFileDetail(props, fileId);
  const [busy, setBusy] = useState(false);

  if (error) return <p className="graph-panel__error">{error}</p>;
  if (!payload) return <p className="graph-panel__muted">正在读取这份文件…</p>;
  const { file } = payload;
  const active = payload.meetings.filter((row) => row.status === "active");
  const rejected = payload.meetings.filter((row) => row.status === "rejected");
  const canReveal = Boolean(props.roots?.can_reveal) && !file.gone;
  const fromRow = fromMeetingId ? active.find((row) => row.meeting_id === fromMeetingId) ?? null : null;
  // ［换成这份］：从某场会点进来时只换那一场，否则提到这份文件的会一起换
  const pickTargets = fromRow ? [fromRow] : active;

  const run = async (work: () => Promise<void>, fallback: string) => {
    setBusy(true);
    try {
      await work();
    } catch (reason) {
      props.onNotice(errorText(reason, fallback), undefined, "error");
    } finally {
      setBusy(false);
    }
  };

  const openMeeting = (meetingId: string) => {
    if (props.layout.byId.has(`m:${meetingId}`)) props.onSelect(`m:${meetingId}`);
    else props.onOpenMeeting(meetingId);
  };

  const pick = (sibling: GraphFileDetail["siblings"][number]) =>
    run(async () => {
      for (const row of pickTargets) {
        await props.apiClient.pickFileMention(row.meeting_id, row.stem_key, sibling.id);
      }
      props.onNotice(
        pickTargets.length > 1 ? `已把 ${pickTargets.length} 场会换成「${sibling.name}」` : `已换成「${sibling.name}」`,
      );
      // 这份文件可能不再有会提到；回到那场会看换好的列表
      const back = pickTargets.length === 1 ? pickTargets[0].meeting_id : null;
      if (back && props.layout.byId.has(`m:${back}`)) props.onSelect(`m:${back}`);
      else props.onClose();
      await props.onChanged();
    }, "没换成");

  const restore = (row: MentionRow) =>
    run(async () => {
      await props.apiClient.restoreFileMention(row.meeting_id, row.stem_key);
      props.onNotice(`已撤销，「${row.title}」又连回这份文件`);
      await props.onChanged();
    }, "撤销失败");

  const deliverables = payload.deliverables;
  // 旧后端的文件面板没有交付物：不显示这一节和［标为交付物］
  const canMark = deliverables !== undefined && typeof props.apiClient.addDeliverable === "function" && !file.gone;

  return (
    <>
      <FilePreview fileId={fileId} props={props} />
      <dl className="graph-panel__facts">
        <div>
          <dt>所在文件夹</dt>
          <dd title={file.folder_path}>{file.folder_path}</dd>
        </div>
        <div>
          <dt>修改时间</dt>
          <dd>{file.modified_at ? formatMonthDayClock(file.modified_at) : "—"}</dd>
        </div>
      </dl>
      {file.gone && <p className="graph-panel__warn">这份文件已经不在了（挪走、改名或删掉了），先按上次认得的算</p>}
      <Section title={`在 ${payload.active_meetings} 场会上被提到`}>
        {active.length ? (
          <ul className="graph-panel__list">
            {active.map((row) => (
              <li className="graph-panel__cue-row" key={row.meeting_id}>
                <button className="text-button" onClick={() => openMeeting(row.meeting_id)} type="button">
                  {meetingDateLabel(row.date, props.graph.today)} {row.title}
                </button>
                <small>{row.source === "minutes" ? "纪要里写到" : `${row.count} 次`}</small>
                <MentionQuote props={props} row={row} />
                {row.meeting_id === fromMeetingId && (
                  <span className="graph-panel__actions">
                    <button
                      className="ghost-button"
                      disabled={busy}
                      onClick={() => void run(() => rejectMention(props, row.meeting_id, row.stem_key, file.name), "没标成")}
                      type="button"
                    >
                      不是这份文件
                    </button>
                  </span>
                )}
              </li>
            ))}
          </ul>
        ) : (
          <p className="graph-panel__muted">现在没有会连到这份文件</p>
        )}
        {rejected.length > 0 && (
          <ul className="graph-panel__list graph-panel__rejected">
            {rejected.map((row) => (
              <li key={row.meeting_id}>
                <small>你标过「{row.title}」说的不是这份文件</small>
                <button className="text-button" disabled={busy} onClick={() => void restore(row)} type="button">
                  撤销
                </button>
              </li>
            ))}
          </ul>
        )}
      </Section>
      {payload.siblings.length > 0 && (
        <Section title={`同名的还有 ${payload.siblings.map((item) => item.name).join("、")}`}>
          <ul className="graph-panel__list">
            {payload.siblings.map((sibling) => (
              <li key={sibling.id}>
                <span className="graph-panel__sibling" title={sibling.rel_path}>
                  {sibling.name}
                  {sibling.modified_at && <small>{formatMonthDayClock(sibling.modified_at)} 改过</small>}
                </span>
                <span className="graph-panel__actions">
                  <button
                    className="ghost-button"
                    disabled={busy || pickTargets.length === 0}
                    onClick={() => void pick(sibling)}
                    type="button"
                  >
                    换成这份
                  </button>
                </span>
              </li>
            ))}
          </ul>
        </Section>
      )}
      {deliverables !== undefined && (
        <Section title={deliverables.length ? `是 ${deliverables.length} 个任务的交付物` : "交付物"}>
          {deliverables.length ? (
            <ul aria-label="是哪些任务的交付物" className="graph-panel__list">
              {deliverables.map((item) => (
                <li key={item.deliverable_id}>
                  <span>{item.title}</span>
                  <small>{TASK_STATUS[item.status] ?? item.status}</small>
                </li>
              ))}
            </ul>
          ) : (
            <p className="graph-panel__muted">还不是哪个任务的交付物</p>
          )}
        </Section>
      )}
      <div className="graph-panel__file-actions">
        <CopyPath apiClient={props.apiClient} canReveal={canReveal} onNotice={props.onNotice} path={file.path} />
        {canMark && <MarkDeliverable fileId={file.id} fileName={file.name} props={props} />}
      </div>
    </>
  );
}

// ------------------------------------------------------------------ 「提到」线

export function MentionEdgeBody({ props, edge }: { props: GraphPanelProps; edge: GraphEdge }) {
  const fileId = Number(edge.to.replace(/^file:/, ""));
  const meetingId = edge.meeting_id ?? edge.from.replace(/^m:/, "");
  const { payload } = useFileDetail(props, fileId);
  const [busy, setBusy] = useState(false);
  const row = payload?.meetings.find((item) => item.meeting_id === meetingId) ?? null;
  const fileName = payload?.file.name ?? describeFile(props, edge.to);
  const stemKey = edge.stem_key ?? row?.stem_key ?? "";
  const needle = edge.needle ?? row?.needle ?? "";

  return (
    <>
      <p className="graph-panel__question">
        {saidText({ needle, count: edge.count ?? row?.count ?? 0, source: edge.source ?? row?.source ?? "transcript" })}
      </p>
      {row ? (
        <MentionQuote props={props} row={row} />
      ) : (
        (edge.anchors_ms ?? []).slice(0, 1).map((ms) => (
          <span className="graph-panel__quote" key={ms}>
            <button
              aria-label={`从 ${formatClockHms(ms)} 播放`}
              className="graph-play"
              onClick={() => void playMeetingAt(props, meetingId, ms)}
              type="button"
            >
              ▶ {formatClockHms(ms)}
            </button>
          </span>
        ))
      )}
      <div className="graph-panel__actions graph-panel__actions--start">
        <button
          className="ghost-button"
          disabled={busy || !stemKey}
          onClick={async () => {
            setBusy(true);
            try {
              await rejectMention(props, meetingId, stemKey, fileName);
            } catch (reason) {
              props.onNotice(errorText(reason, "没标成"), undefined, "error");
            } finally {
              setBusy(false);
            }
          }}
          type="button"
        >
          不是这份文件
        </button>
      </div>
    </>
  );
}

function describeFile(props: GraphPanelProps, id: string) {
  const node = props.layout.byId.get(id);
  return node?.kind === "file" ? node.data.name : "这份文件";
}

// ------------------------------------------------------------------ 「像是新需求」的虚线

export function SuggestedEdgeBody({ props, edge }: { props: GraphPanelProps; edge: GraphEdge }) {
  const meetingId = edge.meeting_id ?? edge.from.replace(/^m:/, "");
  const item = props.graph.suggested_requirements?.find((entry) => entry.id === edge.to);
  const word = item?.spoken[0] ?? edge.name ?? item?.name ?? "";
  const canLoad = typeof props.apiClient.nameCandidates === "function";
  const [payload, setPayload] = useState<NameCandidatesPayload | null>(null);
  const [failed, setFailed] = useState(!canLoad);
  useEffect(() => {
    if (!canLoad) return;
    let active = true;
    setPayload(null);
    props.apiClient
      .nameCandidates(meetingId)
      .then((value) => active && setPayload(value))
      .catch(() => active && setFailed(true));
    return () => {
      active = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [canLoad, meetingId, props.version]);
  // 次数和 ▶ 用候选名里会上说过的那一个（先找同一个词，再用第一个候选）
  const spoken =
    payload?.candidates.find((candidate) => candidate.name === word && candidate.spoken)?.spoken ??
    payload?.candidates[0]?.spoken ??
    null;

  return (
    <>
      <p className="graph-panel__question">
        {spoken ? `会上说『${word}』${spoken.count} 次` : `会上说『${word}』`}
        {spoken && (
          <button
            aria-label={`从 ${formatClockHms(spoken.first_ms)} 播放`}
            className="graph-play graph-play--inline"
            onClick={() => void playMeetingAt(props, meetingId, spoken.first_ms)}
            type="button"
          >
            ▶ {formatClockHms(spoken.first_ms)}
          </button>
        )}
      </p>
      {!spoken && (payload || failed) && (
        <p className="graph-panel__muted">AI 读纪要觉得这场会主要在谈『{item?.name ?? word}』，逐字稿里没数到这个词</p>
      )}
      {item && (
        <button className="text-button" onClick={() => props.onSelect(item.id)} type="button">
          像是新需求『{item.name}』：建成需求或不算 →
        </button>
      )}
    </>
  );
}

// ------------------------------------------------------------------ 「像是新需求」节点

export function SuggestedRequirementBody({ props, item }: { props: GraphPanelProps; item: GraphSuggestedRequirement }) {
  const { apiClient, graph } = props;
  // 对这组里最近的一场会（meeting_ids[0]）
  const meetingId = item.meeting_ids[0];
  const [title, setTitle] = useState("");
  useEffect(() => {
    let active = true;
    loadBrief(apiClient, meetingId)
      .then((brief) => active && setTitle(brief.meeting.title))
      .catch(() => undefined);
    return () => {
      active = false;
    };
  }, [apiClient, meetingId]);

  const assign = async (projectId: string, others: string[]) => {
    try {
      for (const id of [meetingId, ...others]) {
        await apiClient.updateMeeting(id, { project_id: projectId });
      }
      const name = props.projects.find((project) => project.id === projectId)?.name ?? "所选项目";
      props.onNotice(`已改到 ${name}${others.length ? `；同名的另 ${others.length} 场会也改过去了` : ""}`);
      await props.onChanged();
    } catch (reason) {
      props.onNotice(errorText(reason, "改归属失败"), undefined, "error");
    }
  };

  return (
    <>
      {title && (
        <p className="graph-panel__meta">
          最近一场：
          <button className="text-button" onClick={() => props.onSelect(`m:${meetingId}`)} type="button">
            {meetingDateLabel(item.last_day, graph.today)} {title}
          </button>
        </p>
      )}
      <NewNamePrompt
        apiClient={apiClient}
        hint={{
          kind: "requirement",
          name: item.name,
          spoken: item.spoken,
          project_id: graph.project.id,
          project_name: graph.project.name,
        }}
        key={item.id}
        meetingId={meetingId}
        onAssign={assign}
        onDismissed={() => void props.onChanged()}
        onNotice={(message, undoUntil, tone, actions) =>
          props.onNotice(message, undoUntil ? { kind: "project", meetingId, until: undoUntil } : undefined, tone, actions)
        }
        onOpenRequirement={props.onOpenRequirement}
        onPlayMeeting={(otherId, ms) => void playMeetingAt(props, otherId, ms)}
        onProjectsChanged={props.onChanged}
        onReload={async () => {
          await props.onChanged();
        }}
        onSeek={(ms) => void playMeetingAt(props, meetingId, ms)}
        projects={props.projects}
      />
    </>
  );
}

// ------------------------------------------------------------------ 等补建的文件夹

export function PendingFolderBody({ folder }: { folder: GraphFolder }) {
  return folder.state === "stopped" ? (
    <>
      <p className="graph-panel__meta">{folder.reason || "文件夹没建成"}</p>
      <p className="graph-panel__path">
        <code>{folder.path}</code>
      </p>
      <p className="graph-panel__muted">在清单视图的材料根目录里可以重新选位置，或者不建了、以后自己挂文件夹。</p>
    </>
  ) : (
    <p className="graph-panel__meta">资料盘未连接，插上后自动建 {folder.path}</p>
  );
}
