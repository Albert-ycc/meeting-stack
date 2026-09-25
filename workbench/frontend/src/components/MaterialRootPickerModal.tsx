import { useEffect, useRef, useState } from "react";

import type { ApiClient } from "../api";
import type { MaterialBrowsePayload } from "../types";
import { FolderIcon } from "./FolderIcon";
import { useDialogFocus } from "./useDialog";
import "./MaterialRootPickerModal.css";

export interface MaterialRootPickerModalProps {
  apiClient: ApiClient;
  onClose: () => void;
  onConfirm: (path: string) => void;
  /** 上层用选中路径挂根目录/文件夹失败时的原因（D27：接口报错要就地显示，不能吞掉），弹窗留在原地不关 */
  error?: string;
  /** 上层正在处理「确定」的结果（挂根目录的请求还没回来），确定按钮要禁用避免重复提交 */
  busy?: boolean;
}

type LoadState = "loading" | "ready" | "error";

/**
 * 浏览外置盘选一个文件夹（挂材料根目录 / 需求挂材料文件夹的公共取径器）。
 * 只能在 MEETING_WORKBENCH_MATERIAL_BROWSE_ROOT 之下浏览；点行选中，点行尾「›」才进入下一级。
 */
export function MaterialRootPickerModal({
  apiClient,
  onClose,
  onConfirm,
  error,
  busy = false,
}: MaterialRootPickerModalProps) {
  const [payload, setPayload] = useState<MaterialBrowsePayload | null>(null);
  const [state, setState] = useState<LoadState>("loading");
  const [selected, setSelected] = useState<string | null>(null);
  const cardRef = useRef<HTMLDivElement>(null);
  useDialogFocus(cardRef);

  const load = async (path?: string) => {
    setState("loading");
    setSelected(null);
    try {
      const result = await apiClient.browseMaterials(path);
      setPayload(result);
      setState("ready");
    } catch {
      setState("error");
    }
  };

  useEffect(() => {
    void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    const onPointerDown = (event: MouseEvent) => {
      if (cardRef.current && !cardRef.current.contains(event.target as Node)) onClose();
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !event.isComposing) onClose();
    };
    document.addEventListener("mousedown", onPointerDown);
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("mousedown", onPointerDown);
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [onClose]);

  return (
    <div className="material-picker__overlay">
      <div
        aria-label="添加材料根目录"
        aria-modal="true"
        className="material-picker__card"
        ref={cardRef}
        role="dialog"
      >
        <header className="material-picker__head">
          <h2>添加材料根目录</h2>
          <button aria-label="关闭" className="material-picker__close" onClick={onClose} type="button">
            ✕
          </button>
        </header>

        <div className="material-picker__nav">
          <span className="material-picker__breadcrumbs">
            {payload ? payload.breadcrumbs.map((entry) => entry.name).join(" / ") : ""}
          </span>
          {payload && payload.parent !== null && (
            <button
              className="material-picker__up"
              onClick={() => void load(payload.parent ?? undefined)}
              type="button"
            >
              ‹ 返回上一级
            </button>
          )}
        </div>

        <div className="material-picker__list">
          {state === "loading" && <p className="material-picker__hint">正在读取目录…</p>}
          {state === "error" && <p className="material-picker__hint material-picker__hint--error">目录读取失败</p>}
          {state === "ready" && payload && payload.dirs.length === 0 && (
            <p className="material-picker__hint">这里没有子文件夹</p>
          )}
          {state === "ready" &&
            payload &&
            payload.dirs.map((entry) => (
              <div
                aria-selected={selected === entry.path}
                className={`material-picker__row ${selected === entry.path ? "is-selected" : ""}`}
                key={entry.path}
                onClick={() => setSelected(entry.path)}
                role="option"
              >
                <span className="material-picker__row-name">
                  <FolderIcon className="material-picker__folder" />
                  {entry.name}
                </span>
                <span className="material-picker__row-actions">
                  {selected === entry.path && (
                    <span aria-hidden="true" className="material-picker__check">
                      ✓
                    </span>
                  )}
                  <button
                    aria-label={`进入 ${entry.name}`}
                    className="material-picker__enter"
                    onClick={(event) => {
                      event.stopPropagation();
                      void load(entry.path);
                    }}
                    type="button"
                  >
                    ›
                  </button>
                </span>
              </div>
            ))}
        </div>

        {error && (
          <p className="material-picker__error" role="alert">
            {error}
          </p>
        )}

        <footer className="material-picker__foot">
          <span className="material-picker__selected-path">{selected ?? ""}</span>
          <span className="material-picker__foot-actions">
            <button className="material-picker__cancel" onClick={onClose} type="button">
              取消
            </button>
            <button
              className="material-picker__confirm"
              disabled={!selected || busy}
              onClick={() => selected && onConfirm(selected)}
              type="button"
            >
              {busy ? "处理中…" : "确定"}
            </button>
          </span>
        </footer>
      </div>
    </div>
  );
}
