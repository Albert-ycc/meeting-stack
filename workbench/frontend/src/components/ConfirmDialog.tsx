import { useCallback, useRef, useState, type ReactNode } from "react";

import "./ConfirmDialog.css";
import { useBackdropDismiss, useDialogFocus } from "./useDialog";

export interface ConfirmOptions {
  title: string;
  message?: ReactNode;
  confirmLabel?: string;
  cancelLabel?: string;
  /** danger：删除、丢弃这类收不回来的操作，确认按钮用红色。 */
  tone?: "danger" | "default";
  /**
   * 给了 action 就在弹窗里执行：执行中按钮禁用，失败把原因写在弹窗里、不关闭，
   * 成功才关闭并返回 true。没给就只问一句，确认即返回 true。
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
  useDialogFocus(dialogRef);
  const cancel = () => {
    if (!busy) onDone(false);
  };
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
        onKeyDown={(event) => {
          if (event.key === "Escape" && !event.nativeEvent.isComposing) {
            event.stopPropagation();
            cancel();
          }
        }}
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
          <button disabled={busy} onClick={cancel} type="button">
            {cancelLabel}
          </button>
          <button
            className={tone === "danger" ? "confirm-modal__danger" : "confirm-modal__primary"}
            data-autofocus
            disabled={busy}
            onClick={() => void confirm()}
            type="button"
          >
            {busy ? "处理中…" : confirmLabel}
          </button>
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
