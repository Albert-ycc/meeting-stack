import { useEffect, useRef, useState } from "react";

type CopyStatus = "copied" | "failed" | null;

interface CopyFolderPathButtonProps {
  className?: string;
  describedById?: string;
  path?: string | null;
  withLabel?: boolean;
}

function CopyPathIcon({ status }: { status: CopyStatus }) {
  return (
    <svg aria-hidden="true" viewBox="0 0 20 20">
      {status === "copied" ? (
        <path d="m5.2 10.1 3.1 3.1 6.5-6.7" />
      ) : status === "failed" ? (
        <>
          <path d="m6.5 6.5 7 7" />
          <path d="m13.5 6.5-7 7" />
        </>
      ) : (
        <>
          <rect height="9" rx="1.5" width="9" x="7.5" y="7.5" />
          <path d="M12.5 7.5V5A1.5 1.5 0 0 0 11 3.5H5A1.5 1.5 0 0 0 3.5 5v6A1.5 1.5 0 0 0 5 12.5h2.5" />
        </>
      )}
    </svg>
  );
}

export function CopyFolderPathButton({
  className = "",
  describedById,
  path,
  withLabel = false,
}: CopyFolderPathButtonProps) {
  const [status, setStatus] = useState<CopyStatus>(null);
  const resetTimer = useRef<number | null>(null);
  useEffect(
    () => () => {
      if (resetTimer.current !== null) window.clearTimeout(resetTimer.current);
    },
    [],
  );

  const show = (next: Exclude<CopyStatus, null>) => {
    if (resetTimer.current !== null) window.clearTimeout(resetTimer.current);
    setStatus(next);
    resetTimer.current = window.setTimeout(
      () => setStatus(null),
      next === "copied" ? 1600 : 3000,
    );
  };

  const copy = async () => {
    if (!path) return;
    try {
      if (!navigator.clipboard) throw new Error("clipboard unavailable");
      await navigator.clipboard.writeText(path);
      show("copied");
    } catch {
      show("failed");
    }
  };

  const label = !path
    ? "暂无文件夹路径"
    : status === "copied"
      ? "文件夹路径已复制"
      : status === "failed"
        ? "复制文件夹路径失败"
        : "复制文件夹路径";

  return (
    <button
      aria-describedby={describedById}
      aria-label={label}
      className={`archive-path-copy ${withLabel ? "archive-path-copy--labelled" : ""} ${status ? `is-${status}` : ""} ${className}`.trim()}
      data-copy-path={path || undefined}
      data-tooltip={
        status === "copied"
          ? "已复制"
          : status === "failed"
            ? "复制失败，点击重试"
            : "复制文件夹路径"
      }
      disabled={!path}
      onClick={() => void copy()}
      type="button"
    >
      <CopyPathIcon status={status} />
      {withLabel && (
        <span>
          {status === "copied" ? "已复制路径" : status === "failed" ? "复制失败" : "复制文件夹路径"}
        </span>
      )}
    </button>
  );
}
