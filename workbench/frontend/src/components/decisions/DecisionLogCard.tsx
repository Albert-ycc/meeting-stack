import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import {
  ApiError,
  isOldBackend,
  type ApiClient,
  type DecisionDismissed,
  type DecisionLogEntry,
  type DecisionLogMeeting,
  type DecisionPlacementResult,
  type RequirementDecisionLog,
} from "../../api";
import { useMiniPlayer } from "../graph/MiniPlayer";
import type { NoticeFn } from "../graph/panelParts";
import { useLinksFlags } from "../links/LinksFlagsContext";
import { LinksStateLine } from "../links/LinksStateLine";
import { RelationQuestion } from "../links/RelationQuestion";
import { OLD_BACKEND_TEXT, useRelationAnswer } from "../links/useRelationAnswer";
import { NoticeBanner, UNDO_NOTICE_MS, useNotice } from "../Notice";
import { DecisionRow } from "./DecisionRow";
import {
  PLACED_HERE_NOTICE,
  PLACED_NONE_NOTICE,
  UNDONE_NOTICE,
  dayWithWeekday,
  monthDay,
} from "./decisionText";
import { markQuestion } from "./markQuestion";
import "./decisions.css";

type CardApi = Pick<ApiClient, "meetingQuotes"> &
  Partial<Pick<ApiClient, "requirementDecisions" | "answerRelation" | "undoRelation" | "placeDecision" | "retryLinks">>;

interface DecisionLogCardProps {
  apiClient: CardApi;
  requirementId: string;
  canWrite: boolean;
  onOpenMeeting: (meetingId: string, seekMs?: number) => void;
  /** 4e：「『报价单 v3』之后没改过，可能过时」的文件名点了打开预览抽屉 */
  onOpenPreview?: (fileId: number) => void;
  reloadKey?: number | string;
}

/** 放决议以后原地留的一行灰字，留到 undo_until。记着那场会：拿掉的是这场会在卡里唯一的一条时，重读回来
 * 这场会已不在卡里，灰字行照样按这场会画到 undo_until */
interface PlacedLine {
  decisionId: string;
  meetingId: string;
  meeting: DecisionLogMeeting["meeting"];
  text: string;
  undo: DecisionPlacementResult["undo"];
  until: string;
}

type LoadState = "loading" | "ready" | "error" | "hidden";

const NO_MINUTES = "纪要还没写好";

function emptyText(log: RequirementDecisionLog): string | null {
  if (!log.meetings.length) return "还没有关联会议，关联以后这里列出每场会定了什么";
  if (log.meetings.every((group) => group.note === NO_MINUTES)) return "关联的会还没写好纪要";
  if (!log.counts.decisions && !log.counts.unplaced) return `关联的 ${log.meetings.length} 场会，纪要里没有列出决议`;
  return null;
}

/**
 * 需求页「决议」卡（4c）：放在「关联会议」和「材料文件夹」之间。按会分组，新的会在前，组内按纪要顺序；
 * 每条能回听原话，标出后来改了、后来又提到；没归到具体需求的折叠成一行。自己调一次 useMiniPlayer。
 * 旧后台（没有 requirementDecisions，或接口回 FastAPI 的 404）时整张卡不画。
 */
export function DecisionLogCard({
  apiClient,
  requirementId,
  canWrite,
  onOpenMeeting,
  onOpenPreview,
  reloadKey = 0,
}: DecisionLogCardProps) {
  const supported = typeof apiClient.requirementDecisions === "function";
  const flags = useLinksFlags();
  const player = useMiniPlayer();
  const { notice, setNotice, dismissNotice } = useNotice();
  const [state, setState] = useState<LoadState>(supported ? "loading" : "hidden");
  const [log, setLog] = useState<RequirementDecisionLog | null>(null);
  const [openUnplaced, setOpenUnplaced] = useState<ReadonlySet<string>>(() => new Set());
  const [placed, setPlaced] = useState<PlacedLine[]>([]);
  const [busy, setBusy] = useState(false);
  const [now, setNow] = useState(() => Date.now());

  const load = useCallback(
    async (silent = false) => {
      if (typeof apiClient.requirementDecisions !== "function") return;
      if (!silent) setState("loading");
      try {
        const payload = await apiClient.requirementDecisions(requirementId);
        setLog(payload);
        setState("ready");
      } catch (reason) {
        // 旧后台或需求没了：不画这张卡
        if (isOldBackend(reason) || (reason instanceof ApiError && reason.status === 404)) setState("hidden");
        else if (!silent) setState("error");
      }
    },
    [apiClient, requirementId],
  );

  useEffect(() => {
    void load();
  }, [load, reloadKey]);

  const decisionIds = useMemo(
    () =>
      (log?.meetings ?? []).flatMap((group) =>
        [...group.decisions, ...group.unplaced].flatMap((entry) => (entry.id ? [entry.id] : [])),
      ),
    [log],
  );

  // 提示里的［撤销］接到 useRelationAnswer 的 undo()（关系图外没有 ⌘Z）
  const undoRef = useRef<(relationId: number) => Promise<boolean>>(async () => false);
  const onNotice: NoticeFn = useCallback(
    (message, undo, tone, actions) => {
      if (undo?.kind === "relation") {
        setNotice(message, "success", UNDO_NOTICE_MS, [
          { label: "撤销", onClick: () => void undoRef.current(undo.relationId) },
        ]);
        return;
      }
      setNotice(message, tone ?? "success", actions?.length ? UNDO_NOTICE_MS : undefined, actions);
    },
    [setNotice],
  );
  const answering = useRelationAnswer({
    apiClient,
    scope: { decisionIds },
    onNotice,
    onChanged: () => load(true),
  });
  useEffect(() => {
    undoRef.current = answering.undo;
  }, [answering.undo]);

  // 放决议的灰字行到 undo_until 收起
  const livePlaced = placed.filter((line) => Date.parse(line.until) > now);
  const nextDeadline = livePlaced.length ? Math.min(...livePlaced.map((line) => Date.parse(line.until))) : null;
  useEffect(() => {
    if (nextDeadline === null) return;
    const wait = Math.max(0, nextDeadline - Date.now()) + 50;
    const timer = window.setTimeout(() => setNow(Date.now()), Math.min(wait, 2_147_000_000));
    return () => window.clearTimeout(timer);
  }, [nextDeadline]);

  const failure = useCallback(
    (reason: unknown, fallback: string) => {
      if (isOldBackend(reason)) setNotice(OLD_BACKEND_TEXT, "warning");
      else if (reason instanceof ApiError && [404, 409, 422].includes(reason.status)) setNotice(reason.message, "warning");
      else setNotice(reason instanceof Error && reason.message ? reason.message : fallback, "error");
    },
    [setNotice],
  );

  if (state === "hidden") return null;

  const canAnswer =
    canWrite && flags !== null && typeof apiClient.answerRelation === "function" && !answering.oldBackend;
  const canPlace = canWrite && flags !== null && typeof apiClient.placeDecision === "function";

  const undoPlace = async (line: PlacedLine) => {
    if (busy || typeof apiClient.placeDecision !== "function") return;
    setBusy(true);
    try {
      await apiClient.placeDecision(line.decisionId, line.undo);
      setPlaced((current) => current.filter((item) => item.decisionId !== line.decisionId));
      setNotice(UNDONE_NOTICE);
      await load(true);
    } catch (reason) {
      failure(reason, "撤销失败");
    } finally {
      setBusy(false);
    }
  };

  const place = async (entry: DecisionLogEntry, meeting: DecisionLogMeeting["meeting"], placement: "none" | "picked") => {
    if (busy || !entry.id || typeof apiClient.placeDecision !== "function") return;
    setBusy(true);
    try {
      const result = await apiClient.placeDecision(entry.id, {
        placement,
        requirement_id: placement === "picked" ? requirementId : null,
      });
      const text = placement === "picked" ? PLACED_HERE_NOTICE : PLACED_NONE_NOTICE;
      const line: PlacedLine = {
        decisionId: entry.id,
        meetingId: meeting.id,
        meeting,
        text,
        undo: result.undo,
        until: result.undo_until,
      };
      setPlaced((current) => [...current.filter((item) => item.decisionId !== line.decisionId), line]);
      setNow(Date.now());
      setNotice(text, "success", UNDO_NOTICE_MS, [{ label: "撤销", onClick: () => void undoPlace(line) }]);
      await load(true);
    } catch (reason) {
      failure(reason, "操作失败");
    } finally {
      setBusy(false);
    }
  };

  const restore = async (item: DecisionDismissed) => {
    if (busy || typeof apiClient.answerRelation !== "function") return;
    setBusy(true);
    try {
      await apiClient.answerRelation(item.relation_id, { answer: "restore" });
      setNotice(UNDONE_NOTICE);
      await load(true);
    } catch (reason) {
      failure(reason, "撤销失败");
    } finally {
      setBusy(false);
    }
  };

  const dismiss = (relationId: number, kind: "later_changed" | "restated", decisionId: string) => {
    void answering.answer(markQuestion(relationId, kind, decisionId), "no");
  };

  const subtitle = log
    ? `${log.counts.decisions} 条${log.counts.later_changed ? ` · ${log.counts.later_changed} 条后来改了` : ""}`
    : "";

  const row = (entry: DecisionLogEntry, group: DecisionLogMeeting, action: "none" | "picked") => (
    <DecisionRow
      apiClient={apiClient}
      busy={busy || answering.sending !== null}
      canWrite={canAnswer}
      decision={entry}
      key={entry.id ?? `${group.meeting.id}-${entry.text}`}
      meeting={group.meeting}
      onDismiss={dismiss}
      onOpenMeeting={onOpenMeeting}
      onRestore={(item) => void restore(item)}
      player={player}
      actionsShown={action === "picked" ? "always" : "hover"}
      questions={
        // 4e：决议之后没改过的文件每个一行，［已更新］［不相关］，提示用这张卡的 NoticeBanner（带［撤销］）
        // 问题没了（回答以后重读）也挂着：收成的那一行留到撤销期结束
        entry.id && Array.isArray(entry.stale_files) ? (
          <RelationQuestion
            answering={answering}
            apiClient={apiClient}
            canWrite={canWrite}
            compact
            onNotice={onNotice}
            onOpenFile={onOpenPreview}
            questions={entry.stale_files}
            scope={{ decisionIds: [entry.id] }}
          />
        ) : undefined
      }
      actions={
        canPlace && entry.id ? (
          <button className="text-button" disabled={busy} onClick={() => void place(entry, group.meeting, action)} type="button">
            {action === "none" ? "不属于这个需求" : "放到这个需求"}
          </button>
        ) : undefined
      }
    />
  );

  // 刚放走的决议所在的会不在这次返回里了（拿掉的是它在卡里唯一的一条）：按那场会补一组，灰字行留到 undo_until
  const shownMeetings = new Set((log?.meetings ?? []).map((group) => group.meeting.id));
  const orphans: DecisionLogMeeting[] = [];
  for (const line of livePlaced) {
    if (shownMeetings.has(line.meetingId)) continue;
    shownMeetings.add(line.meetingId);
    orphans.push({ meeting: line.meeting, note: null, decisions: [], unplaced: [] });
  }
  // 新的会在前：补的组按日子插回去（sort 是稳定的，同一天的照服务端的先后）
  const groups = orphans.length
    ? [...(log?.meetings ?? []), ...orphans].sort((left, right) =>
        (right.meeting.date ?? "").localeCompare(left.meeting.date ?? ""),
      )
    : log?.meetings ?? [];
  // 还有灰字行要留着时不写空的说法
  const empty = log && !livePlaced.length ? emptyText(log) : null;

  return (
    <section aria-label="决议" className="requirement-detail__card decision-log">
      {player.audioElement}
      <header className="requirement-detail__card-head">
        <strong>决议</strong>
        {subtitle && <span className="decision-log__subtitle">{subtitle}</span>}
      </header>
      {player.clip && <div className="decision-log__player">{player.node}</div>}
      <NoticeBanner notice={notice} onDismiss={dismissNotice} />
      {state === "loading" && (
        <div aria-busy="true" className="decision-log__skeleton">
          <span />
          <span />
          <span />
        </div>
      )}
      {state === "error" && (
        <p className="decision-log__empty">
          没读到决议，稍后再试
          <button className="text-button" onClick={() => void load()} type="button">
            重试
          </button>
        </p>
      )}
      {state === "ready" && log && (
        <>
          <LinksStateLine apiClient={apiClient} onRetried={() => load(true)} state={log.state.text ? log.state : null} />
          {empty ? (
            <p className="decision-log__empty">{empty}</p>
          ) : (
            <div className="decision-log__groups">
              {groups.map((group) => {
                const lines = livePlaced.filter((line) => line.meetingId === group.meeting.id);
                if (!group.decisions.length && !group.unplaced.length) {
                  if (!group.note && !lines.length) return null;
                  return (
                    <div className="decision-log__group" key={group.meeting.id}>
                      {!group.note && (
                        <button
                          className="text-button decision-log__meeting"
                          onClick={() => onOpenMeeting(group.meeting.id)}
                          type="button"
                        >
                          {dayWithWeekday(group.meeting.date)} · {group.meeting.title}
                        </button>
                      )}
                      {group.note && (
                        <p className="decision-log__compact">
                          <button className="text-button" onClick={() => onOpenMeeting(group.meeting.id)} type="button">
                            {monthDay(group.meeting.date)} · {group.meeting.title}
                          </button>
                          <span>　{group.note}</span>
                        </p>
                      )}
                      {lines.map((line) => (
                        <PlacedRow busy={busy} key={line.decisionId} line={line} onUndo={() => void undoPlace(line)} />
                      ))}
                    </div>
                  );
                }
                const unplacedOpen = openUnplaced.has(group.meeting.id);
                return (
                  <div className="decision-log__group" key={group.meeting.id}>
                    <button
                      className="text-button decision-log__meeting"
                      onClick={() => onOpenMeeting(group.meeting.id)}
                      type="button"
                    >
                      {dayWithWeekday(group.meeting.date)} · {group.meeting.title}
                    </button>
                    {group.decisions.map((entry) => row(entry, group, "none"))}
                    {lines.map((line) => (
                      <PlacedRow busy={busy} key={line.decisionId} line={line} onUndo={() => void undoPlace(line)} />
                    ))}
                    {group.unplaced.length > 0 && (
                      <div className="decision-log__unplaced">
                        <p>
                          这场会还有 {group.unplaced.length} 条决议没归到具体需求
                          <button
                            aria-expanded={unplacedOpen}
                            className="text-button"
                            onClick={() =>
                              setOpenUnplaced((current) => {
                                const next = new Set(current);
                                if (next.has(group.meeting.id)) next.delete(group.meeting.id);
                                else next.add(group.meeting.id);
                                return next;
                              })
                            }
                            type="button"
                          >
                            {unplacedOpen ? "收起" : "展开"}
                          </button>
                        </p>
                        {unplacedOpen && group.unplaced.map((entry) => row(entry, group, "picked"))}
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
          )}
        </>
      )}
    </section>
  );
}

function PlacedRow({ line, busy, onUndo }: { line: PlacedLine; busy: boolean; onUndo: () => void }) {
  return (
    <p className="decision-log__placed">
      <span>{line.text}</span>
      <button className="text-button" disabled={busy} onClick={onUndo} type="button">
        撤销
      </button>
    </p>
  );
}
