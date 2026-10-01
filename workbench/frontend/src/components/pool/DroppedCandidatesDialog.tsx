import { useCallback, useEffect, useRef, useState } from "react";

import type { ApiClient } from "../../api";
import { formatMonthDay } from "../../format";
import type { DroppedCandidates } from "../../types";
import { useDialogFocus } from "../useDialog";
import "./PoolDialogs.css";

interface DroppedCandidatesDialogProps {
  apiClient: ApiClient;
  onClose: () => void;
  /** 撤销了一条：需求池重新取 */
  onRestored: (title: string) => void;
}

/** 「已丢掉」：30 天内丢掉的候选，能撤销回待认领（R01-9）。 */
export function DroppedCandidatesDialog({ apiClient, onClose, onRestored }: DroppedCandidatesDialogProps) {
  const dialogRef = useRef<HTMLDivElement>(null);
  useDialogFocus(dialogRef);
  const [payload, setPayload] = useState<DroppedCandidates | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [error, setError] = useState("");

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

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !event.isComposing && !busyId) onClose();
    };
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [busyId, onClose]);

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

  return (
    <div className="pool-dialog__overlay">
      <div aria-label="已丢掉的候选" aria-modal="true" className="pool-dialog" ref={dialogRef} role="dialog">
        <header className="pool-dialog__head">
          <div>
            <h2>已丢掉</h2>
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
            {payload.items.map((item) => (
              <li key={item.id}>
                <span className="dropped-list__body">
                  <span className="dropped-list__title">{item.title}</span>
                  <span className="dropped-list__meta">
                    {item.project_name ?? "未归项目"} · {formatMonthDay(item.dropped_at)} 丢掉 · {formatMonthDay(item.restore_until)}
                    {" "}前可撤销
                  </span>
                </span>
                <button
                  className="dropped-list__restore"
                  disabled={busyId !== null}
                  onClick={() => void restore(item.id, item.title)}
                  type="button"
                >
                  {busyId === item.id ? "撤销中…" : "撤销"}
                </button>
              </li>
            ))}
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
