import { useEffect, useRef, useState, type KeyboardEvent } from "react";

import type { ApiClient } from "../../api";
import type { LinkOption, RequirementOptionsPayload } from "../../types";
import { PriorityBadge } from "../RequirementBadges";
import "./LinkPicker.css";

const SEARCH_DELAY_MS = 200;

const REASON_LABEL: Record<NonNullable<LinkOption["reason"]>, string> = {
  current: "现在挂着",
  paired: "AI 推荐",
  linked: "已关联这场会",
  same_meeting: "同场会",
};

interface LinkPickerProps {
  apiClient: ApiClient;
  taskId: string;
  /** 范围标题里的项目名；任务没归项目时写「同一场会」 */
  projectName?: string | null;
  /** 当前选中的（高亮）；null＝不挂 */
  selectedId?: string | null;
  /** 选了一条需求或候选；null＝不挂需求 */
  onPick: (option: LinkOption | null) => void;
  onClose: () => void;
  /** 推荐项拿到以后告诉外面（审核卡用它当默认选中） */
  onLoaded?: (payload: RequirementOptionsPayload) => void;
}

function FlagIcon() {
  return (
    <svg aria-hidden="true" className="link-picker__flag" fill="none" height="12" stroke="currentColor" strokeLinecap="round" strokeWidth="1.5" viewBox="0 0 16 16" width="12">
      <path d="M3.5 14V2.5" />
      <path d="M3.5 2.5h8l-1.6 3 1.6 3h-8" />
    </svg>
  );
}

/**
 * 挂到需求的选择器（R06-9、R07-8、R07-14）：推荐项在前（现在挂着的 → AI 配好的候选 → 已关联这场会的需求 → 同场会的候选），
 * 其下是任务范围里的进行中需求（待确认的任务还有待认领候选），能搜索，底部是「不挂需求」。
 * 范围由后端定：任务所属项目；没归项目时只限同一场会的候选和已关联这场会的需求。
 */
export function LinkPicker({
  apiClient,
  taskId,
  projectName,
  selectedId = null,
  onPick,
  onClose,
  onLoaded,
}: LinkPickerProps) {
  const [query, setQuery] = useState("");
  const [payload, setPayload] = useState<RequirementOptionsPayload | null>(null);
  const [failed, setFailed] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const requestRef = useRef(0);
  const onLoadedRef = useRef(onLoaded);
  onLoadedRef.current = onLoaded;

  useEffect(() => {
    const request = ++requestRef.current;
    const timer = window.setTimeout(
      () => {
        apiClient
          .taskRequirementOptions(taskId, query.trim() || undefined)
          .then((next) => {
            if (request !== requestRef.current) return;
            setPayload(next);
            setFailed(false);
            if (!query.trim()) onLoadedRef.current?.(next);
          })
          .catch(() => {
            if (request === requestRef.current) setFailed(true);
          });
      },
      query ? SEARCH_DELAY_MS : 0,
    );
    return () => window.clearTimeout(timer);
  }, [apiClient, query, taskId]);

  useEffect(() => {
    inputRef.current?.focus();
    const onPointerDown = (event: MouseEvent) => {
      if (rootRef.current && !rootRef.current.contains(event.target as Node)) onClose();
    };
    document.addEventListener("mousedown", onPointerDown);
    return () => document.removeEventListener("mousedown", onPointerDown);
  }, [onClose]);

  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.key === "Escape") {
      event.stopPropagation();
      onClose();
    }
  };

  const searching = query.trim() !== "";
  const recommended = searching ? [] : payload?.recommended ?? [];
  const recommendedIds = new Set(recommended.map((option) => option.id));
  const rest = (payload?.options ?? []).filter((option) => !recommendedIds.has(option.id));
  const scopeTitle = `${projectName || "同一场会"} · ${
    payload?.can_link_candidates ? "进行中的需求和待认领候选" : "进行中的需求"
  }`;

  const row = (option: LinkOption) => (
    <button
      aria-pressed={option.id === selectedId}
      className={`link-picker__option ${option.id === selectedId ? "is-selected" : ""}`}
      key={option.id}
      onClick={() => onPick(option)}
      role="option"
      type="button"
    >
      {option.kind === "requirement" ? <FlagIcon /> : <span className="link-picker__candidate">候选</span>}
      <span className="link-picker__title">{option.title}</span>
      {option.reason && <span className="link-picker__reason">{REASON_LABEL[option.reason]}</span>}
      {option.kind === "requirement" && <PriorityBadge priority={option.priority} />}
    </button>
  );

  return (
    <div aria-label="挂到需求" className="link-picker" onKeyDown={onKeyDown} ref={rootRef} role="dialog">
      <input
        aria-label="搜索需求"
        className="link-picker__search"
        onChange={(event) => setQuery(event.target.value)}
        placeholder="搜索需求"
        ref={inputRef}
        value={query}
      />
      <div className="link-picker__list" role="listbox">
        {failed ? (
          <div className="link-picker__empty">没取到可挂的需求，稍后再试</div>
        ) : payload === null ? (
          <div className="link-picker__empty">加载中…</div>
        ) : (
          <>
            {recommended.length > 0 && (
              <>
                <div className="link-picker__heading">推荐</div>
                {recommended.map(row)}
              </>
            )}
            <div className="link-picker__heading">{scopeTitle}</div>
            {rest.length > 0 ? (
              rest.map(row)
            ) : (
              <div className="link-picker__empty">
                {searching ? "没有匹配的需求" : recommended.length > 0 ? "没有别的可挂" : "这里还没有可挂的需求"}
              </div>
            )}
          </>
        )}
      </div>
      <button
        aria-pressed={selectedId === null}
        className={`link-picker__none ${selectedId === null ? "is-selected" : ""}`}
        onClick={() => onPick(null)}
        type="button"
      >
        不挂需求
      </button>
    </div>
  );
}
