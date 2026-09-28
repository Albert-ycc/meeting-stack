import { useEffect, useState, type ReactNode } from "react";

import type { ApiClient } from "../../api";
import { formatTime } from "../../format";
import { decisionNodeId } from "./focusLayout";
import type { FocusDecision, FocusTask, MeetingFocus } from "./graphTypes";
import { Quotes } from "./quotes";
import { DecisionMarks } from "../decisions/DecisionRow";
import { markQuestion } from "../decisions/markQuestion";
import { useLinksFlags } from "../links/LinksFlagsContext";
import { useRelationAnswer } from "../links/useRelationAnswer";
import { PlayButton, Section, TASK_STATUS, localUndoUntil, type GraphNoticeUndo, type NoticeFn } from "./panelParts";
import type { MiniPlayerHandle } from "./MiniPlayer";
import "./GraphPanel.css";
import "./MeetingFocus.css";

export interface FocusPanelProps {
  apiClient: ApiClient;
  focus: MeetingFocus;
  selectedId: string;
  player: MiniPlayerHandle;
  onClose: () => void;
  onSelect: (id: string | null) => void;
  onChanged: () => void | Promise<void>;
  onNotice: NoticeFn;
  onOpenRequirement: (requirementId: string) => void;
  /** 4f：决议面板的［来龙去脉］（dec:<id>）；旧后台、手机上不传 */
  onTrace?: (node: string) => void;
  /** 3g：交付物里的文件点了打开预览抽屉 */
  onOpenPreview?: (fileId: number) => void;
}

function FocusQuotes({ props, atMs }: { props: FocusPanelProps; atMs: number | null }) {
  const { focus, player } = props;
  return (
    <Quotes
      apiClient={props.apiClient}
      atMs={atMs}
      audioUrl={focus.meeting.audio_url}
      label={focus.meeting.title}
      meetingId={focus.meeting.id}
      player={player}
    />
  );
}

function Source({ props, atMs }: { props: FocusPanelProps; atMs: number | null }) {
  const { focus, player } = props;
  return (
    <p className="focus-panel__source">
      {atMs === null ? (
        "纪要里没写是什么时候说的"
      ) : (
        <>
          <PlayButton atMs={atMs} audioUrl={focus.meeting.audio_url} label={focus.meeting.title} player={player} />
          会上 {formatTime(atMs)} 说的
        </>
      )}
    </p>
  );
}

/**
 * 4c：决议下面和需求卡一样的标记行。［不是一回事］走 useRelationAnswer，提示交给画布的 showNotice，
 * 撤销进画布的撤销栈（{kind: "relation", relationId, label, until}，until 是服务端的 undo_until）。
 */
function FocusDecisionMarks({ props, decision }: { props: FocusPanelProps; decision: FocusDecision }) {
  const flags = useLinksFlags();
  const decisionId = decision.id ?? null;
  const answering = useRelationAnswer({
    apiClient: props.apiClient,
    scope: { decisionIds: decisionId ? [decisionId] : [] },
    onNotice: props.onNotice,
    onChanged: props.onChanged,
  });
  if (!decisionId) return null;
  const later = (decision.later ?? []).filter((ref) => !answering.gone.has(ref.relation_id));
  const earlier = (decision.earlier ?? []).filter((ref) => !answering.gone.has(ref.relation_id));
  const canAnswer = flags !== null && typeof props.apiClient.answerRelation === "function" && !answering.oldBackend;
  return (
    <DecisionMarks
      busy={answering.sending !== null}
      dismissed={[]}
      earlier={earlier}
      later={later}
      onDismiss={
        canAnswer
          ? (relationId, kind) => void answering.answer(markQuestion(relationId, kind, decisionId), "no")
          : undefined
      }
      player={props.player}
      restated={[]}
    />
  );
}

/** 4e：「1 个文件可能过时」：点了打开第一份文件的预览抽屉；旧后台（没有 stale、没有 linksFlags）不画 */
export function staleTagText(count: number): string {
  return `${count} 个文件可能过时`;
}

function StaleTag({ props, decision }: { props: FocusPanelProps; decision: FocusDecision }) {
  const flags = useLinksFlags();
  const stale = decision.stale;
  if (!flags || !Array.isArray(stale) || !stale.length) return null;
  const text = staleTagText(stale.length);
  if (!props.onOpenPreview) return <p className="focus-panel__stale">{text}</p>;
  return (
    <p className="focus-panel__stale">
      <button className="text-button" onClick={() => props.onOpenPreview?.(stale[0].file_id)} title={stale[0].name} type="button">
        {text}
      </button>
    </p>
  );
}

function DecisionBody({ props, decision }: { props: FocusPanelProps; decision: FocusDecision }) {
  return (
    <>
      <Section title="全文">
        <p className="focus-panel__full">{decision.detail || decision.text}</p>
        <StaleTag decision={decision} props={props} />
        <FocusDecisionMarks decision={decision} props={props} />
      </Section>
      <Section title="来源">
        <Source atMs={decision.start_ms} props={props} />
      </Section>
      <Section title="前后 20 秒的原话">
        <FocusQuotes atMs={decision.start_ms} props={props} />
      </Section>
      {props.onTrace && decision.id && (
        <div className="graph-panel__actions graph-panel__actions--start">
          <button className="ghost-button" onClick={() => props.onTrace?.(`dec:${decision.id}`)} type="button">
            来龙去脉
          </button>
        </div>
      )}
    </>
  );
}

function TaskBody({ props, task }: { props: FocusPanelProps; task: FocusTask }) {
  const { apiClient } = props;
  const [busy, setBusy] = useState(false);
  const [editing, setEditing] = useState(false);
  const [title, setTitle] = useState(task.title);
  const [detail, setDetail] = useState(task.detail);

  useEffect(() => {
    setEditing(false);
    setTitle(task.title);
    setDetail(task.detail);
  }, [task.id, task.title, task.detail]);

  const run = async (work: () => Promise<unknown>, message: string, undo?: GraphNoticeUndo, gone = false) => {
    setBusy(true);
    try {
      await work();
      props.onNotice(message, undo);
      if (gone) props.onSelect(null);
      await props.onChanged();
    } catch (reason) {
      props.onNotice(reason instanceof Error ? reason.message : "操作失败", undefined, "error");
    } finally {
      setBusy(false);
    }
  };

  const save = () => {
    const nextTitle = title.trim();
    if (!nextTitle) return;
    if (nextTitle === task.title && detail === task.detail) {
      setEditing(false);
      return;
    }
    setEditing(false);
    void run(() => apiClient.updateTask(task.id, { title: nextTitle, detail }), `已改好「${nextTitle}」`, {
      kind: "task",
      what: "edit",
      taskId: task.id,
      title: nextTitle,
      before: { title: task.title, detail: task.detail },
      until: localUndoUntil(),
    });
  };

  const open = task.status === "confirmed" || task.status === "in_progress";
  const pending = task.status === "pending_confirm";

  return (
    <>
      {editing ? (
        <Section title="编辑">
          <form
            className="focus-panel__edit"
            onSubmit={(event) => {
              event.preventDefault();
              save();
            }}
          >
            <input aria-label="任务标题" autoFocus onChange={(event) => setTitle(event.target.value)} value={title} />
            <textarea aria-label="任务说明" onChange={(event) => setDetail(event.target.value)} value={detail} />
            <span className="focus-panel__row">
              <button className="primary-button" disabled={busy || !title.trim()} type="submit">
                保存
              </button>
              <button className="text-button" onClick={() => setEditing(false)} type="button">
                取消
              </button>
            </span>
          </form>
        </Section>
      ) : (
        task.detail && (
          <Section title="说明">
            <p className="focus-panel__full">{task.detail}</p>
          </Section>
        )
      )}
      <Section title="来源">
        <Source atMs={task.anchor_ms} props={props} />
        {task.anchor_quote && <p className="graph-panel__muted">「{task.anchor_quote}」</p>}
      </Section>
      {task.requirement_id && (
        <Section title="挂在需求上">
          <button className="text-button" onClick={() => props.onOpenRequirement(task.requirement_id!)} type="button">
            {task.requirement_title ?? "打开需求"}
          </button>
          {task.project_id !== props.focus.meeting.project_id && <small className="graph-panel__muted"> 在别的项目</small>}
        </Section>
      )}
      {task.deliverables.length > 0 && (
        <Section title="交付物">
          <ul className="graph-panel__files">
            {task.deliverables.map((item) => (
              <li key={item.id ?? `${item.kind}-${item.url}`} title={item.url}>
                {item.kind === "file" && typeof item.file_id === "number" && props.onOpenPreview ? (
                  <button className="text-button" onClick={() => props.onOpenPreview?.(item.file_id as number)} type="button">
                    {item.name || item.title || item.url}
                  </button>
                ) : (
                  item.title || item.name || item.url
                )}
                {item.kind === "file" && item.gone && <small className="graph-panel__muted"> 找不到这个文件了</small>}
                {item.kind === "file" && item.file_id !== undefined && <small className="graph-panel__muted"> · 你标的</small>}
              </li>
            ))}
          </ul>
        </Section>
      )}
      <Section title="前后 20 秒的原话">
        <FocusQuotes atMs={task.anchor_ms} props={props} />
      </Section>
      {task.status !== "done" && !editing && (
        <div className="focus-panel__row graph-panel__section">
          {pending && (
            <button
              className="primary-button"
              disabled={busy}
              onClick={() => void run(() => apiClient.confirmTask(task.id), `已确认「${task.title}」`)}
              type="button"
            >
              确认
            </button>
          )}
          {open && (
            <button
              className="primary-button"
              disabled={busy}
              onClick={() => void run(() => apiClient.setTaskStatus(task.id, "done"), `已完成「${task.title}」`)}
              type="button"
            >
              完成
            </button>
          )}
          <button
            className="ghost-button"
            disabled={busy}
            onClick={() =>
              void run(
                () => (pending ? apiClient.rejectTask(task.id) : apiClient.setTaskStatus(task.id, "cancelled")),
                `已不要「${task.title}」`,
                undefined,
                true,
              )
            }
            type="button"
          >
            不要
          </button>
          <button className="text-button" disabled={busy} onClick={() => setEditing(true)} type="button">
            编辑…
          </button>
        </div>
      )}
    </>
  );
}

function AllDecisions({ props }: { props: FocusPanelProps }) {
  const { focus, player } = props;
  return (
    <ol className="graph-panel__list">
      {focus.decisions.map((item, index) => (
        <li key={`${index}-${item.text}`}>
          <PlayButton atMs={item.start_ms} audioUrl={focus.meeting.audio_url} label={focus.meeting.title} player={player} />
          <button className="text-button" onClick={() => props.onSelect(decisionNodeId(item, index))} type="button">
            {item.text}
          </button>
        </li>
      ))}
    </ol>
  );
}

function AllTasks({ props }: { props: FocusPanelProps }) {
  const { focus, player } = props;
  return (
    <ul className="graph-panel__list">
      {focus.tasks.map((task) => (
        <li className="graph-panel__task" key={task.id}>
          <PlayButton atMs={task.anchor_ms} audioUrl={focus.meeting.audio_url} label={focus.meeting.title} player={player} />
          <button className="text-button" onClick={() => props.onSelect(`task:${task.id}`)} type="button">
            {task.title}
          </button>
          <small>{TASK_STATUS[task.status] ?? task.status}</small>
        </li>
      ))}
      {focus.tasks_more > 0 && <li className="graph-panel__muted">还有 {focus.tasks_more} 条，去会议页看</li>}
    </ul>
  );
}

/** 展开一场会时的右侧面板：决议、任务，或「+N」里的全部 */
export function FocusPanel(props: FocusPanelProps) {
  const { focus, selectedId } = props;
  let kind = "";
  let heading = "";
  let body: ReactNode = null;
  if (selectedId.startsWith("dec:")) {
    const decision = focus.decisions.find((item, index) => decisionNodeId(item, index) === selectedId);
    if (decision) {
      kind = "决议";
      heading = decision.text;
      body = <DecisionBody decision={decision} props={props} />;
    }
  } else if (selectedId.startsWith("task:")) {
    const task = focus.tasks.find((item) => item.id === selectedId.slice(5));
    if (task) {
      kind = `任务 · ${TASK_STATUS[task.status] ?? task.status}`;
      heading = task.title;
      body = <TaskBody props={props} task={task} />;
    }
  } else if (selectedId === "more:decisions") {
    kind = "这场会";
    heading = `定了什么（${focus.decisions.length} 条）`;
    body = <AllDecisions props={props} />;
  } else if (selectedId === "more:tasks") {
    kind = "这场会";
    heading = `全部任务（${focus.tasks.length + focus.tasks_more} 条）`;
    body = <AllTasks props={props} />;
  }
  if (!body) {
    kind = "这场会";
    heading = "找不到了";
    body = <p className="graph-panel__muted">这一条已经不在这场会上了，可能刚被改掉或取消。</p>;
  }

  return (
    <aside aria-label="详情面板" className="graph-panel">
      <header className="graph-panel__head">
        <div>
          <span className="graph-panel__kind">{kind}</span>
          <h2>{heading}</h2>
        </div>
        <div className="graph-panel__nav">
          <button aria-label="关闭面板" className="graph-panel__close" onClick={props.onClose} type="button">
            ✕
          </button>
        </div>
      </header>
      <div className="graph-panel__body">{body}</div>
    </aside>
  );
}
