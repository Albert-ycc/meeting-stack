import { useCallback, useEffect, useRef, useState } from "react";

import type { ApiClient } from "../../api";
import { formatMonthDay, formatMonthDayClock } from "../../format";
import type { DroppedCandidates } from "../../types";
import { useBackdropDismiss, useDialogEscape, useDialogFocus } from "../useDialog";
import "./PoolDialogs.css";

interface DroppedCandidatesDialogProps {
  apiClient: ApiClient;
  onClose: () => void;
  /** 撤销了一条：需求池重新取 */
  onRestored: (title: string) => void;
}

const DAY_MS = 86_400_000;

/** 还剩几天可撤销：按 restore_until 往上取整，最后一天不到 24 小时也算 1 天（刚丢掉的是 30 天） */
export function restoreDaysLeft(restoreUntil: string, now: number): number {
  return Math.ceil((Date.parse(restoreUntil) - now) / DAY_MS);
}

/** 「刚刚丢掉」；再久一点写丢掉的日期和钟点 */
function droppedWhen(droppedAt: string | null | undefined, now: number): string {
  if (!droppedAt) return "";
  const elapsed = now - Date.parse(droppedAt);
  if (elapsed >= 0 && elapsed < 60_000) return "刚刚丢掉";
  return `${formatMonthDayClock(droppedAt)} 丢掉`;
}

function ClockIcon() {
  return (
    <svg aria-hidden="true" fill="none" height="12" stroke="currentColor" strokeLinecap="round" strokeWidth="1.5" viewBox="0 0 16 16" width="12">
      <circle cx="8" cy="8" r="6" />
      <path d="M8 4.6V8l2.2 1.4" />
    </svg>
  );
}

/**
 * 「已丢掉」抽屉（S01-c）：30 天内丢掉的候选，每条写候选名、项目、来源会议、丢掉时间和剩余天数，
 * 能撤销回待认领（R01-9、R01-15）。
 */
export function DroppedCandidatesDialog({ apiClient, onClose, onRestored }: DroppedCandidatesDialogProps) {
  const dialogRef = useRef<HTMLDivElement>(null);
  useDialogFocus(dialogRef);
  const [payload, setPayload] = useState<DroppedCandidates | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [error, setError] = useState("");
  const backdrop = useBackdropDismiss(onClose, busyId !== null);
  useDialogEscape(dialogRef, () => {
    if (!busyId) onClose();
  });

  const load = useCallback(async () => {
    try {
      setPayload(await apiClient.droppedCandidates());
    } catch (err) {
      setError(err instanceof Error ? err.message : "读取失败");
      setPayload({ items: [], total: 0, undo_days: 30 });
    }
  }, [apiClient]);

  useEffect(() => {
    void load();
  }, [load]);

  const restore = async (id: string, title: string) => {
    setBusyId(id);
    setError("");
    try {
      await apiClient.restoreCandidate(id);
      onRestored(title);
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "撤销失败");
    } finally {
      setBusyId(null);
    }
  };

  const now = Date.now();

  return (
    <div className="pool-drawer__overlay" {...backdrop}>
      <div aria-label="已丢掉的候选" aria-modal="true" className="pool-drawer" ref={dialogRef} role="dialog">
        <header className="pool-dialog__head">
          <div>
            <h2>
              已丢掉 {payload && payload.items.length > 0 && <span className="pool-drawer__count">{payload.items.length}</span>}
            </h2>
            <p>丢掉的候选保留 {payload?.undo_days ?? 30} 天，期间可以撤销回待认领</p>
          </div>
          <button aria-label="关闭" className="pool-dialog__close" onClick={onClose} type="button">
            ✕
          </button>
        </header>
        {payload === null ? (
          <p className="pool-dialog__state">正在读取…</p>
        ) : payload.items.length === 0 ? (
          <p className="pool-dialog__state">没有可以撤销的候选</p>
        ) : (
          <ul className="dropped-list">
            {payload.items.map((item) => {
              const daysLeft = restoreDaysLeft(item.restore_until, now);
              return (
                <li className="dropped-item" key={item.id}>
                  <div className="dropped-item__head">
                    <span className="dropped-item__title">{item.title}</span>
                    <button
                      className="dropped-item__restore"
                      disabled={busyId !== null}
                      onClick={() => void restore(item.id, item.title)}
                      type="button"
                    >
                      {busyId === item.id ? "撤销中…" : "撤销"}
                    </button>
                  </div>
                  <p className="dropped-item__project">
                    {typeof item.project_seat === "number" && <span className="dropped-item__seat">{item.project_seat}</span>}
                    {item.project_name ?? "未归项目"}
                  </p>
                  {item.source && (
                    <p className="dropped-item__source">
                      出自 {item.source.meeting_title}
                      {item.source.recording_date ? ` · ${formatMonthDay(item.source.recording_date)}` : ""}
                    </p>
                  )}
                  <p className="dropped-item__when">
                    <ClockIcon />
                    {droppedWhen(item.dropped_at, now)}
                    {Number.isFinite(daysLeft) && (daysLeft > 0 ? ` · 还剩 ${daysLeft} 天` : " · 已过期")}
                  </p>
                </li>
              );
            })}
          </ul>
        )}
        {error && (
          <p className="pool-dialog__error" role="alert">
            {error}
          </p>
        )}
      </div>
    </div>
  );
}
