import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import "./Toast.css";

/**
 * 顶栏下方居中的轻提示（「已复制路径」「需求已创建」这类操作结果），到时自动消失。
 * 页面里 `const { toastNode, showToast } = useToast();`，把 toastNode 渲染在页面根节点里即可。
 */
export function useToast(durationMs = 2400): {
  toastNode: ReactNode;
  showToast: (message: string) => void;
} {
  const [message, setMessage] = useState<string | null>(null);
  const timer = useRef<number | null>(null);

  useEffect(
    () => () => {
      if (timer.current !== null) window.clearTimeout(timer.current);
    },
    [],
  );

  const showToast = useCallback(
    (next: string) => {
      if (timer.current !== null) window.clearTimeout(timer.current);
      setMessage(next);
      timer.current = window.setTimeout(() => setMessage(null), durationMs);
    },
    [durationMs],
  );

  const toastNode = message ? (
    <div aria-live="polite" className="app-toast" role="status">
      <span aria-hidden="true" className="app-toast__dot" />
      {message}
    </div>
  ) : null;

  return { toastNode, showToast };
}
