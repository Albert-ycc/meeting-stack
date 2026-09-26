import { useEffect, useRef, useState } from "react";

import type { ApiClient } from "../api";
import type { ColdStartFolderItem } from "../types";
import { FolderIcon } from "./FolderIcon";
import { NoticeBanner, useNotice } from "./Notice";
import "./FolderSuggestionBanner.css";

interface FolderSuggestionBannerProps {
  apiClient: ApiClient;
  /** 挂好文件夹后刷新项目列表 */
  onProjectsChanged?: () => void | Promise<void>;
  /** 读完之后告诉工作台这条横幅在不在：一次性横幅同一时间只出一条，它排在最前 */
  onActiveChange?: (active: boolean) => void;
}

/**
 * 工作台一次性横幅：还没挂文件夹的项目找到了同名文件夹，问一次要不要挂上。
 * 同名的默认勾选，名字相近的默认不勾；没勾的记为「不挂」，以后不再问；［稍后］几天内不再出现。
 */
export function FolderSuggestionBanner({ apiClient, onProjectsChanged, onActiveChange }: FolderSuggestionBannerProps) {
  const [items, setItems] = useState<ColdStartFolderItem[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [open, setOpen] = useState(false);
  const [checked, setChecked] = useState<Set<string>>(new Set());
  const [busy, setBusy] = useState(false);
  const [errors, setErrors] = useState<Record<string, string>>({});
  const { notice, setNotice, dismissNotice } = useNotice();
  const cardRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (typeof apiClient.coldStartFolders !== "function") {
      setLoaded(true);
      return;
    }
    let alive = true;
    apiClient
      .coldStartFolders()
      .then((payload) => {
        if (!alive) return;
        setItems(payload.items);
        setChecked(new Set(payload.items.filter((item) => item.match === "exact").map((item) => item.project_id)));
      })
      .catch(() => {
        // 只是个提醒，读不到就不出现
      })
      .finally(() => {
        if (alive) setLoaded(true);
      });
    return () => {
      alive = false;
    };
  }, [apiClient]);

  const active = items.length > 0;
  useEffect(() => {
    if (loaded) onActiveChange?.(active);
  }, [active, loaded, onActiveChange]);

  useEffect(() => {
    if (!open) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !busy) setOpen(false);
    };
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [open, busy]);

  if (items.length === 0) {
    return <NoticeBanner notice={notice} onDismiss={dismissNotice} />;
  }

  const toggle = (projectId: string) =>
    setChecked((current) => {
      const next = new Set(current);
      if (next.has(projectId)) next.delete(projectId);
      else next.add(projectId);
      return next;
    });

  const later = async () => {
    setItems([]);
    try {
      await apiClient.snoozeFolderSuggestions();
    } catch {
      // 稍后只影响这次提醒，失败了下次再问也无妨
    }
  };

  const apply = async () => {
    setBusy(true);
    setErrors({});
    const failed: Record<string, string> = {};
    let mounted = 0;
    for (const item of items.filter((entry) => checked.has(entry.project_id))) {
      try {
        await apiClient.addProjectMaterialRoot(item.project_id, item.path);
        mounted += 1;
      } catch (error) {
        failed[item.project_id] = error instanceof Error ? error.message : "没挂上，请稍后重试";
      }
    }
    const declined = items.filter((entry) => !checked.has(entry.project_id)).map((entry) => entry.project_id);
    if (declined.length) {
      try {
        await apiClient.declineFolderSuggestions(declined);
      } catch {
        // 没记上「不挂」，下次还会再问一次，不影响已经挂上的
      }
    }
    setBusy(false);
    if (mounted) await onProjectsChanged?.();
    if (Object.keys(failed).length) {
      // 没挂上的留在弹窗里，就地说原因
      setErrors(failed);
      setItems((current) => current.filter((entry) => failed[entry.project_id]));
      return;
    }
    setOpen(false);
    setItems([]);
    setNotice(mounted ? `已挂上 ${mounted} 个文件夹` : "好的，这些项目先不挂文件夹");
  };

  const exactCount = items.filter((item) => item.match === "exact").length;
  const checkedCount = items.filter((item) => checked.has(item.project_id)).length;

  return (
    <>
      <div className="folder-suggestion" role="status">
        <FolderIcon className="folder-suggestion__icon" />
        <span>
          {exactCount === items.length
            ? `${items.length} 个项目找到了同名文件夹，要挂上吗？`
            : `${items.length} 个项目找到了同名或名字相近的文件夹，要挂上吗？`}
        </span>
        <button className="ghost-button" onClick={() => setOpen(true)} type="button">
          看看
        </button>
        <button className="text-button" onClick={() => void later()} type="button">
          稍后
        </button>
      </div>

      {open && (
        <div
          className="folder-suggestion__overlay"
          onMouseDown={(event) => {
            if (!busy && cardRef.current && !cardRef.current.contains(event.target as Node)) setOpen(false);
          }}
        >
          <div aria-label="挂上同名文件夹" aria-modal="true" className="folder-suggestion__card" ref={cardRef} role="dialog">
            <header className="folder-suggestion__head">
              <h2>挂上同名文件夹</h2>
              <p>勾上的会挂成项目的材料根目录；没勾的以后不再问，想挂随时在项目页添加。</p>
            </header>
            <ul className="folder-suggestion__list">
              {items.map((item) => (
                <li key={item.project_id}>
                  <label className="folder-suggestion__row">
                    <input
                      checked={checked.has(item.project_id)}
                      disabled={busy}
                      onChange={() => toggle(item.project_id)}
                      type="checkbox"
                    />
                    <span className="folder-suggestion__text">
                      <strong>{item.project_name}</strong>
                      <span className="folder-suggestion__path">
                        <FolderIcon className="folder-suggestion__icon" />
                        <span className="folder-suggestion__path-text">{item.path}</span>
                        {item.match === "similar" && <em>名字相近</em>}
                      </span>
                      {errors[item.project_id] && (
                        <span className="folder-suggestion__error" role="alert">
                          {errors[item.project_id]}
                        </span>
                      )}
                    </span>
                  </label>
                </li>
              ))}
            </ul>
            <footer className="folder-suggestion__footer">
              <button className="ghost-button" disabled={busy} onClick={() => setOpen(false)} type="button">
                取消
              </button>
              <button className="folder-suggestion__submit" disabled={busy} onClick={() => void apply()} type="button">
                {busy ? "挂载中…" : checkedCount ? `挂上 ${checkedCount} 个` : "都不挂"}
              </button>
            </footer>
          </div>
        </div>
      )}
    </>
  );
}
