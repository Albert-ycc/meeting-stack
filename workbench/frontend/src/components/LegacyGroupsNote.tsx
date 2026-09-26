import { useEffect, useState } from "react";

import type { ApiClient } from "../api";
import type { LegacyGroupsSummary } from "../types";
import "./LegacyGroupsNote.css";

interface LegacyGroupsNoteProps {
  apiClient: ApiClient;
  canWrite: boolean;
  /** 撤销后词条的分组变了，要重读术语和分组 */
  onChanged: () => void | Promise<void>;
}

/** 词典页一行：「已自动整理 N 个旧分组［查看］［撤销］」。升级时没挂项目的旧分组被自动归到项目或公共。 */
export function LegacyGroupsNote({ apiClient, canWrite, onChanged }: LegacyGroupsNoteProps) {
  const [summary, setSummary] = useState<LegacyGroupsSummary | null>(null);
  const [expanded, setExpanded] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    if (typeof apiClient.legacyGroups !== "function") return;
    let alive = true;
    apiClient
      .legacyGroups()
      .then((payload) => {
        if (alive) setSummary(payload.summary);
      })
      .catch(() => {
        // 只是一行说明，读不到就不显示
      });
    return () => {
      alive = false;
    };
  }, [apiClient]);

  if (!summary) return null;

  const undo = async () => {
    setBusy(true);
    setError("");
    try {
      await apiClient.undoLegacyGroups();
      setSummary({ ...summary, undone: true });
      setExpanded(false);
      await onChanged();
    } catch (err) {
      setError(err instanceof Error ? err.message : "撤销失败，请稍后重试");
    } finally {
      setBusy(false);
    }
  };

  const dismiss = async () => {
    setSummary(null);
    try {
      await apiClient.dismissLegacyGroups();
    } catch {
      // 收起只影响这一行，失败了下次进来再收
    }
  };

  return (
    <div className="legacy-groups" role="status">
      <div className="legacy-groups__line">
        <span>
          {summary.undone
            ? "已撤销自动整理，旧分组恢复原样"
            : `升级时已自动整理 ${summary.groups.length} 个旧分组`}
        </span>
        {!summary.undone && (
          <button className="text-button" onClick={() => setExpanded((value) => !value)} type="button">
            {expanded ? "收起" : "查看"}
          </button>
        )}
        {canWrite && !summary.undone && (
          <button className="text-button" disabled={busy} onClick={() => void undo()} type="button">
            撤销
          </button>
        )}
        {canWrite && (
          <button className="text-button" disabled={busy} onClick={() => void dismiss()} type="button">
            知道了
          </button>
        )}
      </div>
      {expanded && (
        <ul className="legacy-groups__list">
          {summary.groups.map((group) => (
            <li key={group.scope}>
              「{group.scope}」{group.count} 条 → {group.project_name ? `项目「${group.project_name}」` : "公共"}
            </li>
          ))}
        </ul>
      )}
      {error && (
        <p className="legacy-groups__error" role="alert">
          {error}
        </p>
      )}
    </div>
  );
}
