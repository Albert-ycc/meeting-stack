import { useState } from "react";

import { isOldBackend, type ApiClient, type LinksState } from "../../api";
import { useLinksFlags } from "./LinksFlagsContext";
import { OLD_BACKEND_TEXT } from "./useRelationAnswer";
import "./links.css";

interface LinksStateLineProps {
  state: LinksState | null | undefined;
  apiClient?: Partial<Pick<ApiClient, "retryLinks">>;
  /** retry 以外的按钮（［挂上文件夹］［去项目页］）由宿主处理；没给时不画按钮 */
  onAction?: (action: NonNullable<LinksState["action"]>) => void;
  /** ［现在重试］放回排队以后，宿主重取 */
  onRetried?: () => void | Promise<void>;
  className?: string;
}

/**
 * 第四期的一句话状态：好了、在等什么、为什么停了，最多一个按钮。
 * 句子由后端生成；不用红色，不用 role="alert"。旧后台（useLinksFlags 为 null）时不画。
 */
export function LinksStateLine({ state, apiClient, onAction, onRetried, className = "" }: LinksStateLineProps) {
  const flags = useLinksFlags();
  const [busy, setBusy] = useState(false);
  const [retried, setRetried] = useState(false);
  const [failure, setFailure] = useState("");
  if (!flags || !state?.text) return null;
  const action = state.action;
  const retry = action?.kind === "retry" && typeof apiClient?.retryLinks === "function";
  const showButton = Boolean(action) && !retried && (retry || Boolean(onAction));

  const press = async () => {
    if (!action) return;
    if (!retry) {
      onAction?.(action);
      return;
    }
    setBusy(true);
    setFailure("");
    try {
      await apiClient?.retryLinks?.();
      setRetried(true);
      await onRetried?.();
    } catch (reason) {
      setFailure(isOldBackend(reason) ? OLD_BACKEND_TEXT : reason instanceof Error && reason.message ? reason.message : "没重试成，稍后再试");
    } finally {
      setBusy(false);
    }
  };

  return (
    <p className={`links-state links-state--${state.kind}${className ? ` ${className}` : ""}`}>
      <span>{failure || state.text}</span>
      {showButton && action && (
        <button className="text-button" disabled={busy} onClick={() => void press()} type="button">
          {action.label}
        </button>
      )}
    </p>
  );
}
