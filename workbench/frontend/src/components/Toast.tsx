import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { UNDO_NOTICE_MS } from "./Notice";
import "./Toast.css";

export type ToastTone = "error";

export interface ToastOptions {
  onUndo?: () => void | Promise<void>;
  durationMs?: number;
  /** 操作没成：红色叹号、读屏立即播报（role=alert），默认停 10 秒（失败原因要看清） */
  tone?: ToastTone;
}

interface ToastState {
  message: string;
  onUndo?: () => void | Promise<void>;
  tone?: ToastTone;
  /** 每次 showToast 递增：同一句话连着出现两次也重新播报 */
  key: number;
}

/**
 * 屏幕底部居中的深色条（「已复制路径」「需求已创建」这类操作结果），到时自动消失。
 * 页面里 `const { toastNode, showToast } = useToast();`，把 toastNode 渲染在页面根节点里即可。
 *
 * 可撤销的动作传 `onUndo`：条的右边多一个［撤销］，停 10 秒（和提示条 NoticeBanner 的撤销同一个时长）。
 * 点［撤销］先收起这条提示、再调 onUndo，撤销成没成由调用方在 onUndo 里另行提示——所以收起必须在前，
 * 免得晚到的「收起」把调用方刚弹出的「已撤销」也抹掉。
 *
 * 操作失败传 `tone: "error"`（项目页与待办改版加的，只做加法）：叹号换掉勾，停 10 秒。
 */
export function useToast(durationMs = 2400): {
  toastNode: ReactNode;
  showToast: (message: string, options?: ToastOptions) => void;
  /** 立刻收起（比如切了页签，上一页的结果不该带过来） */
  hideToast: () => void;
} {
  const [toast, setToast] = useState<ToastState | null>(null);
  const timer = useRef<number | null>(null);
  const sequence = useRef(0);

  const clearTimer = useCallback(() => {
    if (timer.current !== null) window.clearTimeout(timer.current);
    timer.current = null;
  }, []);

  useEffect(() => clearTimer, [clearTimer]);

  const showToast = useCallback(
    (message: string, options?: ToastOptions) => {
      clearTimer();
      sequence.current += 1;
      setToast({ message, onUndo: options?.onUndo, tone: options?.tone, key: sequence.current });
      const stay =
        options?.durationMs ?? (options?.onUndo || options?.tone === "error" ? UNDO_NOTICE_MS : durationMs);
      timer.current = window.setTimeout(() => setToast(null), stay);
    },
    [clearTimer, durationMs],
  );

  const hideToast = useCallback(() => {
    clearTimer();
    setToast(null);
  }, [clearTimer]);

  const undo = () => {
    const onUndo = toast?.onUndo;
    if (!onUndo) return;
    clearTimer();
    setToast(null);
    void onUndo();
  };

  const error = toast?.tone === "error";
  const toastNode = toast ? (
    <div
      aria-live={error ? "assertive" : "polite"}
      className={`app-toast${error ? " app-toast--error" : ""}`}
      key={toast.key}
      role={error ? "alert" : "status"}
    >
      {error ? (
        <svg aria-hidden="true" className="app-toast__mark" fill="none" height="14" viewBox="0 0 16 16" width="14">
          <circle cx="8" cy="8" fill="currentColor" r="7" />
          <path d="M8 4.4v4.4" stroke="#1d1d1f" strokeLinecap="round" strokeWidth="1.8" />
          <circle cx="8" cy="11.4" fill="#1d1d1f" r="1" />
        </svg>
      ) : (
      <svg
        aria-hidden="true"
        className="app-toast__check"
        fill="none"
        height="14"
        stroke="currentColor"
        strokeLinecap="round"
        strokeLinejoin="round"
        strokeWidth="2"
        viewBox="0 0 16 16"
        width="14"
      >
        <path d="M3.2 8.6l3 3 6.6-7.2" />
      </svg>
      )}
      <span className="app-toast__text">{toast.message}</span>
      {toast.onUndo && (
        <button className="app-toast__undo" onClick={undo} type="button">
          撤销
        </button>
      )}
    </div>
  ) : null;

  return { toastNode, showToast, hideToast };
}
