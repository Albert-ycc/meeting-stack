import { useEffect, useRef, useState } from "react";

import type { ApiClient } from "../../api";
import type { RequirementMergeResult, RequirementMergeTarget, RequirementMoving, RequirementPriority } from "../../types";
import { REQUIREMENT_STATUS_LABELS } from "../RequirementBadges";
import { useDialogEscape, useDialogFocus } from "../useDialog";
import "./PoolDialogs.css";

/** 并入成功的提示（D7） */
export const mergedIntoMessage = (title: string) => `已并入「${title}」`;

/** 合并后等级取两者较高的（P0 最高） */
export function higherPriority(left: RequirementPriority, right: RequirementPriority): RequirementPriority {
  return left < right ? left : right;
}

interface MergeRequirementDialogProps {
  apiClient: ApiClient;
  /** 被并掉的「这条」 */
  requirement: { id: string; title: string; project_name: string | null };
  onClose: () => void;
  onMerged: (result: RequirementMergeResult) => void;
}

/**
 * 把一条需求并入同项目里的另一条（D4）：列同项目里除自己外的需求，三种状态都列，可以按名称搜；
 * 选中以后底部预览带过去多少东西、合并后的等级。名字、说明、出处怎么取由后端定（D6），这里只管选哪条。
 */
export function MergeRequirementDialog({ apiClient, requirement, onClose, onMerged }: MergeRequirementDialogProps) {
  const dialogRef = useRef<HTMLDivElement>(null);
  useDialogFocus(dialogRef);
  const [draft, setDraft] = useState("");
  const [query, setQuery] = useState("");
  // 这条要带过去的东西、可选的需求；还没读回来是 null
  const [moving, setMoving] = useState<RequirementMoving | null>(null);
  const [items, setItems] = useState<RequirementMergeTarget[] | null>(null);
  const [choice, setChoice] = useState<RequirementMergeTarget | null>(null);
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);
  useDialogEscape(dialogRef, () => {
    if (!saving) onClose();
  });

  // 搜索打字时不每个字都查，停下来 300ms 再查（和需求池的名称搜索一样）
  useEffect(() => {
    if (draft === query) return;
    const timer = window.setTimeout(() => setQuery(draft), 300);
    return () => window.clearTimeout(timer);
  }, [draft, query]);

  useEffect(() => {
    let active = true;
    void apiClient
      .requirementMergeTargets(requirement.id, query.trim() || undefined)
      .then((next) => {
        if (!active) return;
        setMoving(next.moving);
        setItems(next.items);
        setError("");
        // 选中的那条被搜索筛掉了就不再算选中：底部预览和［并入］只对看得见的那条
        setChoice((current) => (current && next.items.some((item) => item.id === current.id) ? current : null));
      })
      .catch((err: unknown) => {
        if (!active) return;
        // 没读到不等于没有别的需求：不写「这个项目里没有别的需求」，只给后端的原因（比如这条已经在别处并走了）
        setError(err instanceof Error ? err.message : "读取需求失败");
      });
    return () => {
      active = false;
    };
  }, [apiClient, query, requirement.id]);

  const merge = async () => {
    if (!choice || saving) return;
    setSaving(true);
    setError("");
    try {
      onMerged(await apiClient.mergeRequirement(requirement.id, choice.id));
    } catch (err) {
      setError(err instanceof Error ? err.message : "并入失败，请稍后重试");
      setSaving(false);
    }
  };

  const heading = `把「${requirement.title}」并入`;

  return (
    <div className="pool-dialog__overlay">
      <div aria-label={heading} aria-modal="true" className="pool-dialog" ref={dialogRef} role="dialog">
        <header className="pool-dialog__head">
          <h2>{heading}</h2>
          <button aria-label="关闭" className="pool-dialog__close" disabled={saving} onClick={onClose} type="button">
            ✕
          </button>
        </header>

        <label className="merge-search">
          <svg aria-hidden="true" fill="none" height="14" stroke="currentColor" strokeWidth="1.6" viewBox="0 0 16 16" width="14">
            <circle cx="7" cy="7" r="5" />
            <path d="M11 11l3.5 3.5" strokeLinecap="round" />
          </svg>
          {/* 打开就能直接打字搜 */}
          <input
            aria-label="搜需求名称"
            data-autofocus
            disabled={saving}
            onChange={(event) => setDraft(event.target.value)}
            placeholder="搜需求名称"
            value={draft}
          />
        </label>

        {(items !== null || !error) && (
          <p className="pool-dialog__section">{requirement.project_name ?? "这个项目"}里的其他需求</p>
        )}
        {items === null ? (
          !error && <p className="pool-dialog__state">正在读取…</p>
        ) : items.length === 0 ? (
          <p className="pool-dialog__state">
            {query.trim() ? `没有名称含「${query.trim()}」的需求` : "这个项目里没有别的需求"}
          </p>
        ) : (
          <div aria-label="并入哪条需求" className="merge-targets" role="radiogroup">
            {items.map((target) => (
              <label className={`merge-target ${choice?.id === target.id ? "is-checked" : ""}`} key={target.id}>
                <input
                  checked={choice?.id === target.id}
                  disabled={saving}
                  name="merge-into"
                  onChange={() => setChoice(target)}
                  type="radio"
                  value={target.id}
                />
                <span className="merge-target__body">
                  <span className="merge-target__title">{target.title}</span>
                  <span className="merge-target__meta">
                    {target.open_task_count} 待办 · {target.meeting_count} 场会
                  </span>
                </span>
                <span className={`merge-target__status merge-target__status--${target.status}`}>
                  {REQUIREMENT_STATUS_LABELS[target.status]}
                </span>
                <span className={`merge-target__priority merge-target__priority--${target.priority.toLowerCase()}`}>
                  {target.priority}
                </span>
              </label>
            ))}
          </div>
        )}

        {choice && moving && (
          <div className="merge-preview" aria-live="polite">
            <p>
              带过去：{moving.open_task_count} 待办 · {moving.meeting_count} 场会 · {moving.source_count} 句原话 ·{" "}
              {moving.folder_count} 个文件夹
            </p>
            <p className="merge-preview__after">
              合并后 {higherPriority(moving.priority, choice.priority)}（取较高）· 10 分钟内可撤销
            </p>
          </div>
        )}
        {error && (
          <p className="pool-dialog__error" role="alert">
            {error}
          </p>
        )}
        <footer className="pool-dialog__foot">
          <button className="pool-dialog__cancel" disabled={saving} onClick={onClose} type="button">
            取消
          </button>
          <button className="pool-dialog__submit" disabled={!choice || saving} onClick={() => void merge()} type="button">
            {saving ? "并入中…" : "并入"}
          </button>
        </footer>
      </div>
    </div>
  );
}
