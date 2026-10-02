import { useEffect, useRef, useState } from "react";

import { ApiError } from "../api";
import type { ApiClient } from "../api";
import type { MaterialBrowsePayload } from "../types";
import { FolderIcon } from "./FolderIcon";
import { useBackdropDismiss, useDialogFocus } from "./useDialog";
import "./MaterialRootPickerModal.css";

export interface MaterialRootPickerModalProps {
  apiClient: ApiClient;
  onClose: () => void;
  onConfirm: (path: string) => void;
  /** 上层用选中路径挂根目录/文件夹失败时的原因（D27：接口报错要就地显示，不能吞掉），弹窗留在原地不关 */
  error?: string;
  /** 上层正在处理「确定」的结果（挂根目录的请求还没回来），确定按钮要禁用避免重复提交 */
  busy?: boolean;
  /** 标题，默认「添加材料根目录」；设项目总文件夹时是「选项目总文件夹」 */
  title?: string;
  /** 标题下面的一句说明 */
  description?: string;
  /** 确认按钮的文字，默认「确定」 */
  confirmLabel?: string;
  /** 底部额外的文字选项，比如「不建了，以后自己挂文件夹」；点了由上层处理（通常是关掉取径器） */
  extraOption?: { label: string; onSelect: () => void };
}

type LoadState = "loading" | "ready" | "error";

/**
 * 浏览外置盘选一个文件夹（挂材料根目录 / 需求挂材料文件夹 / 项目总文件夹的公共取径器）。
 * 只能在 MEETING_WORKBENCH_MATERIAL_BROWSE_ROOT 之下浏览；点行选中，点行尾「›」才进入下一级，
 * 也可以直接选当前打开的这个文件夹。
 */
export function MaterialRootPickerModal({
  apiClient,
  onClose,
  onConfirm,
  error,
  busy = false,
  title = "添加材料根目录",
  description,
  confirmLabel = "确定",
  extraOption,
}: MaterialRootPickerModalProps) {
  const [payload, setPayload] = useState<MaterialBrowsePayload | null>(null);
  const [state, setState] = useState<LoadState>("loading");
  // 读目录失败的原因：服务端给了就照原文显示（如「资料盘未连接，插上后再选」）
  const [loadError, setLoadError] = useState("");
  const [selected, setSelected] = useState<string | null>(null);
  const cardRef = useRef<HTMLDivElement>(null);
  useDialogFocus(cardRef);
  const close = () => {
    if (!busy) onClose();
  };
  // 只做选择的弹窗：点背景关
  const backdrop = useBackdropDismiss(close, busy);

  // 进下一级还没回来就点了返回上一级：只认最后一次点的，慢回来的旧请求丢掉
  const loadSeqRef = useRef(0);
  const load = async (path?: string) => {
    const seq = ++loadSeqRef.current;
    setState("loading");
    setSelected(null);
    try {
      const result = await apiClient.browseMaterials(path);
      if (seq !== loadSeqRef.current) return;
      setPayload(result);
      setState("ready");
    } catch (reason) {
      if (seq !== loadSeqRef.current) return;
      setLoadError(reason instanceof ApiError ? reason.message : "目录读取失败");
      setState("error");
    }
  };

  useEffect(() => {
    void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // 当前打开的这个文件夹本身也能选（比如项目总文件夹就是「项目」这一层）
  const current = state === "ready" && payload ? payload.path : null;

  return (
    <div className="material-picker__overlay" {...backdrop}>
      <div
        aria-label={title}
        aria-modal="true"
        className="material-picker__card"
        // 点到行这类不能聚焦的地方时焦点落在弹窗上，Esc 照样能关
        tabIndex={-1}
        onKeyDown={(event) => {
          // 自己处理 Esc，别再冒泡到外层表单弹窗把它也关了
          if (event.key === "Escape" && !event.nativeEvent.isComposing) {
            event.stopPropagation();
            close();
          }
        }}
        ref={cardRef}
        role="dialog"
      >
        <header className="material-picker__head">
          <h2>{title}</h2>
          <button aria-label="关闭" className="material-picker__close" disabled={busy} onClick={close} type="button">
            ✕
          </button>
        </header>
        {description && <p className="material-picker__desc">{description}</p>}

        <div className="material-picker__nav">
          <span className="material-picker__breadcrumbs">
            {payload ? payload.breadcrumbs.map((entry) => entry.name).join(" / ") : ""}
          </span>
          <span className="material-picker__nav-actions">
            {current && (
              <button
                aria-pressed={selected === current}
                className={`material-picker__current ${selected === current ? "is-selected" : ""}`}
                onClick={() => setSelected(current)}
                title={current}
                type="button"
              >
                选当前文件夹
              </button>
            )}
            {payload && payload.parent !== null && (
              <button
                className="material-picker__up"
                onClick={() => void load(payload.parent ?? undefined)}
                type="button"
              >
                ‹ 返回上一级
              </button>
            )}
          </span>
        </div>

        <div className="material-picker__list">
          {state === "loading" && <p className="material-picker__hint">正在读取目录…</p>}
          {state === "error" && <p className="material-picker__hint material-picker__hint--error">{loadError}</p>}
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

        {extraOption && (
          <div className="material-picker__extra">
            <button className="text-button" disabled={busy} onClick={extraOption.onSelect} type="button">
              {extraOption.label}
            </button>
          </div>
        )}

        <footer className="material-picker__foot">
          <span className="material-picker__selected-path">{selected ?? ""}</span>
          <span className="material-picker__foot-actions">
            <button className="material-picker__cancel" disabled={busy} onClick={close} type="button">
              取消
            </button>
            <button
              className="material-picker__confirm"
              disabled={!selected || busy}
              onClick={() => selected && onConfirm(selected)}
              type="button"
            >
              {busy ? "处理中…" : confirmLabel}
            </button>
          </span>
        </footer>
      </div>
    </div>
  );
}
