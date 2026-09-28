import { useCallback, useEffect, useReducer, useRef, useState, type FormEvent, type ReactNode } from "react";

import { ApiError, isOldBackend, type ApiClient } from "../../api";
import { copyText } from "../../clipboard";
import type { AskJob, AskPlan, AskSource, PreviewTarget } from "../../types";
import { useMiniPlayer, type MiniPlayerHandle } from "../graph/MiniPlayer";
import { AnswerText } from "./AnswerText";
import { newTurnId, readTurns, takeDraft, writeTurns, type AskTurn } from "./askStore";
import { sourceLabel, type ChipHandlers } from "./CitationChip";
import { SourceList } from "./SourceList";
import { useAskJob, type AskJobUpdate } from "./useAskJob";
import "./ask.css";

/*
 * 「问这个项目」（4g）：项目页的卡片（variant="card"，手机上也有）和关系图右侧面板（variant="panel"）共用。
 * 先 prepare 在本机找原文（不调 AI）；有材料段落时在［发送］正上方写「将发送 N 段材料原文给 <主机>」，
 * 点了［发送］才发，旁边［只用会议回答］只发会议里的；没有材料段落时直接发。回答按纯文本画，出处是小块。
 * 回答只在内存里（askStore），刷新就没有；问答不改本机数据，没有撤销，也不看 canWrite。
 */

export const ASK_TITLE = "问这个项目";
export const ASK_PLACEHOLDER = "比如：报价最后定的是多少？";
export const ASK_MAX = 300;
export const OLD_BACKEND_TEXT = "后台还是旧版本，重启声档后再试";

const LLM_TEXTS: Record<string, string> = {
  no_key: "没配置 AI，先列出找到的原话",
  off: "问答的 AI 回答已关闭，先列出找到的原话",
  capped: "今天问答的次数到上限了，先列出找到的原话",
};

type AskApi = Partial<Pick<ApiClient, "askPrepare" | "ask" | "askJob">>;

export interface ProjectAskProps {
  apiClient: AskApi;
  projectId: string;
  projectName: string;
  variant: "card" | "panel";
  isMobile?: boolean;
  /** 关系图传它自己的迷你播放器；不传时卡片里放一个 */
  player?: MiniPlayerHandle;
  onOpenMeeting: (meetingId: string, seekMs?: number, tab?: "transcript" | "minutes") => void;
  onOpenPreview: (target: PreviewTarget) => void;
  /** 回答显示期间出处里的会和文件（m:<id>、file:<id>），关掉时 null */
  onHighlight?: (ids: string[] | null) => void;
  onClose?: () => void;
}

export function ProjectAsk(props: ProjectAskProps) {
  // 旧后台（部分客户端）没有 askPrepare：卡片、关系图按钮和搜索提示都不画
  if (typeof props.apiClient.askPrepare !== "function") return null;
  return <AskInner {...props} />;
}

/** 「找到会议里的 5 段、材料里的 3 段」 */
export function foundText(counts: { meetings: number; materials: number }): string {
  const parts = [];
  if (counts.meetings) parts.push(`会议里的 ${counts.meetings} 段`);
  if (counts.materials) parts.push(`材料里的 ${counts.materials} 段`);
  return `找到${parts.join("、")}`;
}

/** 「只看了最相关的 3 段材料、5 段会议里的原话」 */
export function sentText(sent: { meetings: number; materials: number }): string {
  const parts = [];
  if (sent.materials) parts.push(`${sent.materials} 段材料`);
  if (sent.meetings) parts.push(`${sent.meetings} 段会议里的原话`);
  return `只看了最相关的 ${parts.join("、")}`;
}

/** 复制：回答文字（带编号）和编号的出处 */
export function copyAnswerText(question: string, answer: string, sources: AskSource[], cited: string[]): string {
  const lines = [`问：${question}`, answer, "", "引用："];
  for (const id of cited) {
    const source = sources.find((item) => item.id === id);
    if (source) lines.push(`[${id}] ${sourceLabel(source)}：${source.quote || source.text}`);
  }
  return lines.join("\n");
}

function highlightIds(sources: AskSource[], cited: string[]): string[] {
  const ids = new Set<string>();
  for (const source of sources) {
    if (!cited.includes(source.id)) continue;
    if (source.kind === "material" && source.file_id !== undefined) ids.add(`file:${source.file_id}`);
    else if (source.meeting_id) ids.add(`m:${source.meeting_id}`);
  }
  return [...ids];
}

function AskInner({
  apiClient,
  projectId,
  projectName,
  variant,
  isMobile = false,
  player: hostPlayer,
  onOpenMeeting,
  onOpenPreview,
  onHighlight,
  onClose,
}: ProjectAskProps) {
  const ownPlayer = useMiniPlayer();
  const player = hostPlayer ?? ownPlayer;
  const [, rerender] = useReducer((count: number) => count + 1, 0);
  const mounted = useRef(true);
  const [input, setInput] = useState("");
  const [oldBackend, setOldBackend] = useState(false);
  const [openList, setOpenList] = useState<number | null>(null);
  const [copied, setCopied] = useState<number | null>(null);

  useEffect(() => {
    mounted.current = true;
    // 搜索页交过来的问题：填好，不自动发
    const draft = takeDraft(projectId);
    if (draft) setInput(draft.slice(0, ASK_MAX));
    return () => {
      mounted.current = false;
    };
  }, [projectId]);

  // 几轮都在模块的 Map 里（卸载以后回来的请求照样写进去）
  const turns = readTurns(projectId);
  const current = turns[0] ?? null;
  const earlier = turns.slice(1);

  const patch = useCallback(
    (id: number, change: Partial<AskTurn>) => {
      writeTurns(
        projectId,
        readTurns(projectId).map((turn) => (turn.id === id ? { ...turn, ...change } : turn)),
      );
      if (mounted.current) rerender();
    },
    [projectId],
  );

  const send = useCallback(
    async (id: number, plan: AskPlan, withMaterials: boolean) => {
      patch(id, { phase: "sending", withMaterials });
      try {
        const started = await apiClient.ask!(projectId, plan.plan_id, withMaterials);
        patch(id, { phase: "waiting", jobId: started.job_id });
      } catch (error) {
        const status = error instanceof ApiError ? error.status : 0;
        if (status === 409) patch(id, { phase: "failed", error: { text: "上一个问题还在回答", retry: false } });
        else if (status === 404) patch(id, { phase: "failed", error: { text: "这次找到的原话过期了", retry: true } });
        else patch(id, { phase: "failed", error: { text: (error as Error).message, retry: false } });
      }
    },
    [apiClient, patch, projectId],
  );

  const prepare = useCallback(
    async (id: number, question: string) => {
      patch(id, { phase: "preparing", plan: undefined, job: undefined, jobId: undefined, error: undefined });
      let plan: AskPlan;
      try {
        plan = await apiClient.askPrepare!(projectId, question);
      } catch (error) {
        if (isOldBackend(error)) {
          writeTurns(projectId, readTurns(projectId).filter((turn) => turn.id !== id));
          if (mounted.current) setOldBackend(true);
          return;
        }
        patch(id, { phase: "failed", error: { text: (error as Error).message, retry: false } });
        return;
      }
      if (plan.sources.length === 0) patch(id, { phase: "empty", plan });
      else if (plan.llm !== "ok") patch(id, { phase: "unavailable", plan });
      else if (plan.confirm) patch(id, { phase: "confirm", plan });
      else {
        // 没有材料段落：逐字稿、纪要、决议本来就发给同一个 AI，直接发
        patch(id, { plan });
        await send(id, plan, false);
      }
    },
    [apiClient, patch, projectId, send],
  );

  const onJob = useCallback(
    (update: AskJobUpdate) => {
      if (!current) return;
      if (update.kind === "job") {
        patch(current.id, { phase: update.job.state === "done" ? "done" : "stopped", job: update.job });
      } else {
        patch(current.id, { phase: "failed", error: { text: update.text, retry: true } });
      }
    },
    [current, patch],
  );
  useAskJob(
    apiClient as Pick<ApiClient, "askJob">,
    current?.phase === "waiting" && current.jobId ? current.jobId : null,
    onJob,
  );

  // 回答显示期间点亮出处里的会和文件
  const doneJob = current?.phase === "done" && current.job?.state === "done" ? current.job : null;
  const lit = doneJob && doneJob.answer.text ? highlightIds(doneJob.sources, doneJob.answer.cited).join("|") : "";
  // 宿主常传内联函数：放进 ref，只在点亮的内容变了时调，免得来回触发
  const highlightRef = useRef(onHighlight);
  highlightRef.current = onHighlight;
  useEffect(() => {
    highlightRef.current?.(lit ? lit.split("|") : null);
  }, [lit]);
  useEffect(() => () => highlightRef.current?.(null), []);

  if (oldBackend) {
    return (
      <p className="ask-old" role="status">
        {OLD_BACKEND_TEXT}
      </p>
    );
  }

  const busy = current !== null && ["preparing", "sending", "waiting"].includes(current.phase);

  const submit = (event: FormEvent) => {
    event.preventDefault();
    const question = input.trim();
    if (!question || busy) return;
    const id = newTurnId();
    writeTurns(projectId, [{ id, question, phase: "preparing" }, ...readTurns(projectId)]);
    setInput("");
    setOpenList(null);
    rerender();
    void prepare(id, question);
  };

  const retry = (turn: AskTurn) => {
    if (turn.plan && turn.phase === "stopped") void resend(turn);
    else void prepare(turn.id, turn.question);
  };

  // ［再问一次］：同一个计划重发；计划过期（404）时重新找一遍
  const resend = async (turn: AskTurn) => {
    const plan = turn.plan as AskPlan;
    patch(turn.id, { phase: "sending", job: undefined, jobId: undefined });
    try {
      const started = await apiClient.ask!(projectId, plan.plan_id, turn.withMaterials ?? false);
      patch(turn.id, { phase: "waiting", jobId: started.job_id });
    } catch (error) {
      if (error instanceof ApiError && error.status === 404) await prepare(turn.id, turn.question);
      else if (error instanceof ApiError && error.status === 409)
        patch(turn.id, { phase: "failed", error: { text: "上一个问题还在回答", retry: false } });
      else patch(turn.id, { phase: "failed", error: { text: (error as Error).message, retry: false } });
    }
  };

  const highlight = current?.plan?.highlight ?? [];
  const handlers: ChipHandlers = { onOpenMeeting, onOpenPreview, player, highlight };

  const listToggle = (turn: AskTurn, label: string) => (
    <button
      aria-expanded={openList === turn.id}
      className="text-button ask-toggle"
      onClick={() => setOpenList(openList === turn.id ? null : turn.id)}
      type="button"
    >
      {label} {openList === turn.id ? "▴" : "▾"}
    </button>
  );

  const body = current && (
    <TurnView
      handlers={handlers}
      listOpen={openList === current.id}
      listToggle={listToggle}
      copied={copied === current.id}
      onCopy={async (text) => {
        try {
          await copyText(text);
          setCopied(current.id);
        } catch {
          setCopied(null);
        }
      }}
      onRetry={() => retry(current)}
      onSend={(withMaterials) => current.plan && void send(current.id, current.plan, withMaterials)}
      turn={current}
    />
  );

  const Root = variant === "card" ? "section" : "div";
  return (
    <Root
      aria-label={ASK_TITLE}
      className={`ask ask--${variant}${variant === "card" ? " detail-card" : ""}${isMobile ? " ask--mobile" : ""}`}
    >
      {!hostPlayer && ownPlayer.audioElement}
      <header className="ask__head">
        <h2>{ASK_TITLE}</h2>
        {onClose && (
          <button aria-label="关掉问答" className="text-button" onClick={onClose} type="button">
            ×
          </button>
        )}
      </header>
      {!hostPlayer && ownPlayer.clip && <div className="ask__player">{ownPlayer.node}</div>}
      <form className="ask__form" onSubmit={submit}>
        <input
          aria-label="问题"
          maxLength={ASK_MAX}
          onChange={(event) => setInput(event.target.value)}
          placeholder={ASK_PLACEHOLDER}
          type="text"
          value={input}
        />
        <button className="primary-button" disabled={busy || !input.trim()} type="submit">
          问
        </button>
      </form>
      {turns.length === 0 && (
        <p className="ask__hint">只在『{projectName}』的会和材料里找；要把材料原文发出去时会先告诉你</p>
      )}
      <div aria-live="polite" className="ask__answer">
        {body}
      </div>
      {earlier.length > 0 && (
        <details className="ask-history">
          <summary>之前问过</summary>
          {earlier.map((turn) => (
            <div className="ask-history__turn" key={turn.id}>
              <p className="ask-question">问：{turn.question}</p>
              {turn.job?.state === "done" && turn.job.answer.text ? (
                <AnswerText handlers={handlers} sources={turn.job.sources} text={turn.job.answer.text} />
              ) : (
                <p className="ask-muted">{historyText(turn)}</p>
              )}
            </div>
          ))}
        </details>
      )}
    </Root>
  );
}

function historyText(turn: AskTurn): string {
  if (turn.job?.state === "stopped") return turn.job.text;
  if (turn.job?.state === "done") return "会议和材料里没找到能回答这个问题的原话";
  if (turn.error) return turn.error.text;
  return "没有发送";
}

function notesOf(turn: AskTurn, job: Extract<AskJob, { state: "done" }>): string[] {
  const lines: string[] = [];
  if (job.local_model) lines.push("用本机模型回答");
  for (const note of job.notes) lines.push(note.text);
  const unattributed = turn.plan?.unattributed_meetings ?? 0;
  if (unattributed > 0) lines.push(`另有 ${unattributed} 场没归项目的会也说到这些词，这次没用上`);
  return [...new Set(lines)].slice(0, 2);
}

function TurnView({
  turn,
  handlers,
  listOpen,
  listToggle,
  copied,
  onCopy,
  onRetry,
  onSend,
}: {
  turn: AskTurn;
  handlers: ChipHandlers;
  listOpen: boolean;
  listToggle: (turn: AskTurn, label: string) => ReactNode;
  copied: boolean;
  onCopy: (text: string) => void;
  onRetry: () => void;
  onSend: (withMaterials: boolean) => void;
}) {
  const plan = turn.plan;
  const retryButton = (
    <button className="text-button" onClick={onRetry} type="button">
      再问一次
    </button>
  );
  switch (turn.phase) {
    case "preparing":
      return <p className="ask-status">正在找相关的原话…</p>;
    case "sending":
    case "waiting":
      return <p className="ask-status">在等 AI 回答</p>;
    case "empty":
      return (
        <>
          <p className="ask-status">会议和材料里都没找到和这个问题有关的原话</p>
          <p className="ask-muted">换个说法，或者用文件名、词典里的词问</p>
        </>
      );
    case "unavailable":
      return (
        <>
          <p className="ask-status">{LLM_TEXTS[plan?.llm ?? ""] ?? LLM_TEXTS.no_key}</p>
          {plan && <SourceList handlers={handlers} sources={plan.sources} />}
        </>
      );
    case "confirm":
      if (!plan?.confirm) return null;
      return (
        <div className="ask-confirm">
          <p className="ask-status">{foundText(plan.counts)}</p>
          <p className="ask-confirm__line">{plan.confirm.text}</p>
          <div className="ask-confirm__actions">
            <button autoFocus className="primary-button" onClick={() => onSend(true)} type="button">
              发送
            </button>
            {plan.counts.meetings > 0 && (
              <button className="ghost-button" onClick={() => onSend(false)} type="button">
                只用会议回答
              </button>
            )}
            {listToggle(turn, "看看是哪几段")}
          </div>
          {listOpen && <SourceList handlers={handlers} sources={plan.sources} />}
        </div>
      );
    case "stopped": {
      const job = turn.job?.state === "stopped" ? turn.job : null;
      return (
        <>
          <p className="ask-question">问：{turn.question}</p>
          <p className="ask-status">
            {job?.text}
            {job?.retry && retryButton}
          </p>
          {job && <SourceList handlers={handlers} sources={job.sources} />}
        </>
      );
    }
    case "failed":
      return (
        <p className="ask-status">
          {turn.error?.text}
          {turn.error?.retry && retryButton}
        </p>
      );
    case "done": {
      const job = turn.job?.state === "done" ? turn.job : null;
      if (!job) return null;
      const { answer } = job;
      const notes = notesOf(turn, job);
      if (answer.no_evidence) {
        return (
          <>
            <p className="ask-question">问：{turn.question}</p>
            <p className="ask-status">AI 的回答没指到原文，没列出来；下面是找到的原话</p>
            <SourceList handlers={handlers} sources={job.sources} />
          </>
        );
      }
      if (!answer.found) {
        return (
          <>
            <p className="ask-question">问：{turn.question}</p>
            <p className="ask-status">会议和材料里没找到能回答这个问题的原话</p>
            {listToggle(turn, "看看找到的原话")}
            {listOpen && <SourceList handlers={handlers} sources={job.sources} />}
          </>
        );
      }
      const cited = job.sources.filter((source) => answer.cited.includes(source.id));
      return (
        <>
          <p className="ask-question">问：{turn.question}</p>
          <AnswerText handlers={handlers} sources={job.sources} text={answer.text} />
          {answer.truncated && <p className="ask-muted">回答太长，后面截掉了</p>}
          <p className="ask-summary">
            <span>{sentText(job.sent)}</span>
            <span aria-hidden="true"> · </span>
            {listToggle(turn, "看看是哪几段")}
            <span aria-hidden="true"> · </span>
            <button
              className="text-button"
              onClick={() => onCopy(copyAnswerText(turn.question, answer.text, job.sources, answer.cited))}
              type="button"
            >
              复制回答
            </button>
            {copied && (
              <span className="ask-muted" role="status">
                已复制
              </span>
            )}
          </p>
          {notes.map((note) => (
            <p className="ask-muted" key={note}>
              {note}
            </p>
          ))}
          {listOpen && <SourceList handlers={handlers} sources={job.sources.filter((source) => source.sent !== false)} />}
          {cited.length > 0 && (
            <section className="ask-cited">
              <h3>引用</h3>
              <SourceList handlers={handlers} label="引用" sources={cited} />
            </section>
          )}
        </>
      );
    }
    default:
      return null;
  }
}
