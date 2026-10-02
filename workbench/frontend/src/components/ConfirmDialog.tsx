import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";

import { ApiTimeoutError } from "../api";
import "./ConfirmDialog.css";
import { useBackdropDismiss, useDialogEscape, useDialogFocus } from "./useDialog";

export interface ConfirmOptions {
  title: string;
  message?: ReactNode;
  confirmLabel?: string;
  cancelLabel?: string;
  /** danger：删除、丢弃这类收不回来的操作，确认按钮用红色。 */
  tone?: "danger" | "default";
  /**
   * 给了 action 就在弹窗里执行：执行中按钮禁用，失败把原因写在弹窗里、不关闭（可以再点一次重试），
   * 成功才关闭并返回 true。没给就只问一句，确认即返回 true。
   * 请求超时（ApiTimeoutError）是个例外：服务端多半已经收到、可能还在处理，再执行一次会重复，
   * 这时不再给确认键，换成「关闭」（返回 false，提示里已经写了「稍后刷新确认」）。
   */
  action?: () => Promise<unknown>;
}

interface PendingConfirm extends ConfirmOptions {
  resolve: (confirmed: boolean) => void;
}

interface ConfirmDialogProps {
  options: ConfirmOptions;
  onDone: (confirmed: boolean) => void;
}

function ConfirmDialog({ options, onDone }: ConfirmDialogProps) {
  const { title, message, confirmLabel = "确定", cancelLabel = "取消", tone = "default", action } = options;
  const dialogRef = useRef<HTMLDivElement>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [timedOut, setTimedOut] = useState(false);
  const closeRef = useRef<HTMLButtonElement>(null);
  useDialogFocus(dialogRef);
  // 确认键换成「关闭」以后，焦点要跟过去：原来的确认键执行中是禁用的，焦点早已不在它身上
  useEffect(() => {
    if (timedOut) closeRef.current?.focus();
  }, [timedOut]);
  const cancel = () => {
    if (!busy) onDone(false);
  };
  useDialogEscape(dialogRef, cancel);
  const backdrop = useBackdropDismiss(cancel, busy);

  const confirm = async () => {
    if (!action) {
      onDone(true);
      return;
    }
    setBusy(true);
    setError("");
    try {
      await action();
      onDone(true);
    } catch (reason) {
      if (reason instanceof ApiTimeoutError) setTimedOut(true);
      setError(reason instanceof Error ? reason.message : "操作失败，请稍后重试");
      setBusy(false);
    }
  };

  return (
    <div className="confirm-modal__overlay" {...backdrop}>
      <div
        aria-describedby={message ? "confirm-modal-body" : undefined}
        aria-labelledby="confirm-modal-title"
        aria-modal="true"
        className="confirm-modal__card"
        ref={dialogRef}
        role="alertdialog"
      >
        <header className="confirm-modal__head">
          <h2 id="confirm-modal-title">{title}</h2>
          <button aria-label="关闭" disabled={busy} onClick={cancel} type="button">
            ✕
          </button>
        </header>
        {(message || error) && (
          <div className="confirm-modal__body" id="confirm-modal-body">
            {typeof message === "string" ? <p>{message}</p> : message}
            {error && (
              <p className="confirm-modal__error" role="alert">
                {error}
              </p>
            )}
          </div>
        )}
        <footer className="confirm-modal__footer">
          {timedOut ? (
            <button onClick={cancel} ref={closeRef} type="button">
              关闭
            </button>
          ) : (
            <>
              {/* 删除、丢弃这类收不回来的操作默认落在「取消」上：弹出后手一滑按回车不会就丢了；Tab 到确认键再回车照常能用 */}
              <button data-autofocus={tone === "danger" ? true : undefined} disabled={busy} onClick={cancel} type="button">
                {cancelLabel}
              </button>
              <button
                className={tone === "danger" ? "confirm-modal__danger" : "confirm-modal__primary"}
                data-autofocus={tone === "danger" ? undefined : true}
                disabled={busy}
                onClick={() => void confirm()}
                type="button"
              >
                {busy ? "处理中…" : confirmLabel}
              </button>
            </>
          )}
        </footer>
      </div>
    </div>
  );
}

/**
 * 页面里的二次确认。用法：
 *   const [confirm, confirmDialog] = useConfirm();
 *   if (!(await confirm({ title: "丢弃草稿？", tone: "danger" }))) return;
 *   …把 {confirmDialog} 渲染在组件里任意位置。
 */
export function useConfirm(): [(options: ConfirmOptions) => Promise<boolean>, ReactNode] {
  const [pending, setPending] = useState<PendingConfirm | null>(null);
  const confirm = useCallback(
    (options: ConfirmOptions) =>
      new Promise<boolean>((resolve) => {
        setPending({ ...options, resolve });
      }),
    [],
  );
  const dialog = pending ? (
    <ConfirmDialog
      onDone={(confirmed) => {
        pending.resolve(confirmed);
        setPending(null);
      }}
      options={pending}
    />
  ) : null;
  return [confirm, dialog];
}
