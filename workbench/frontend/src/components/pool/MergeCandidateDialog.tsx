import { useEffect, useRef, useState } from "react";

import type { ApiClient } from "../../api";
import { formatMonthDay } from "../../format";
import type { MergeTarget, RequirementDetail } from "../../types";
import { REQUIREMENT_STATUS_LABELS } from "../RequirementBadges";
import { useDialogEscape, useDialogFocus } from "../useDialog";
import "./PoolDialogs.css";

interface MergeCandidateDialogProps {
  apiClient: ApiClient;
  candidate: { id: string; title: string; project_name: string | null };
  /** 认领页上改选的项目（撞名后「改为合并」）；不传按来源会议的归属 */
  projectId?: string | null;
  projectName?: string | null;
  /** 预选的需求：撞名时那一条；不传时选 AI 推荐的，没有就选第一条 */
  preferredId?: string | null;
  onClose: () => void;
  onMerged: (requirement: RequirementDetail) => void;
}

/** S03 合并到已有需求：只列所属项目里进行中、已搁置的需求，AI 推荐的排第一。 */
export function MergeCandidateDialog({
  apiClient,
  candidate,
  projectId,
  projectName,
  preferredId,
  onClose,
  onMerged,
}: MergeCandidateDialogProps) {
  const dialogRef = useRef<HTMLDivElement>(null);
  useDialogFocus(dialogRef);
  const [targets, setTargets] = useState<MergeTarget[] | null>(null);
  const [choice, setChoice] = useState<string | null>(null);
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);
  const shownProject = projectName ?? candidate.project_name;
  useDialogEscape(dialogRef, () => {
    if (!saving) onClose();
  });

  useEffect(() => {
    let active = true;
    void apiClient
      .candidateMergeTargets(candidate.id, projectId)
      .then((payload) => {
        if (!active) return;
        setTargets(payload.items);
        const preferred = payload.items.find((item) => item.id === preferredId);
        const recommended = payload.items.find((item) => item.recommended);
        setChoice((preferred ?? recommended ?? payload.items[0])?.id ?? null);
      })
      .catch((err: unknown) => {
        if (!active) return;
        setTargets([]);
        setError(err instanceof Error ? err.message : "读取需求失败");
      });
    return () => {
      active = false;
    };
  }, [apiClient, candidate.id, preferredId, projectId]);

  const merge = async () => {
    if (!choice || saving) return;
    setSaving(true);
    setError("");
    try {
      onMerged(await apiClient.mergeCandidate(candidate.id, choice, projectId));
    } catch (err) {
      setError(err instanceof Error ? err.message : "合并失败，请稍后重试");
      setSaving(false);
    }
  };

  return (
    <div className="pool-dialog__overlay">
      <div aria-label="合并到已有需求" aria-modal="true" className="pool-dialog" ref={dialogRef} role="dialog">
        <header className="pool-dialog__head">
          <div>
            <h2>合并到已有需求</h2>
            <p>
              候选「{candidate.title}」{shownProject ? ` · ${shownProject}` : ""}
            </p>
          </div>
          <button aria-label="关闭" className="pool-dialog__close" disabled={saving} onClick={onClose} type="button">
            ✕
          </button>
        </header>

        <p className="pool-dialog__section">{shownProject ?? "这个项目"} · 进行中和已搁置的需求</p>
        {targets === null ? (
          <p className="pool-dialog__state">正在读取…</p>
        ) : targets.length === 0 ? (
          <p className="pool-dialog__state">这个项目下没有进行中或已搁置的需求，只能认领或丢掉。</p>
        ) : (
          <div aria-label="要合并到哪条需求" className="merge-targets" role="radiogroup">
            {targets.map((target) => (
              <label className={`merge-target ${choice === target.id ? "is-checked" : ""}`} key={target.id}>
                <input
                  checked={choice === target.id}
                  disabled={saving}
                  name="merge-target"
                  onChange={() => setChoice(target.id)}
                  type="radio"
                  value={target.id}
                />
                <span className="merge-target__body">
                  <span className="merge-target__title">
                    {target.title}
                    {target.recommended && <span className="merge-target__ai">AI 推荐</span>}
                  </span>
                  <span className="merge-target__meta">
                    {[target.meeting_title, target.recording_date ? formatMonthDay(target.recording_date) : null]
                      .filter(Boolean)
                      .join(" · ") || "还没有关联会议"}
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

        <p className="pool-dialog__note">
          <svg aria-hidden="true" fill="none" height="12" stroke="currentColor" strokeLinecap="round" strokeWidth="1.6" viewBox="0 0 16 16" width="12">
            <path d="M6.6 9.4l2.8-2.8" />
            <path d="M7.4 4.6l1.1-1.1a2.8 2.8 0 014 4l-1.1 1.1" />
            <path d="M8.6 11.4l-1.1 1.1a2.8 2.8 0 01-4-4l1.1-1.1" />
          </svg>
          合并后，这场会和原话会加进这条需求，候选消失
        </p>
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
            {saving ? "合并中…" : "合并"}
          </button>
        </footer>
      </div>
    </div>
  );
}
