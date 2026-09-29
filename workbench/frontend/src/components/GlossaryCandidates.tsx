import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";

import { ApiError, isOldBackend, type ApiClient } from "../api";
import { formatTime } from "../format";
import type { GlossaryCandidateGroup, MaterialWord } from "../types";
import { formatMonthDay, matchesCandidateSearch } from "./glossaryModel";
import { IconBack, IconPlay, IconX, Kbd } from "./glossaryUi";
import { MATERIAL_WORDS_FOOTNOTE, WORD_GONE_TEXT, acceptLabel, answerFailure } from "./MaterialWords";

/*
 * 词典页的「待认词」：从各项目材料里找到、等你认的词。
 * 收件箱（所有项目汇总）和项目里的「从材料里找到的词」页签共用这里的数据和两块界面：
 * 中栏一行一个词，右栏是它在哪里出现过。回答走原来的三个接口，撤销窗口以服务端的 undo_until 为准。
 */

export interface CandidateEntry {
  /** 项目 id + "::" + key，全页唯一 */
  id: string;
  projectId: string;
  projectName: string;
  projectColor: string | null;
  word: MaterialWord;
}

export interface CandidateAnswer {
  state: "added" | "skipped";
  /** 服务端回的那句话 */
  text: string;
  /** 撤销期限；已经记过（already）或没有期限时为 null */
  undoUntil: string | null;
}

export const entryId = (projectId: string, key: string) => `${projectId}::${key}`;

/** 记到已有词条、听错的写法又全被去掉时，没东西可记 */
export function isBlocked(word: MaterialWord, removed: string[]): boolean {
  return Boolean(word.existing_term) && word.wrongs.length > 0 && word.wrongs.every((wrong) => removed.includes(wrong.text));
}

interface UseCandidatesOptions {
  apiClient: ApiClient;
  /** canWrite、第四期开关、三个回答接口都在，才有待认词这一整块 */
  enabled: boolean;
  onTermsChanged: () => void | Promise<void>;
  notify: (message: string, tone: "success" | "error", undoAction?: () => void) => void;
}

export function useGlossaryCandidates({ apiClient, enabled, onTermsChanged, notify }: UseCandidatesOptions) {
  // null：还没读到 / 接口不在（整块静默不出）
  const [groups, setGroups] = useState<GlossaryCandidateGroup[] | null>(null);
  // 收件箱这个汇总接口的状态；不在或出错时页面照常用，只是没有收件箱
  const [inboxState, setInboxState] = useState<"idle" | "ready" | "unavailable">("idle");
  const [answers, setAnswers] = useState<Record<string, CandidateAnswer>>({});
  const [removed, setRemoved] = useState<Record<string, string[]>>({});
  const [busy, setBusy] = useState<ReadonlySet<string>>(() => new Set());
  const inboxSeq = useRef(0);
  const projectSeq = useRef(0);

  const loadInbox = useCallback(async () => {
    const seq = ++inboxSeq.current;
    if (!enabled || typeof apiClient.glossaryCandidatesInbox !== "function") {
      setInboxState("unavailable");
      return;
    }
    try {
      const result = await apiClient.glossaryCandidatesInbox();
      if (seq !== inboxSeq.current) return;
      if (!Array.isArray(result?.projects)) {
        setInboxState("unavailable");
        return;
      }
      setGroups(result.projects);
      setAnswers({});
      setInboxState("ready");
    } catch {
      if (seq !== inboxSeq.current) return;
      setInboxState("unavailable");
    }
  }, [apiClient, enabled]);

  /** 进某个项目的「从材料里找到的词」时，以这个项目的接口为准 */
  const loadProject = useCallback(
    async (projectId: string, projectName: string, projectColor: string | null) => {
      const seq = ++projectSeq.current;
      if (!enabled || typeof apiClient.glossaryCandidates !== "function") return;
      try {
        const result = await apiClient.glossaryCandidates(projectId);
        if (seq !== projectSeq.current) return;
        if (!Array.isArray(result?.items)) return;
        setGroups((current) => {
          const next: GlossaryCandidateGroup = {
            project_id: projectId,
            project_name: projectName,
            project_color: projectColor,
            items: result.items,
            total: result.total ?? result.items.length,
          };
          const list = current ?? [];
          return list.some((group) => group.project_id === projectId)
            ? list.map((group) => (group.project_id === projectId ? next : group))
            : [...list, next];
        });
        setAnswers((current) => {
          const rest = { ...current };
          Object.keys(rest).forEach((id) => id.startsWith(`${projectId}::`) && delete rest[id]);
          return rest;
        });
      } catch {
        // 读不到就沿用收件箱里已有的
      }
    },
    [apiClient, enabled],
  );

  const entries = useMemo<CandidateEntry[]>(
    () =>
      (groups ?? []).flatMap((group) =>
        group.items.map((word) => ({
          id: entryId(group.project_id, word.key),
          projectId: group.project_id,
          projectName: group.project_name,
          projectColor: group.project_color,
          word,
        })),
      ),
    [groups],
  );

  const pendingIn = useCallback(
    (projectId: string) => {
      const group = groups?.find((item) => item.project_id === projectId);
      if (!group) return 0;
      const answered = group.items.filter((word) => answers[entryId(projectId, word.key)]).length;
      return Math.max(0, group.total - answered);
    },
    [answers, groups],
  );
  const pendingTotal = useMemo(
    () => (groups ?? []).reduce((sum, group) => sum + pendingIn(group.project_id), 0),
    [groups, pendingIn],
  );

  const markBusy = (id: string, on: boolean) =>
    setBusy((current) => {
      const next = new Set(current);
      if (on) next.add(id);
      else next.delete(id);
      return next;
    });

  const dropEntry = (entry: CandidateEntry) =>
    setGroups((current) =>
      (current ?? []).map((group) =>
        group.project_id === entry.projectId
          ? { ...group, items: group.items.filter((word) => word.key !== entry.word.key), total: Math.max(0, group.total - 1) }
          : group,
      ),
    );

  const fail = (entry: CandidateEntry, reason: unknown) => {
    if (reason instanceof ApiError && reason.status === 404 && !isOldBackend(reason)) {
      dropEntry(entry);
      notify(WORD_GONE_TEXT, "error");
    } else {
      notify(answerFailure(reason), "error");
    }
  };

  const undo = useCallback(
    async (entry: CandidateEntry) => {
      markBusy(entry.id, true);
      try {
        const result = await apiClient.undoGlossaryCandidate(entry.projectId, { key: entry.word.key });
        setAnswers((current) => {
          const rest = { ...current };
          delete rest[entry.id];
          return rest;
        });
        notify(result.text, "success");
        await onTermsChanged();
      } catch (reason) {
        fail(entry, reason);
      } finally {
        markBusy(entry.id, false);
      }
    },
    [apiClient, notify, onTermsChanged],
  );

  const accept = useCallback(
    async (entry: CandidateEntry) => {
      if (busy.has(entry.id) || answers[entry.id]) return;
      const gone = removed[entry.id] ?? [];
      if (isBlocked(entry.word, gone)) return;
      markBusy(entry.id, true);
      try {
        const result = await apiClient.acceptGlossaryCandidate(entry.projectId, { key: entry.word.key, not_wrong: gone });
        setAnswers((current) => ({
          ...current,
          [entry.id]: { state: "added", text: result.text, undoUntil: result.already ? null : result.undo_until },
        }));
        notify(result.text, "success", result.already ? undefined : () => void undo(entry));
        await onTermsChanged();
      } catch (reason) {
        fail(entry, reason);
      } finally {
        markBusy(entry.id, false);
      }
    },
    [answers, apiClient, busy, notify, onTermsChanged, removed, undo],
  );

  const reject = useCallback(
    async (entry: CandidateEntry) => {
      if (busy.has(entry.id) || answers[entry.id]) return;
      markBusy(entry.id, true);
      try {
        const result = await apiClient.rejectGlossaryCandidate(entry.projectId, { key: entry.word.key });
        setAnswers((current) => ({
          ...current,
          [entry.id]: { state: "skipped", text: result.text, undoUntil: result.undo_until },
        }));
        notify(result.text, "success", () => void undo(entry));
      } catch (reason) {
        fail(entry, reason);
      } finally {
        markBusy(entry.id, false);
      }
    },
    [answers, apiClient, busy, notify, undo],
  );

  const dropWrong = (id: string, wrong: string) =>
    setRemoved((current) => ({ ...current, [id]: [...(current[id] ?? []), wrong] }));

  return { groups, inboxState, entries, answers, removed, busy, pendingIn, pendingTotal, loadInbox, loadProject, accept, reject, undo, dropWrong };
}

export type CandidateStore = ReturnType<typeof useGlossaryCandidates>;

// ---------------------------------------------------------------------------
// 中栏
// ---------------------------------------------------------------------------

const INBOX_LIMIT = 5;

interface CandidateListProps {
  store: CandidateStore;
  /** inbox：按项目分组，每组先给 5 个；project：只有这个项目 */
  mode: "inbox" | "project";
  projectId: string | null;
  needle: string;
  selectedId: string | null;
  expanded: Record<string, boolean>;
  onExpand: (projectId: string) => void;
  onSelect: (id: string) => void;
  onOpenProject: (projectId: string) => void;
  canAnswer: boolean;
}

/** 收件箱里按项目分好组、被折叠掉之后真正显示的那些 id，键盘上下就走这个顺序 */
export function visibleCandidateIds(
  store: CandidateStore,
  mode: "inbox" | "project",
  projectId: string | null,
  needle: string,
  expanded: Record<string, boolean>,
): string[] {
  return candidateGroups(store, mode, projectId, needle).flatMap(({ group, items }) => {
    const limit = mode === "inbox" && !expanded[group.project_id] && !needle ? INBOX_LIMIT : items.length;
    return items.slice(0, limit).map((entry) => entry.id);
  });
}

function candidateGroups(store: CandidateStore, mode: "inbox" | "project", projectId: string | null, needle: string) {
  const source = mode === "inbox" ? store.groups ?? [] : (store.groups ?? []).filter((group) => group.project_id === projectId);
  return source
    .map((group) => ({
      group,
      items: store.entries.filter((entry) => entry.projectId === group.project_id && matchesCandidateSearch(entry.word, needle)),
    }))
    .filter(({ items }) => items.length > 0);
}

function rowInfo(word: MaterialWord, removed: string[]) {
  const wrongs = word.wrongs.map((wrong) => wrong.text).filter((text) => !removed.includes(text));
  if (wrongs.length) {
    return (
      <>
        听成 <b>{wrongs.join(" · ")}</b>
      </>
    );
  }
  const bits: ReactNode[] = [];
  if (word.spoken) bits.push(<span key="s">会上说过 <b>{word.spoken}</b> 次</span>);
  if (word.files) bits.push(<span key="f">在 <b>{word.files}</b> 个文件里</span>);
  return bits.flatMap((bit, index) => (index ? [" · ", bit] : [bit]));
}

export function CandidateList({
  store, mode, projectId, needle, selectedId, expanded, onExpand, onSelect, onOpenProject, canAnswer,
}: CandidateListProps) {
  const groups = candidateGroups(store, mode, projectId, needle);
  if (groups.length === 0) {
    return (
      <div className="gw-empty">
        <strong>{needle ? "没找到这个词" : "没有等你认的词了"}</strong>
        {needle ? "候选词和听错的写法里都没有。" : "新材料放进项目文件夹后，找到的词会出现在这里。"}
      </div>
    );
  }
  return (
    <div aria-label="待认词" role="listbox">
      {groups.map(({ group, items }) => {
        const limit = mode === "inbox" && !expanded[group.project_id] && !needle ? INBOX_LIMIT : items.length;
        return (
          <section key={group.project_id}>
            {mode === "inbox" && (
              <div className="gw-gh gw-gh--flat">
                <i className="gw-dot" style={{ background: group.project_color ?? undefined }} />
                <span>{group.project_name}</span>
                <span className="gw-gh__n">{store.pendingIn(group.project_id)}</span>
                <span className="gw-gh__sp" />
                <button className="gw-lb" onClick={() => onOpenProject(group.project_id)} type="button">
                  进入项目 ›
                </button>
              </div>
            )}
            {items.slice(0, limit).map((entry) => {
              const answer = store.answers[entry.id];
              const gone = store.removed[entry.id] ?? [];
              return (
                <div
                  aria-selected={selectedId === entry.id}
                  className={`gw-row gw-row--cand${answer ? " gw-row--done" : ""}`}
                  key={entry.id}
                  onClick={() => onSelect(entry.id)}
                  role="option"
                >
                  <span className="gw-t">{entry.word.term}</span>
                  <span className="gw-ci">{rowInfo(entry.word, gone)}</span>
                  {answer ? (
                    <span className={`gw-cstate gw-cstate--${answer.state === "added" ? "ok" : "skip"}`}>
                      {answer.state === "added" ? (entry.word.existing_term ? "已记到那条" : "已记入") : "不是"}
                    </span>
                  ) : canAnswer ? (
                    <span className="gw-cacts">
                      <button
                        aria-label={`${acceptLabel(entry.word, entry.projectName)}：${entry.word.term}`}
                        className="gw-btn gw-btn--xs"
                        disabled={store.busy.has(entry.id) || isBlocked(entry.word, gone)}
                        onClick={(event) => {
                          event.stopPropagation();
                          void store.accept(entry);
                        }}
                        type="button"
                      >
                        <Kbd>1</Kbd>记入
                      </button>
                      <button
                        aria-label={`不是：${entry.word.term}`}
                        className="gw-btn gw-btn--xs"
                        disabled={store.busy.has(entry.id)}
                        onClick={(event) => {
                          event.stopPropagation();
                          void store.reject(entry);
                        }}
                        type="button"
                      >
                        <Kbd>2</Kbd>不是
                      </button>
                    </span>
                  ) : (
                    <span />
                  )}
                </div>
              );
            })}
            {items.length > limit && (
              <div className="gw-row gw-row--more">
                <button onClick={() => onExpand(group.project_id)} type="button">
                  还有 {items.length - limit} 个
                </button>
              </div>
            )}
          </section>
        );
      })}
      <p className="gw-note">{MATERIAL_WORDS_FOOTNOTE}</p>
    </div>
  );
}

// ---------------------------------------------------------------------------
// 右栏
// ---------------------------------------------------------------------------

function HighlightedQuote({ quote, marks }: { quote: string; marks: string[] }) {
  const words = marks.filter((mark) => mark && quote.includes(mark));
  if (words.length === 0) return <>{quote}</>;
  const pattern = new RegExp(`(${words.map((word) => word.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("|")})`, "g");
  return (
    <>
      {quote.split(pattern).map((part, index) =>
        words.includes(part) ? (
          <mark className="gw-hl" key={index}>
            {part}
          </mark>
        ) : (
          part
        ),
      )}
    </>
  );
}

interface CandidateDetailProps {
  store: CandidateStore;
  entry: CandidateEntry | null;
  canAnswer: boolean;
  /** 已选好的词条名，用来在「词典里已有『X』」旁边写它在哪个范围 */
  existingScope: (termId: string) => string;
  pendingLeft: number;
  onOpenMeeting?: (meetingId: string, seekMs: number) => void;
  onBack?: () => void;
  onAccept: (entry: CandidateEntry) => void;
  onReject: (entry: CandidateEntry) => void;
}

export function CandidateDetail({
  store, entry, canAnswer, existingScope, pendingLeft, onOpenMeeting, onBack, onAccept, onReject,
}: CandidateDetailProps) {
  const [, setTick] = useState(0);
  const answer = entry ? store.answers[entry.id] : undefined;
  // 撤销期限一到，[撤销] 自己收起
  useEffect(() => {
    if (!answer?.undoUntil) return;
    const left = Date.parse(answer.undoUntil) - Date.now();
    if (left <= 0) return;
    const timer = window.setTimeout(() => setTick((value) => value + 1), left + 50);
    return () => window.clearTimeout(timer);
  }, [answer?.undoUntil]);

  if (!entry) {
    return (
      <div className="gw-dempty">
        <div>
          {pendingLeft > 0 ? "选一个候选词看它在哪里出现过" : "都处理完了"}
          <div className="gw-keys">
            <Kbd>J</Kbd>
            <span>下一个</span>
            <Kbd>1</Kbd>
            <span>记入</span>
            <Kbd>2</Kbd>
            <span>不是</span>
          </div>
        </div>
      </div>
    );
  }

  const { word } = entry;
  const gone = store.removed[entry.id] ?? [];
  const wrongs = word.wrongs.filter((wrong) => !gone.includes(wrong.text));
  const quoteFile = word.file_quote ? word.file_names.find((file) => file.file_id === word.file_quote?.file_id) : undefined;
  const blocked = isBlocked(word, gone);
  const busy = store.busy.has(entry.id);
  const canUndo = answer?.undoUntil ? Date.parse(answer.undoUntil) > Date.now() : false;
  const marks = [...wrongs.map((wrong) => wrong.text), word.term];

  return (
    <>
      <div className="gw-dscroll">
        <div className="gw-crumb">
          {onBack && (
            <button className="gw-btn gw-btn--sm gw-btn--ghost gw-mback" onClick={onBack} type="button">
              <IconBack />
              列表
            </button>
          )}
          <i className="gw-dot" style={{ background: entry.projectColor ?? undefined }} />
          <span>从 {entry.projectName} 的材料里找到</span>
        </div>
        <h2 className="gw-dtitle gw-dtitle--static">{word.term}</h2>
        <p className="gw-hint">
          {word.existing_term
            ? `词典里已有『${word.existing_term.term}』（${existingScope(word.existing_term.id)}），记入会把听错的写法加到那条`
            : "词典里还没有这个词"}
        </p>

        {answer ? (
          <div className={`gw-statebar gw-statebar--${answer.state === "added" ? "ok" : "skip"}`} role="status">
            <span>{answer.text}</span>
            <span className="gw-statebar__sp" />
            {canAnswer && canUndo && (
              <button className="gw-lb" disabled={busy} onClick={() => void store.undo(entry)} type="button">
                撤销
              </button>
            )}
          </div>
        ) : (
          canAnswer && (
            <div className="gw-cactsbar">
              <button
                className="gw-btn gw-btn--pri"
                disabled={busy || blocked}
                onClick={() => onAccept(entry)}
                title={blocked ? "听错的写法都去掉了，没东西可记" : undefined}
                type="button"
              >
                {acceptLabel(word, entry.projectName)} <Kbd>1</Kbd>
              </button>
              <button className="gw-btn" disabled={busy} onClick={() => onReject(entry)} type="button">
                不是 <Kbd>2</Kbd>
              </button>
            </div>
          )
        )}

        {word.wrongs.length > 0 && (
          <div className="gw-block">
            <div className="gw-block__label">
              会上可能听成了 <em>去掉不是听错的</em>
            </div>
            <div className="gw-chips">
              {wrongs.length > 0 ? (
                wrongs.map((wrong) => (
                  <span className={`gw-chip${answer || !canAnswer ? " gw-chip--ro" : ""}`} key={wrong.text} title={`${wrong.meetings} 场会里听到`}>
                    『{wrong.text}』<small>{wrong.meetings} 场</small>
                    {canAnswer && !answer && (
                      <button
                        aria-label={`不是听错：${wrong.text}`}
                        disabled={busy}
                        onClick={() => store.dropWrong(entry.id, wrong.text)}
                        type="button"
                      >
                        <IconX />
                      </button>
                    )}
                  </span>
                ))
              ) : (
                <span className="gw-hint">都去掉了</span>
              )}
            </div>
          </div>
        )}

        {word.heard.length > 0 ? (
          <div className="gw-block">
            <div className="gw-block__label">
              会上原话 {word.spoken > 0 && <em>会上说过 {word.spoken} 次</em>}
            </div>
            {word.heard.map((heard) => (
              <div className="gw-quote" key={`${heard.meeting.id}-${heard.start_ms}`}>
                <q>
                  <HighlightedQuote marks={marks} quote={heard.quote} />
                </q>
                <div className="gw-quote__meta">
                  <span>
                    {heard.meeting.title} · {formatMonthDay(heard.meeting.date)}
                  </span>
                  <button className="gw-tchip" onClick={() => onOpenMeeting?.(heard.meeting.id, heard.start_ms)} type="button">
                    <IconPlay />
                    {formatTime(heard.start_ms, true)}
                  </button>
                </div>
              </div>
            ))}
          </div>
        ) : (
          word.spoken > 0 && (
            <div className="gw-block">
              <span className="gw-stat">
                会上说过 <b>{word.spoken}</b> 次
              </span>
            </div>
          )
        )}

        {(word.files > 0 || word.file_quote) && (
          <div className="gw-block">
            <div className="gw-block__label">材料来源</div>
            {word.file_quote && quoteFile && (
              <div className="gw-src">
                『…{word.file_quote.quote}…』<span className="gw-src__fn"> · {quoteFile.name}</span>
              </div>
            )}
            {word.files > 0 && (!word.file_quote || word.files > 1) && (
              <div className={`gw-src${word.file_quote ? " gw-src--gap" : ""}`}>
                <span className="gw-src__fn">在 {word.files} 个文件里{word.file_names.length > 0 ? "：" : ""}</span>
                {word.file_names.map((file) => file.name).join("、")}
                {word.files > 2 && word.file_names.length > 0 && " 等"}
              </div>
            )}
          </div>
        )}
        <p className="gw-foot-note">{MATERIAL_WORDS_FOOTNOTE}</p>
      </div>
    </>
  );
}
