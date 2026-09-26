import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";

/*
 * 页面操作结果的提示条。全站约定：
 * - success：操作成功，5 秒后自动收起；
 * - warning：成功了但有需要注意的后续（比如「请求发出后的本地修改仍保留，请再次保存」），不自动收起；
 * - error：失败原因，不自动收起，直到用户点 ✕ 或下一次操作换掉它。
 * 三种语气配色和图标都不同，失败用 role="alert" 让读屏立即播报。
 */
export type NoticeTone = "success" | "warning" | "error";

export interface NoticeState {
  message: string;
  tone: NoticeTone;
  /** 每次 setNotice 递增，同一句话连着出现两次也会重新计时。 */
  key: number;
  durationMs?: number;
}

export const NOTICE_AUTO_HIDE_MS = 5000;

export function useNotice() {
  const [state, setState] = useState<NoticeState | null>(null);
  const sequence = useRef(0);

  /** 空字符串等于清掉提示；durationMs 只对 success 生效，用来覆盖默认的 5 秒。 */
  const setNotice = useCallback((message: string, tone: NoticeTone = "success", durationMs?: number) => {
    if (!message) {
      setState(null);
      return;
    }
    sequence.current += 1;
    setState({ message, tone, key: sequence.current, durationMs });
  }, []);

  const dismissNotice = useCallback(() => setState(null), []);

  useEffect(() => {
    if (!state || state.tone !== "success") return;
    const key = state.key;
    const timer = window.setTimeout(
      () => setState((current) => (current && current.key === key ? null : current)),
      state.durationMs ?? NOTICE_AUTO_HIDE_MS,
    );
    return () => window.clearTimeout(timer);
  }, [state]);

  return { notice: state, setNotice, dismissNotice };
}

interface NoticeBannerProps {
  notice: NoticeState | null;
  onDismiss: () => void;
  className?: string;
  /** 附在提示后面的操作按钮，比如「撤销」。 */
  children?: ReactNode;
}

const TONE_ICON: Record<NoticeTone, string> = { success: "✓", warning: "!", error: "!" };

export function NoticeBanner({ notice, onDismiss, className = "", children }: NoticeBannerProps) {
  if (!notice) return null;
  return (
    <div
      className={`action-banner action-banner--${notice.tone}${className ? ` ${className}` : ""}`}
      key={notice.key}
      role={notice.tone === "error" ? "alert" : "status"}
    >
      <span aria-hidden="true" className="action-banner__icon">
        {TONE_ICON[notice.tone]}
      </span>
      <span className="action-banner__text">{notice.message}</span>
      {children}
      <button aria-label="关闭提示" className="action-banner__close" onClick={onDismiss} type="button">
        ✕
      </button>
    </div>
  );
}
