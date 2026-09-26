import { useEffect, useMemo, useRef, useState } from "react";

import type { ApiClient } from "../api";
import { formatMonthDay } from "../format";
import type { MaterialFolderStat, ProjectSubfoldersRoot } from "../types";
import { useBackdropDismiss, useDialogFocus } from "./useDialog";
import { FolderIcon } from "./FolderIcon";
import "./MaterialFolderPickerModal.css";

interface MaterialFolderPickerModalProps {
  apiClient: ApiClient;
  projectId: string;
  projectName: string;
  /** 当前已选的文件夹（可能跨根目录累计），用于预勾选与「已选 N 个」计数 */
  selectedFolders: MaterialFolderStat[];
  onCancel: () => void;
  onConfirm: (folders: MaterialFolderStat[]) => void;
  /** A-02-5：项目没挂材料根目录时，引导去项目详情挂根目录 */
  onOpenProject?: (projectId: string) => void;
}

function folderCountLabel(folder: MaterialFolderStat): string {
  return folder.file_count_capped ? "2000+" : String(folder.file_count);
}

/** 选择材料文件夹（A-02-4 / A-02-5），叠在一级弹窗上。 */
export function MaterialFolderPickerModal({
  apiClient,
  projectId,
  projectName,
  selectedFolders,
  onCancel,
  onConfirm,
  onOpenProject,
}: MaterialFolderPickerModalProps) {
  const dialogRef = useRef<HTMLDivElement>(null);
  useDialogFocus(dialogRef);
  const backdrop = useBackdropDismiss(onCancel);
  const [roots, setRoots] = useState<ProjectSubfoldersRoot[] | null>(null);
  const [loadError, setLoadError] = useState(false);
  const [activeRootId, setActiveRootId] = useState<number | null>(null);
  const [search, setSearch] = useState("");
  const [draft, setDraft] = useState<Map<string, MaterialFolderStat>>(
    () => new Map(selectedFolders.map((folder) => [folder.path, folder])),
  );

  useEffect(() => {
    let active = true;
    void apiClient
      .projectMaterialSubfolders(projectId)
      .then((payload) => {
        if (!active) return;
        setRoots(payload.roots);
        setActiveRootId(payload.roots[0]?.root_id ?? null);
      })
      .catch(() => {
        if (active) setLoadError(true);
      });
    return () => {
      active = false;
    };
  }, [apiClient, projectId]);

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !event.isComposing) onCancel();
    };
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [onCancel]);

  const activeRoot = roots?.find((root) => root.root_id === activeRootId) ?? null;
  const visibleFolders = useMemo(() => {
    const list = activeRoot?.folders ?? [];
    const keyword = search.trim();
    return keyword ? list.filter((folder) => folder.name.includes(keyword)) : list;
  }, [activeRoot, search]);

  const toggleOne = (folder: MaterialFolderStat) =>
    setDraft((current) => {
      const next = new Map(current);
      if (next.has(folder.path)) next.delete(folder.path);
      else next.set(folder.path, folder);
      return next;
    });

  const allVisibleSelected = visibleFolders.length > 0 && visibleFolders.every((folder) => draft.has(folder.path));
  const someVisibleSelected = visibleFolders.some((folder) => draft.has(folder.path));

  const toggleAllVisible = () =>
    setDraft((current) => {
      const next = new Map(current);
      if (allVisibleSelected) {
        visibleFolders.forEach((folder) => next.delete(folder.path));
      } else {
        visibleFolders.forEach((folder) => next.set(folder.path, folder));
      }
      return next;
    });

  const goPickRoot = () => {
    onOpenProject?.(projectId);
    onCancel();
  };

  const loading = roots === null && !loadError;
  const noRoots = roots !== null && roots.length === 0;

  return (
    <div className="folder-picker__overlay" {...backdrop}>
      <div
        aria-label="选择材料文件夹"
        aria-modal="true"
        className="folder-picker__card"
        onClick={(event) => event.stopPropagation()}
        ref={dialogRef}
        role="dialog"
      >
        <header className="folder-picker__head">
          <div>
            <h2>选择材料文件夹</h2>
            {!noRoots && roots && roots.length === 1 && (
              <p className="folder-picker__root-line">{roots[0].root_path}</p>
            )}
          </div>
          <button aria-label="关闭" className="folder-picker__close" onClick={onCancel} type="button">✕</button>
        </header>

        {loading ? (
          <div className="folder-picker__state">加载中…</div>
        ) : loadError ? (
          <div className="folder-picker__state folder-picker__state--error" role="alert">材料文件夹读取失败</div>
        ) : noRoots ? (
          <div className="folder-picker__empty">
            <span aria-hidden="true" className="folder-picker__empty-icon">
              <FolderIcon />
            </span>
            <p>{projectName} 还没有材料根目录</p>
            <button className="folder-picker__go-root" onClick={goPickRoot} type="button">去挂根目录</button>
          </div>
        ) : (
          <>
            {roots && roots.length > 1 && (
              <div aria-label="切换根目录" className="folder-picker__root-tabs" role="tablist">
                {roots.map((root) => (
                  <button
                    aria-selected={root.root_id === activeRootId}
                    key={root.root_id}
                    onClick={() => setActiveRootId(root.root_id)}
                    role="tab"
                    title={root.root_path}
                    type="button"
                  >
                    {root.root_path.split("/").pop() || root.root_path}
                  </button>
                ))}
              </div>
            )}
            <input
              aria-label="搜索文件夹"
              className="folder-picker__search"
              onChange={(event) => setSearch(event.target.value)}
              placeholder="搜索文件夹"
              value={search}
            />
            <div className="folder-picker__table">
              <div className="folder-picker__row folder-picker__row--head">
                <input
                  aria-label="全选"
                  checked={allVisibleSelected}
                  onChange={toggleAllVisible}
                  ref={(el) => {
                    if (el) el.indeterminate = !allVisibleSelected && someVisibleSelected;
                  }}
                  type="checkbox"
                />
                <span>文件夹</span>
                <span>文件数</span>
                <span>修改日期</span>
              </div>
              <div className="folder-picker__body">
                {visibleFolders.length === 0 ? (
                  <div className="folder-picker__state">没有匹配的文件夹</div>
                ) : (
                  visibleFolders.map((folder) => (
                    <label className="folder-picker__row" key={folder.path}>
                      <input checked={draft.has(folder.path)} onChange={() => toggleOne(folder)} type="checkbox" />
                      <span className="folder-picker__name">{folder.name}</span>
                      <span>{folderCountLabel(folder)} 个文件</span>
                      <span>{formatMonthDay(folder.modified_at)}</span>
                    </label>
                  ))
                )}
              </div>
            </div>
          </>
        )}

        {!loading && !loadError && !noRoots && (
          <footer className="folder-picker__footer">
            <span className="folder-picker__count">已选 {draft.size} 个</span>
            <span className="folder-picker__actions">
              <button className="folder-picker__cancel" onClick={onCancel} type="button">取消</button>
              <button className="folder-picker__confirm" onClick={() => onConfirm([...draft.values()])} type="button">确定</button>
            </span>
          </footer>
        )}
        {(loading || loadError || noRoots) && (
          <footer className="folder-picker__footer folder-picker__footer--single">
            <button className="folder-picker__cancel" onClick={onCancel} type="button">取消</button>
          </footer>
        )}
      </div>
    </div>
  );
}
