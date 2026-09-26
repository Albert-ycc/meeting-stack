import { useEffect, useRef, useState } from "react";

import type { ApiClient } from "../api";
import type { MeetingGlossary, MeetingGlossaryHit } from "../types";
import "./MeetingGlossaryPanel.css";

interface MeetingGlossaryPanelProps {
  apiClient: ApiClient;
  meetingId: string;
  glossary: MeetingGlossary;
  /** 桌面端、纪要没有没保存的改动时才能改纪要 */
  canEdit: boolean;
  /** 不能改时按钮上的说明；手机上不传，直接不出改纪要的按钮 */
  editBlockedReason?: string;
  isMobile: boolean;
  /** 纪要被改过或撤销以后：提示一句并重新载入会议 */
  onMinutesChanged: (message: string) => Promise<void>;
}

const PAIR_LIMIT = 5;

function pairs(hits: MeetingGlossaryHit[]) {
  const shown = hits.slice(0, PAIR_LIMIT).map((hit) => `${hit.wrong}→${hit.term}`);
  return hits.length > PAIR_LIMIT ? `${shown.join("、")} 等` : shown.join("、");
}

function basisLine(glossary: MeetingGlossary): string {
  const { receipt, project } = glossary;
  const name = project?.name ? `「${project.name}」` : "";
  switch (glossary.basis) {
    case "receipt": {
      if (!receipt || receipt.snapshot_missing) return "出纪要时没读到词典，这版纪要没按词典纠错";
      if (!receipt.project_id) return `出纪要时没认出项目，只用了 ${receipt.term_count} 条公共词`;
      const found = receipt.project_source === "transcript" ? "；项目是按逐字稿认出来的" : "";
      const shown = project?.name ?? receipt.project_name;
      return `出纪要时按${shown ? `「${shown}」` : "项目"}的词典纠错，用了 ${receipt.term_count} 条词（项目词 ${receipt.project_terms} 条、公共词 ${receipt.public_terms} 条）${found}`;
    }
    case "meeting":
      return `这版纪要出的时候还没按项目挑词，下面按${name}的词典查了一遍`;
    case "chosen":
      return `按你选的${name}的词典查了一遍`;
    default:
      return "这场会还没归项目，只按公共词查了一遍";
  }
}

/**
 * 会议纪要下方的「词典」小节：出纪要时按哪个项目纠的错、用了几条；纠正了哪些；
 * 纪要里还留着的错写可以一键改过来（改完可撤销）；归属和纠错用的项目不一样时可以换个项目再查。
 */
export function MeetingGlossaryPanel({
  apiClient,
  meetingId,
  glossary,
  canEdit,
  editBlockedReason,
  isMobile,
  onMinutesChanged,
}: MeetingGlossaryPanelProps) {
  const [view, setView] = useState(glossary);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const recheckedFor = useRef<string | null>(null);

  useEffect(() => setView(glossary), [glossary]);

  // 纪要改过以后（存了草稿、回滚了版本）体检结果就旧了，悄悄重查一遍
  useEffect(() => {
    if (!view.stale || recheckedFor.current === view.checked_at) return;
    recheckedFor.current = view.checked_at;
    void apiClient
      .checkMeetingGlossary(meetingId)
      .then((result) => {
        if (result.glossary) setView(result.glossary);
      })
      .catch(() => undefined);
  }, [apiClient, meetingId, view.checked_at, view.stale]);

  const run = async (work: () => Promise<void>) => {
    setBusy(true);
    setError("");
    try {
      await work();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "操作失败，请稍后重试");
    } finally {
      setBusy(false);
    }
  };

  const recheck = (projectId: string | null) =>
    void run(async () => {
      const result = await apiClient.checkMeetingGlossary(meetingId, projectId);
      if (result.glossary) setView(result.glossary);
    });

  const apply = () =>
    void run(async () => {
      if (!view.minutes_version_id) return;
      const result = await apiClient.applyMeetingGlossary(meetingId, view.minutes_version_id);
      if (result.glossary) setView(result.glossary);
      await onMinutesChanged(`已把 ${result.replaced} 处错写改成词典里的写法，可以撤销`);
    });

  const undo = () =>
    void run(async () => {
      const result = await apiClient.undoMeetingGlossary(meetingId);
      if (result.glossary) setView(result.glossary);
      await onMinutesChanged("已撤销，纪要回到按词典改之前");
    });

  const { corrected, missed, applied } = view;
  const missedCount = missed.reduce((sum, hit) => sum + hit.minutes_count, 0);
  const editTitle = canEdit ? undefined : editBlockedReason;
  const quiet = corrected.length === 0 && missed.length === 0 && !applied;

  return (
    <section aria-label="词典" className="meeting-glossary">
      <p className="meeting-glossary__lead">
        <strong>词典</strong>
        <span>{basisLine(view)}</span>
        {view.basis === "chosen" && (
          <button className="text-button" disabled={busy} onClick={() => recheck(null)} type="button">
            回到默认
          </button>
        )}
      </p>

      {view.mismatch && view.meeting_project && (
        <p className="meeting-glossary__row">
          <span>
            这场会现在归在「{view.meeting_project.name}」，和纠错用的不是同一个项目。
          </span>
          <button
            className="ghost-button"
            disabled={busy}
            onClick={() => recheck(view.meeting_project!.id)}
            type="button"
          >
            按「{view.meeting_project.name}」的词典检查
          </button>
        </p>
      )}

      {applied && (
        <p className="meeting-glossary__row meeting-glossary__applied">
          <span>
            {applied.by === "auto"
              ? `出纪要后已按词典自动改了 ${applied.count} 处漏纠的错写`
              : `已按词典改了 ${applied.count} 处`}
          </span>
          {applied.can_undo && !isMobile && (
            <button
              className="text-button"
              disabled={busy || !canEdit}
              onClick={undo}
              title={editTitle}
              type="button"
            >
              撤销
            </button>
          )}
        </p>
      )}

      {corrected.length > 0 && (
        <p className="meeting-glossary__row">
          <span>
            纠正了 {corrected.length} 处：{pairs(corrected)}
          </span>
        </p>
      )}

      {missed.length > 0 && (
        <div className="meeting-glossary__missed">
          <p className="meeting-glossary__row">
            <span>可能漏纠 {missedCount} 处，纪要里还留着错写：</span>
            {!isMobile && (
              <button
                className="ghost-button"
                disabled={busy || !canEdit}
                onClick={apply}
                title={editTitle}
                type="button"
              >
                改过来
              </button>
            )}
          </p>
          <ul>
            {missed.map((hit) => (
              <li key={`${hit.wrong}-${hit.term}`}>
                <s>{hit.wrong}</s> → {hit.term}
                <span className="meeting-glossary__count">纪要里 {hit.minutes_count} 次</span>
              </li>
            ))}
          </ul>
          {isMobile && <p className="meeting-glossary__note">在电脑上打开可以一键改过来</p>}
        </div>
      )}

      {quiet && <p className="meeting-glossary__note">没发现要改的错写</p>}
      {error && <p className="meeting-glossary__error">{error}</p>}
    </section>
  );
}
