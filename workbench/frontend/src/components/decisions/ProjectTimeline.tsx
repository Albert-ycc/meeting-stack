import { useCallback, useEffect, useRef, useState } from "react";

import {
  ApiError,
  isOldBackend,
  type ApiClient,
  type DecisionPlacementResult,
  type ProjectTimelinePayload,
  type TimelineDay,
  type TimelineItem,
  type TimelineKind,
} from "../../api";
import { formatTime } from "../../format";
import { useMiniPlayer, type MiniPlayerHandle } from "../graph/MiniPlayer";
import { useLinksFlags } from "../links/LinksFlagsContext";
import { LinksStateLine } from "../links/LinksStateLine";
import { OLD_BACKEND_TEXT } from "../links/useRelationAnswer";
import { NoticeBanner, UNDO_NOTICE_MS, useNotice } from "../Notice";
import { DecisionRow } from "./DecisionRow";
import { UNDONE_NOTICE, laterTail, monthDay, placedIntoNotice } from "./decisionText";
import "./decisions.css";

type TimelineApi = Pick<ApiClient, "meetingQuotes"> &
  Partial<Pick<ApiClient, "projectTimeline" | "placeDecision" | "retryLinks">>;

interface ProjectTimelineProps {
  apiClient: TimelineApi;
  projectId: string;
  canWrite: boolean;
  /** 会名、决议打开会议并从那里放；「还有 N 条」打开纪要 */
  onOpenMeeting: (meetingId: string, seekMs?: number, tab?: "transcript" | "minutes") => void;
  /** ［挂上文件夹］沿用项目页现有的按钮和流程，只在电脑上（没给时不出按钮） */
  onAttachRoot?: () => void;
  reloadKey?: number;
}

const FILTERS: Array<{ kind: TimelineKind; label: string }> = [
  { kind: "all", label: "全部" },
  { kind: "decisions", label: "决议" },
  { kind: "tasks", label: "任务" },
  { kind: "files", label: "文件" },
];
const EMPTY: Record<TimelineKind, string> = {
  all: "这个项目还没有会议、任务和文件的变化",
  decisions: "这个项目的会还没有列出决议",
  tasks: "还没有确认或完成的任务",
  files: "还没有记到文件的新增和修改",
};
const PAGE_DAYS = 7;
/** 一页的天都被滤掉（挪过来、回来了的文件）时自动往前再取，最多这么多页，免得第一页是空的 */
const EMPTY_PAGE_HOPS = 3;
/** 在等什么（资料盘没插、第一次收文件名、还在对比）时 15 秒重取 */
export const TIMELINE_WAIT_MS = 15_000;
const SHOWN_DECISIONS = 3;

function durationText(seconds: number | null): string {
  if (!seconds) return "";
  return `· ${Math.max(1, Math.round(seconds / 60))} 分钟`;
}

function namesText(names: string[], total: number): string {
  return `${names.join("、")}${total > names.length ? " 等" : ""}`;
}

/** 文件组那一行：「『能耗看板』里新增 5 个、改了 2 个：报价单_v3.xlsx、排期表.xlsx 等」 */
export function filesText(item: Extract<TimelineItem, { type: "files" }>): string {
  const place = item.folder ? `『${item.folder}』里` : "项目文件夹里";
  if (item.prelog) {
    const count = item.count ?? item.names.length;
    return `${place} ${count} 个文件最后一次修改在这天：${namesText(item.names, count)}`;
  }
  const parts = [item.added ? `新增 ${item.added} 个` : "", item.changed ? `改了 ${item.changed} 个` : ""].filter(Boolean);
  return `${place}${parts.join("、")}：${namesText(item.names, item.added + item.changed)}`;
}

/** 任务那一行：「确认了任务：写一版方案」「完成了 3 条任务：写一版方案、…」 */
export function tasksText(item: Extract<TimelineItem, { type: "tasks" }>): string {
  const verb = item.event === "done" ? "完成了" : "确认了";
  const total = item.tasks.length + item.more;
  const titles = item.tasks.map((task) => task.title).join("、");
  if (total <= 1) return `${verb}任务：${titles}`;
  return `${verb} ${total} 条任务：${titles}${item.more ? "、…" : ""}`;
}

function merge(pages: TimelineDay[], next: TimelineDay[]): TimelineDay[] {
  const seen = new Set(pages.map((day) => day.day));
  return [...pages, ...next.filter((day) => !seen.has(day.day))];
}

interface PlaceMenuProps {
  item: Extract<TimelineItem, { type: "decision" }>;
  requirements: Array<{ id: string; title: string }>;
  busy: boolean;
  onPick: (requirement: { id: string; title: string }) => void;
}

/** ［放到需求 ▾］：下拉先列这场会关联的需求 */
function PlaceMenu({ item, requirements, busy, onPick }: PlaceMenuProps) {
  const [open, setOpen] = useState(false);
  const linked = new Set(item.linked_requirement_ids);
  const ordered = [
    ...requirements.filter((requirement) => linked.has(requirement.id)),
    ...requirements.filter((requirement) => !linked.has(requirement.id)),
  ];
  if (!ordered.length) return null;
  return (
    <span className="timeline__place">
      <button aria-expanded={open} className="text-button" disabled={busy} onClick={() => setOpen((value) => !value)} type="button">
        放到需求 ▾
      </button>
      {open && (
        <span className="timeline__place-menu" role="menu">
          {ordered.map((requirement) => (
            <button
              className="text-button"
              key={requirement.id}
              onClick={() => {
                setOpen(false);
                onPick(requirement);
              }}
              role="menuitem"
              type="button"
            >
              {requirement.title}
            </button>
          ))}
        </span>
      )}
    </span>
  );
}

function PlayDecision({
  player,
  audioUrl,
  atMs,
  label,
}: {
  player: MiniPlayerHandle;
  audioUrl: string | null;
  atMs: number | null;
  label: string;
}) {
  if (!audioUrl || atMs === null) return null;
  const time = formatTime(atMs);
  return (
    <button aria-label={`从 ${time} 听这条`} className="decision-row__play" onClick={() => player.play(audioUrl, atMs, label)} type="button">
      ▶ {time}
    </button>
  );
}

/**
 * 项目页的时间线（4c）：放在「AI 自动建的项目」提示之后、「材料根目录」卡之前。按天列会议和定下的决议、
 * 确认和完成的任务、交付物、文件的新增和修改；［更早］按有动静的天往前翻。自己调一次 useMiniPlayer。
 * 旧后台（没有 projectTimeline，或接口回 FastAPI 的 404）时不画。
 */
export function ProjectTimeline({ apiClient, projectId, canWrite, onOpenMeeting, onAttachRoot, reloadKey = 0 }: ProjectTimelineProps) {
  const supported = typeof apiClient.projectTimeline === "function";
  const flags = useLinksFlags();
  const player = useMiniPlayer();
  const { notice, setNotice, dismissNotice } = useNotice();
  const [kind, setKind] = useState<TimelineKind>("all");
  const [state, setState] = useState<"loading" | "ready" | "error" | "hidden">(supported ? "loading" : "hidden");
  const [first, setFirst] = useState<ProjectTimelinePayload | null>(null);
  const [days, setDays] = useState<TimelineDay[]>([]);
  const [nextBefore, setNextBefore] = useState<string | null>(null);
  const [loadingMore, setLoadingMore] = useState(false);
  const [busy, setBusy] = useState(false);
  const request = useRef(0);
  // 换筛选、换项目、完整重读时加一：还在路上的［更早］回来时作废
  const generation = useRef(0);
  // 翻过［更早］：静默重取只换第一页那几天，翻出来的更早的天和翻页起点留着
  const paged = useRef(false);

  /** 取一页；这一页的天全被滤掉但还有更早的时自动往前取，最多 EMPTY_PAGE_HOPS 页 */
  const fetchPage = useCallback(
    async (before?: string): Promise<ProjectTimelinePayload | null> => {
      if (typeof apiClient.projectTimeline !== "function") return null;
      let payload = await apiClient.projectTimeline(projectId, before ? { kind, days: PAGE_DAYS, before } : { kind, days: PAGE_DAYS });
      for (let hop = 0; hop < EMPTY_PAGE_HOPS && !payload.days.length && payload.next_before; hop += 1) {
        payload = await apiClient.projectTimeline(projectId, { kind, days: PAGE_DAYS, before: payload.next_before });
      }
      return payload;
    },
    [apiClient, kind, projectId],
  );

  const load = useCallback(
    async (silent = false) => {
      if (typeof apiClient.projectTimeline !== "function") return;
      const ticket = ++request.current;
      if (!silent) {
        generation.current += 1;
        paged.current = false;
        setLoadingMore(false);
        setState("loading");
      }
      try {
        const payload = await fetchPage();
        if (ticket !== request.current || !payload) return;
        setFirst(payload);
        const boundary = payload.next_before;
        if (silent && paged.current && boundary) {
          // 第一页范围（boundary 及以后）换成新的，更早翻出来的天和翻页起点不动
          setDays((current) => [...payload.days, ...current.filter((day) => day.day < boundary)]);
        } else {
          paged.current = false;
          setDays(payload.days);
          setNextBefore(payload.next_before);
        }
        setState("ready");
      } catch (reason) {
        if (ticket !== request.current) return;
        if (isOldBackend(reason) || (reason instanceof ApiError && reason.status === 404)) setState("hidden");
        else if (!silent) setState("error");
      }
    },
    [apiClient, fetchPage],
  );

  useEffect(() => {
    void load();
  }, [load, reloadKey]);

  // 在等什么时 15 秒重取（和相关材料栏同一个规矩）
  const waiting = state === "ready" && first?.state.kind === "waiting";
  useEffect(() => {
    if (!waiting) return;
    const timer = window.setTimeout(() => void load(true), TIMELINE_WAIT_MS);
    return () => window.clearTimeout(timer);
  }, [waiting, first, load]);

  if (state === "hidden") return null;

  const more = async () => {
    if (!nextBefore || loadingMore || typeof apiClient.projectTimeline !== "function") return;
    // 票号：加载途中换了筛选（或整页重读）时，回来的这一页是旧筛选的，丢掉
    const ticket = generation.current;
    setLoadingMore(true);
    try {
      const payload = await fetchPage(nextBefore);
      if (ticket !== generation.current || !payload) return;
      paged.current = true;
      setDays((current) => merge(current, payload.days));
      setNextBefore(payload.next_before);
    } catch (reason) {
      if (ticket !== generation.current) return;
      setNotice(isOldBackend(reason) ? OLD_BACKEND_TEXT : reason instanceof Error ? reason.message : "没读到更早的", "warning");
    } finally {
      if (ticket === generation.current) setLoadingMore(false);
    }
  };

  const canPlace = canWrite && flags !== null && typeof apiClient.placeDecision === "function";

  const undoPlace = async (decisionId: string, undo: DecisionPlacementResult["undo"]) => {
    if (typeof apiClient.placeDecision !== "function") return;
    try {
      await apiClient.placeDecision(decisionId, undo);
      setNotice(UNDONE_NOTICE);
      await load(true);
    } catch (reason) {
      setNotice(reason instanceof Error ? reason.message : "撤销失败", reason instanceof ApiError ? "warning" : "error");
    }
  };

  const place = async (decisionId: string, requirement: { id: string; title: string }) => {
    if (busy || typeof apiClient.placeDecision !== "function") return;
    setBusy(true);
    try {
      const result = await apiClient.placeDecision(decisionId, { placement: "picked", requirement_id: requirement.id });
      setNotice(placedIntoNotice(requirement.title), "success", UNDO_NOTICE_MS, [
        { label: "撤销", onClick: () => void undoPlace(decisionId, result.undo) },
      ]);
      await load(true);
    } catch (reason) {
      if (isOldBackend(reason)) setNotice(OLD_BACKEND_TEXT, "warning");
      else setNotice(reason instanceof Error ? reason.message : "操作失败", reason instanceof ApiError ? "warning" : "error");
    } finally {
      setBusy(false);
    }
  };

  const renderItem = (item: TimelineItem, index: number) => {
    const time = item.time ? `${item.time} ` : "";
    if (item.type === "meeting") {
      const shown = item.decisions.slice(0, SHOWN_DECISIONS);
      const rest = item.decisions.length - shown.length + item.decisions_more;
      return (
        <li className="timeline__item" key={`m-${item.meeting.id}`}>
          <button className="text-button timeline__meeting" onClick={() => onOpenMeeting(item.meeting.id)} type="button">
            {time}会议『{item.meeting.title}』{durationText(item.meeting.duration_sec)}
          </button>
          {shown.length > 0 && (
            <ul className="timeline__decisions">
              {shown.map((decision) => (
                <li key={decision.id}>
                  <span className={decision.later ? "is-changed" : undefined}>
                    定了：{decision.text}
                    {decision.later && ` ${laterTail(decision.later.date)}`}
                  </span>
                  <PlayDecision
                    atMs={decision.start_ms}
                    audioUrl={item.meeting.audio_url}
                    label={item.meeting.title}
                    player={player}
                  />
                </li>
              ))}
              {rest > 0 && (
                <li>
                  <button className="text-button" onClick={() => onOpenMeeting(item.meeting.id, undefined, "minutes")} type="button">
                    还有 {rest} 条
                  </button>
                </li>
              )}
            </ul>
          )}
        </li>
      );
    }
    if (item.type === "tasks") {
      return (
        <li className="timeline__item" key={`t-${item.event}-${index}`}>
          {time}
          {tasksText(item)}
        </li>
      );
    }
    if (item.type === "deliverable") {
      return (
        <li className="timeline__item" key={`d-${item.deliverable.id}`}>
          {time}『{item.task.title}』的产出：{item.deliverable.name}
        </li>
      );
    }
    if (item.type === "files") {
      return (
        <li className={`timeline__item${item.prelog ? " timeline__item--prelog" : ""}`} key={`f-${item.root_id}-${item.folder}-${index}`}>
          {item.prelog ? "" : time}
          {filesText(item)}
        </li>
      );
    }
    return (
      <li className="timeline__item" key={`dec-${item.decision.id}`}>
        <DecisionRow
          apiClient={apiClient}
          canWrite={false}
          decision={{ id: item.decision.id, text: item.decision.text, detail: item.decision.detail, start_ms: item.decision.start_ms }}
          meeting={item.meeting}
          onOpenMeeting={(meetingId, seekMs) => onOpenMeeting(meetingId, seekMs)}
          player={player}
          tag={
            <>
              {item.decision.later && <span className="timeline__later">{laterTail(item.decision.later.date)}</span>}
              {item.requirement ? (
              <span className="timeline__tag">{item.requirement.title}</span>
            ) : (
              <span className="timeline__tag timeline__tag--none">
                没归到具体需求
                {canPlace && (
                  <PlaceMenu
                    busy={busy}
                    item={item}
                    onPick={(requirement) => void place(item.decision.id, requirement)}
                    requirements={first?.requirements ?? []}
                  />
                )}
              </span>
            )}
            </>
          }
        />
      </li>
    );
  };

  const logSince = first?.file_log_since ?? null;
  const oldest = days.length ? days[days.length - 1].day : null;
  const showLogSince =
    Boolean(logSince) && (kind === "all" || kind === "files") && (nextBefore === null || (oldest !== null && oldest < (logSince as string)));

  return (
    <section aria-label="时间线" className="detail-card timeline">
      {player.audioElement}
      <header className="detail-card__head">
        <h2>时间线</h2>
        <div aria-label="筛选" className="timeline__filters" role="group">
          {FILTERS.map((filter) => (
            <button
              aria-pressed={kind === filter.kind}
              className={`timeline__filter${kind === filter.kind ? " is-active" : ""}`}
              key={filter.kind}
              onClick={() => setKind(filter.kind)}
              type="button"
            >
              {filter.label}
            </button>
          ))}
        </div>
      </header>
      <div className="timeline__body">
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
            没读到时间线，稍后再试
            <button className="text-button" onClick={() => void load()} type="button">
              重试
            </button>
          </p>
        )}
        {state === "ready" && first && (
          <>
            <LinksStateLine
              apiClient={apiClient}
              onAction={onAttachRoot ? () => onAttachRoot() : undefined}
              onRetried={() => load(true)}
              state={first.state.text ? { kind: first.state.kind, text: first.state.text, action: first.state.action } : null}
            />
            {days.length === 0 ? (
              // 这几页的天都被滤掉了但更早还有：不说「还没有…」，只留［更早］
              nextBefore ? null : <p className="decision-log__empty">{EMPTY[kind]}</p>
            ) : (
              <ol className="timeline__days">
                {days.map((day) => (
                  <li className="timeline__day" key={day.day}>
                    <h3>{day.label}</h3>
                    <ul className="timeline__items">
                      {day.items.map(renderItem)}
                      {day.more_dirs > 0 && <li className="timeline__item timeline__item--muted">另有 {day.more_dirs} 个文件夹有变化</li>}
                    </ul>
                  </li>
                ))}
              </ol>
            )}
            {nextBefore && (
              <button className="text-button timeline__more" disabled={loadingMore} onClick={() => void more()} type="button">
                更早
              </button>
            )}
            {showLogSince && logSince && <p className="timeline__since">文件从 {monthDay(logSince)} 起记录</p>}
          </>
        )}
      </div>
    </section>
  );
}
