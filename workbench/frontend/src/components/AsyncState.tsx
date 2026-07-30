import type { LoadState } from "../types";

interface AsyncStateProps {
  state: Extract<LoadState, "loading" | "error" | "empty">;
  message?: string;
}

export function AsyncState({ state, message }: AsyncStateProps) {
  if (state === "loading") {
    return (
      <div className="async-state" aria-live="polite">
        <span className="signal-loader" aria-hidden="true" />
        <p>正在读取本地档案…</p>
      </div>
    );
  }
  if (state === "error") {
    return (
      <div className="async-state async-state--error" role="alert">
        <span className="state-mark">!</span>
        <p>{message || "读取失败，请稍后重试"}</p>
      </div>
    );
  }
  return (
    <div className="async-state">
      <span className="state-mark">○</span>
      <p>{message || "这里还没有可显示的记录"}</p>
    </div>
  );
}
