import { useCallback, useEffect, useRef, useState } from "react";

import type { ApiClient } from "../../api";
import { formatMonthDayClock, formatTime } from "../../format";
import type {
  LinkOption,
  ReviewCard,
  ReviewCardCandidate,
  ReviewCardTask,
  ReviewCardsPayload,
  Task,
} from "../../types";
import { MergeCandidateDialog } from "../pool/MergeCandidateDialog";
import { LinkPicker } from "./LinkPicker";
import "./ReviewCardsPanel.css";

/** 待确认页签：按会议的审核卡（R07-9～11、16）。 */
export interface ReviewCardsPanelProps {
  apiClient: ApiClient;
  canWrite: boolean;
  /** 会议这一层的筛选，跟着页面的筛选走：所属项目（逗号分隔多选，none 是未归项目）、会议日期 */
  filters: { project_id?: string; meeting_date_from?: string; meeting_date_to?: string };
  /** 页面别处有写操作（比如在修改弹窗里存了）时递增，面板重新取数 */
  reloadKey: number;
  onOpenMeeting: (meetingId: string, seekMs?: number) => void;
  /** 候选「认领」：进需求池的认领二级页 */
  onClaimCandidate: (candidateId: string) => void;
  /** 「修改」任务：页面打开修改弹窗 */
  onEditTask: (task: Task) => void;
  /** 面板里有写操作以后，页面刷新页签计数 */
  onChanged: () => void;
  /** 结果提示：显示在页面统一的提示条里；给了 undo 就带［撤销］ */
  /** tone 缺省按成功样式；操作失败给 "error" */
  onNotify: (message: string, undo?: () => Promise<void>, tone?: "success" | "error") => void;
}

const POLL_MS = 20_000;
/** 和后端 UNDO_WINDOW_SECONDS=600 对齐：确认、驳回只在这段时间内能撤销 */
const UNDO_WINDOW_MS = 10 * 60 * 1000;

/** 不另起计时器，跟着重新取数、轮询的重渲染刷新 */
function withinUndoWindow(task: ReviewCardTask): boolean {
  const changedAt = new Date(task.status_changed_at).getTime();
  return Number.isFinite(changedAt) && Date.now() - changedAt <= UNDO_WINDOW_MS;
}

const errorText = (err: unknown) => (err instanceof Error && err.message ? err.message : "请稍后重试");

/** 卡上改选的挂接，连同选的那一刻任务已挂的需求/候选一起记下：别处（修改弹窗）改过挂接，这条选择就作废 */
type PickMap = Record<string, { option: LinkOption | null; snapshot: string }>;

const linkSnapshot = (task: ReviewCardTask) => `${task.requirement_id ?? ""}|${task.candidate_id ?? ""}`;

function hasPick(task: ReviewCardTask, picks: PickMap): boolean {
  return task.id in picks && picks[task.id].snapshot === linkSnapshot(task);
}

/** 有效的手选用手选（「不挂」是 null）；否则按后端推荐，后端把任务当前挂着的排在第一 */
function pickedOption(task: ReviewCardTask, picks: PickMap): LinkOption | null {
  return hasPick(task, picks) ? picks[task.id].option : (task.recommended[0] ?? null);
}

function confirmBody(option: LinkOption | null) {
  if (option?.kind === "requirement") return { requirement_id: option.id };
  if (option?.kind === "candidate") return { candidate_id: option.id };
  return { requirement_id: null, candidate_id: null };
}

function linkedName(task: ReviewCardTask): string {
  if (task.requirement_title) return task.requirement_title;
  if (task.candidate_title) return `候选：${task.candidate_title}`;
  return "未挂需求";
}

function FlagIcon() {
  return (
    <svg aria-hidden="true" className="review-link__flag" fill="none" height="12" stroke="currentColor" strokeLinecap="round" strokeWidth="1.5" viewBox="0 0 16 16" width="12">
      <path d="M3.5 14V2.5" />
      <path d="M3.5 2.5h8l-1.6 3 1.6 3h-8" />
    </svg>
  );
}

function SeekButton({ ms, label, onClick }: { ms: number; label: string; onClick: () => void }) {
  return (
    <button aria-label={label} className="review-seek" onClick={onClick} type="button">
      <svg aria-hidden="true" height="8" viewBox="0 0 8 8" width="8">
        <path d="M1 0.5l6 3.5-6 3.5z" fill="currentColor" />
      </svg>
      原话 {formatTime(ms, true)}
    </button>
  );
}

function metaLine(card: ReviewCard): string {
  const { meeting } = card;
  const parts = [formatMonthDayClock(meeting.recording_date)];
  if (meeting.duration_ms) parts.push(`${Math.max(1, Math.round(meeting.duration_ms / 60000))} 分钟`);
  if (meeting.project_name) parts.push(meeting.project_name);
  return parts.join(" · ");
}

export function ReviewCardsPanel({
  apiClient,
  canWrite,
  filters,
  reloadKey,
  onOpenMeeting,
  onClaimCandidate,
  onEditTask,
  onChanged,
  onNotify,
}: ReviewCardsPanelProps) {
  const [payload, setPayload] = useState<ReviewCardsPayload | null>(null);
  const [failed, setFailed] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  // 同一帧里连点两下，state 还没刷新，只有 ref 拦得住
  const busyRef = useRef<string | null>(null);
  // 任务 id → 用户改选的挂接对象（null＝不挂）；没改选的按推荐
  const [picks, setPicks] = useState<PickMap>({});
  const [pickerTaskId, setPickerTaskId] = useState<string | null>(null);
  const [mergeCandidate, setMergeCandidate] = useState<ReviewCardCandidate | null>(null);
  // 处理完的卡默认收起；在卡上操作过的卡保持展开，免得最后一条处理完卡片突然收走、行内撤销跟着消失
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const seqRef = useRef(0);

  const { project_id, meeting_date_from, meeting_date_to } = filters;
  const load = useCallback(async () => {
    const seq = ++seqRef.current;
    try {
      const next = await apiClient.reviewCards({ project_id, meeting_date_from, meeting_date_to });
      if (seq !== seqRef.current) return;
      setPayload(next);
      setFailed(false);
    } catch {
      if (seq === seqRef.current) setFailed(true);
    }
  }, [apiClient, project_id, meeting_date_from, meeting_date_to]);

  useEffect(() => {
    void load();
  }, [load, reloadKey]);

  useEffect(() => {
    const interval = window.setInterval(() => {
      if (!document.hidden) void load();
    }, POLL_MS);
    return () => window.clearInterval(interval);
  }, [load]);

  const refreshed = async () => {
    await load();
    onChanged();
  };

  const expand = (meetingId: string) => setExpanded((current) => new Set(current).add(meetingId));
  const collapse = (meetingId: string) =>
    setExpanded((current) => {
      const next = new Set(current);
      next.delete(meetingId);
      return next;
    });

  const run = async (key: string, meetingId: string, failure: string, action: () => Promise<void>) => {
    if (busyRef.current) return;
    busyRef.current = key;
    setBusy(key);
    expand(meetingId);
    try {
      await action();
    } catch (err) {
      onNotify(`${failure}：${errorText(err)}`, undefined, "error");
      // 失败多半是行状态已被别处改了，立刻按最新的重画
      void load();
    } finally {
      busyRef.current = null;
      setBusy(null);
    }
  };

  /** 撤销：成功给「已撤销」；没撤成（过了窗口、状态变了）按错误报。都按最新状态重画 */
  const undoReview = (ids: string[], done: string) => async () => {
    try {
      const result = await apiClient.undoTaskReview(ids);
      await refreshed();
      if (result.failed.length > 0) {
        onNotify(`撤销失败：${[...new Set(result.failed.map((item) => item.error))].join("；")}`, undefined, "error");
      } else {
        onNotify(done);
      }
    } catch (err) {
      onNotify(`撤销失败：${errorText(err)}`, undefined, "error");
      void load();
    }
  };

  const confirmTask = (card: ReviewCard, task: ReviewCardTask) =>
    run(`confirm:${task.id}`, card.meeting.id, "确认失败", async () => {
      const option = pickedOption(task, picks);
      await apiClient.confirmTask(task.id, confirmBody(option));
      await refreshed();
      onNotify(
        option ? `已确认，挂到「${option.title}」` : "已确认，未挂需求",
        undoReview([task.id], "已撤销确认"),
      );
    });

  const rejectTask = (card: ReviewCard, task: ReviewCardTask) =>
    run(`reject:${task.id}`, card.meeting.id, "驳回失败", async () => {
      await apiClient.rejectTask(task.id);
      await refreshed();
    });

  const undoRow = (card: ReviewCard, task: ReviewCardTask, done: string) =>
    run(`undo:${task.id}`, card.meeting.id, "撤销失败", undoReview([task.id], done));

  const confirmAll = (card: ReviewCard) =>
    run(`all:${card.meeting.id}`, card.meeting.id, "全部确认失败", async () => {
      const pending = card.tasks.filter((task) => task.status === "pending_confirm");
      // 用户在某行改选过挂接（和推荐不同）的，先按他选的逐条确认，别被「各自按推荐」盖掉
      const manual = pending.filter(
        (task) =>
          hasPick(task, picks) && (pickedOption(task, picks)?.id ?? null) !== (task.recommended[0]?.id ?? null),
      );
      const confirmedIds: string[] = [];
      const reasons: string[] = [];
      let linked = 0;
      let failedCount = 0;
      for (const task of manual) {
        try {
          const option = pickedOption(task, picks);
          await apiClient.confirmTask(task.id, confirmBody(option));
          confirmedIds.push(task.id);
          if (option) linked += 1;
        } catch (err) {
          failedCount += 1;
          reasons.push(errorText(err));
        }
      }
      // 有手选的没确认上时不再批量：批量会按推荐把它挂上，违背他选的
      const rest = pending.length - manual.length;
      if (failedCount === 0 && rest > 0) {
        try {
          const result = await apiClient.confirmAllInMeeting(card.meeting.id);
          confirmedIds.push(...result.confirmed);
          linked += result.confirmed.filter((id) => result.linked[id]).length;
          failedCount += result.failed.length;
          reasons.push(...result.failed.map((item) => item.error));
        } catch (err) {
          failedCount += rest;
          reasons.push(errorText(err));
        }
      }
      await refreshed();
      const reasonText = [...new Set(reasons)].join("；");
      if (confirmedIds.length === 0) {
        if (failedCount > 0) onNotify(`${failedCount} 条没确认上：${reasonText}`, undefined, "error");
        return;
      }
      const base = `已确认 ${confirmedIds.length} 条任务，${linked > 0 ? `其中 ${linked} 条挂到需求` : "没有挂需求"}`;
      onNotify(
        failedCount > 0 ? `${base}；${failedCount} 条没确认上：${reasonText}` : base,
        undoReview(confirmedIds, `已撤销 ${confirmedIds.length} 条确认`),
        failedCount > 0 ? "error" : undefined,
      );
    });

  const dropCandidate = (card: ReviewCard, candidate: ReviewCardCandidate) =>
    run(`drop:${candidate.id}`, card.meeting.id, "丢掉失败", async () => {
      await apiClient.dropCandidate(candidate.id);
      await refreshed();
      onNotify(`已丢掉「${candidate.title}」`, async () => {
        try {
          await apiClient.restoreCandidate(candidate.id);
          await refreshed();
          onNotify(`已撤销丢掉「${candidate.title}」`);
        } catch (err) {
          onNotify(`撤销失败：${errorText(err)}`, undefined, "error");
          void load();
        }
      });
    });

  const renderCandidate = (card: ReviewCard, candidate: ReviewCardCandidate) => {
    const handled = candidate.status !== "pending";
    const source = candidate.source;
    const note =
      candidate.status === "claimed"
        ? `已认领为「${candidate.requirement_title ?? ""}」`
        : candidate.status === "merged"
          ? `已合并到「${candidate.requirement_title ?? ""}」`
          : "已丢掉";
    return (
      <li className={`review-candidate ${handled ? "is-handled" : ""}`} key={candidate.id}>
        <span className="review-tag">AI 候选</span>
        <strong className="review-candidate__title">{candidate.title}</strong>
        <span className="review-candidate__summary">{candidate.summary}</span>
        {source?.anchor_ms != null && (
          <SeekButton
            label={`播放「${candidate.title}」的原话`}
            ms={source.anchor_ms}
            onClick={() => onOpenMeeting(source.meeting_id, source.anchor_ms ?? undefined)}
          />
        )}
        {handled ? (
          <span className="review-state">{note}</span>
        ) : (
          canWrite && (
            <span className="review-candidate__actions">
              <button
                aria-label={`丢掉「${candidate.title}」`}
                className="review-text-button"
                disabled={busy !== null}
                onClick={() => void dropCandidate(card, candidate)}
                type="button"
              >
                丢掉
              </button>
              {candidate.can_merge && (
                <button
                  aria-label={`合并「${candidate.title}」`}
                  className="review-button"
                  disabled={busy !== null}
                  onClick={() => setMergeCandidate(candidate)}
                  type="button"
                >
                  合并
                </button>
              )}
              <button
                aria-label={`认领「${candidate.title}」`}
                className="review-button review-button--dark"
                disabled={busy !== null}
                onClick={() => onClaimCandidate(candidate.id)}
                type="button"
              >
                认领 →
              </button>
            </span>
          )
        )}
      </li>
    );
  };

  const renderLinkCell = (task: ReviewCardTask) => {
    if (task.status === "cancelled") return <span className="review-muted">—</span>;
    if (task.status !== "pending_confirm") {
      return (
        <span className="review-link review-link--static">
          {task.requirement_title && <FlagIcon />}
          <span className="review-link__name">{linkedName(task)}</span>
        </span>
      );
    }
    const option = pickedOption(task, picks);
    const recommendedHere = option !== null && task.recommended.some((item) => item.id === option.id);
    const label = option ? option.title : "不挂需求";
    if (!canWrite) {
      return (
        <span className="review-link review-link--static">
          <span className="review-link__name">{label}</span>
          {recommendedHere && <span className="review-recommend">推荐</span>}
        </span>
      );
    }
    const open = pickerTaskId === task.id;
    return (
      <div className="review-link-wrap">
        <button
          aria-expanded={open}
          aria-haspopup="dialog"
          aria-label={`挂到需求：${label}`}
          className="review-link review-link--trigger"
          // 选择器靠「点到外面就收起」，按在触发按钮上要让它自己收，不然刚收起又被 click 打开
          onMouseDown={(event) => event.stopPropagation()}
          onClick={() => setPickerTaskId(open ? null : task.id)}
          type="button"
        >
          {option?.kind === "candidate" && <span className="review-link__kind">候选</span>}
          <span className="review-link__name">{label}</span>
          {recommendedHere && <span className="review-recommend">推荐</span>}
          <svg aria-hidden="true" className="review-link__caret" fill="none" height="10" stroke="currentColor" strokeLinecap="round" strokeWidth="1.6" viewBox="0 0 12 12" width="10">
            <path d="M2.5 4.5L6 8l3.5-3.5" />
          </svg>
        </button>
        {open && (
          <LinkPicker
            apiClient={apiClient}
            onClose={() => setPickerTaskId(null)}
            onPick={(picked) => {
              setPicks((current) => ({ ...current, [task.id]: { option: picked, snapshot: linkSnapshot(task) } }));
              setPickerTaskId(null);
            }}
            projectName={task.project_name}
            selectedId={option?.id ?? null}
            taskId={task.id}
          />
        )}
      </div>
    );
  };

  const renderTask = (card: ReviewCard, task: ReviewCardTask) => {
    const rejected = task.status === "cancelled";
    const pending = task.status === "pending_confirm";
    const anchor = task.anchor_ms;
    return (
      <tr className={`review-task ${pending ? "" : "is-handled"} ${rejected ? "is-rejected" : ""}`} key={task.id}>
        <td className="review-task__title">{task.title}</td>
        <td>
          <span className="review-assignee">{task.assignee === "me" ? "我" : "AI"}</span>
        </td>
        <td className="review-task__due">{task.due_date ? task.due_date.slice(5, 10) : "截止未定"}</td>
        <td>
          {anchor != null ? (
            <SeekButton
              label={`播放「${task.title}」的原话`}
              ms={anchor}
              onClick={() => onOpenMeeting(task.meeting_id ?? card.meeting.id, anchor)}
            />
          ) : (
            <span className="review-muted">—</span>
          )}
        </td>
        <td className="review-task__link">{renderLinkCell(task)}</td>
        <td className="review-task__actions">
          {pending ? (
            canWrite && (
              <span className="review-task__buttons">
                <button
                  aria-label={`确认「${task.title}」`}
                  className="review-button"
                  disabled={busy !== null}
                  onClick={() => void confirmTask(card, task)}
                  type="button"
                >
                  确认
                </button>
                <button
                  aria-label={`驳回「${task.title}」`}
                  className="review-text-button"
                  disabled={busy !== null}
                  onClick={() => void rejectTask(card, task)}
                  type="button"
                >
                  驳回
                </button>
                <button
                  aria-label={`修改「${task.title}」`}
                  className="review-text-button"
                  onClick={() => onEditTask(task)}
                  type="button"
                >
                  修改
                </button>
              </span>
            )
          ) : rejected ? (
            <span className="review-task__buttons">
              <span className="review-state">已驳回</span>
              {canWrite && withinUndoWindow(task) && (
                <button
                  aria-label={`撤销驳回「${task.title}」`}
                  className="review-text-button review-text-button--signal"
                  disabled={busy !== null}
                  onClick={() => void undoRow(card, task, "已撤销驳回")}
                  type="button"
                >
                  撤销
                </button>
              )}
            </span>
          ) : (
            <span className="review-task__buttons">
              <span className="review-state review-state--ok">已确认 ✓</span>
              {canWrite && task.status === "confirmed" && withinUndoWindow(task) && (
                <button
                  aria-label={`撤销确认「${task.title}」`}
                  className="review-text-button review-text-button--signal"
                  disabled={busy !== null}
                  onClick={() => void undoRow(card, task, "已撤销确认")}
                  type="button"
                >
                  撤销
                </button>
              )}
            </span>
          )}
        </td>
      </tr>
    );
  };

  const renderCard = (card: ReviewCard) => {
    const { meeting } = card;
    if (card.done && !expanded.has(meeting.id)) {
      return (
        <section aria-label={meeting.title} className="review-card is-done is-collapsed" key={meeting.id}>
          <h3 className="review-card__title">{meeting.title}</h3>
          <span className="review-card__meta">{metaLine(card)}</span>
          <span className="review-card__counts">
            需求候选 {card.candidates.length} · 任务 {card.tasks.length}
          </span>
          <button
            aria-label={`展开「${meeting.title}」`}
            className="review-text-button review-text-button--signal"
            onClick={() => expand(meeting.id)}
            type="button"
          >
            展开
          </button>
        </section>
      );
    }
    return (
      <section aria-label={meeting.title} className={`review-card ${card.done ? "is-done" : ""}`} key={meeting.id}>
        <header className="review-card__head">
          <h3 className="review-card__title">{meeting.title}</h3>
          <span className="review-card__meta">{metaLine(card)}</span>
          <span className="review-card__side">
            {card.done && (
              <button className="review-text-button" onClick={() => collapse(meeting.id)} type="button">
                收起
              </button>
            )}
            <button className="review-open" onClick={() => onOpenMeeting(meeting.id)} type="button">
              打开会议 →
            </button>
          </span>
        </header>

        {card.candidates.length > 0 && (
          <div className="review-section">
            <h4 className="review-section__title">
              需求候选 <span>{card.pending_candidate_count}</span>
            </h4>
            <ul className="review-candidates">{card.candidates.map((candidate) => renderCandidate(card, candidate))}</ul>
          </div>
        )}

        {card.tasks.length > 0 && (
          <div className="review-section">
            <h4 className="review-section__title">
              待确认任务 <span>{card.pending_task_count}</span>
            </h4>
            <table className="review-tasks">
              <thead>
                <tr>
                  <th>任务</th>
                  <th>负责人</th>
                  <th>截止</th>
                  <th>原话</th>
                  <th>挂到需求</th>
                  <th>操作</th>
                </tr>
              </thead>
              <tbody>{card.tasks.map((task) => renderTask(card, task))}</tbody>
            </table>
          </div>
        )}

        {canWrite && (
          <footer className="review-card__foot">
            <span>剩下的待确认任务按推荐一并确认，10 分钟内可撤销</span>
            <button
              className="review-button review-button--signal"
              disabled={busy !== null || card.pending_task_count === 0}
              onClick={() => void confirmAll(card)}
              type="button"
            >
              全部确认
            </button>
          </footer>
        )}
      </section>
    );
  };

  if (payload === null) {
    return (
      <div className="review-cards">
        <p className="review-empty">{failed ? "没取到待确认的内容，稍后会自动重试。" : "正在读取…"}</p>
      </div>
    );
  }

  return (
    <div className="review-cards">
      {payload.cards.length === 0 ? (
        <p className="review-empty">没有要审的会。会议纪要生成后，AI 抽出的任务和需求候选会按会议列在这里。</p>
      ) : (
        payload.cards.map(renderCard)
      )}
      {mergeCandidate && (
        <MergeCandidateDialog
          apiClient={apiClient}
          candidate={{
            id: mergeCandidate.id,
            title: mergeCandidate.title,
            project_name: mergeCandidate.project_name,
          }}
          onClose={() => setMergeCandidate(null)}
          onMerged={(requirement) => {
            const owner = payload.cards.find((card) => card.candidates.some((item) => item.id === mergeCandidate.id));
            if (owner) expand(owner.meeting.id);
            setMergeCandidate(null);
            void refreshed();
            onNotify(`已合并到「${requirement.title}」`);
          }}
        />
      )}
    </div>
  );
}
