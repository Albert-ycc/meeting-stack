import { GlossaryTargetButton, targetName } from "./GlossaryTargetButton";
import type { GlossarySuggestion, GlossaryTarget, LoadState, MeetingSummary, Project } from "../types";
import { formatFullDate } from "./glossaryModel";
import { IconBack, IconInbox } from "./glossaryUi";

export type SuggestionStatus = "pending" | "confirmed" | "rejected";

const STATUS_TABS: Array<{ key: SuggestionStatus; label: string }> = [
  { key: "pending", label: "待确认" },
  { key: "confirmed", label: "已确认" },
  { key: "rejected", label: "已驳回" },
];

const EMPTY_TEXT: Record<SuggestionStatus, [string, string]> = {
  pending: ["这里还没有待确认建议。", "编辑纪要时的错字更正会出现在这里等你处理。"],
  confirmed: ["还没有已确认的建议。", "在「待确认」里记入的更正会留在这里，可以撤销。"],
  rejected: ["还没有已驳回的建议。", "标成「不是错字」的更正会留在这里，可以恢复。"],
};

/** 来源会议只显示标题；会议不在当前列表页时退成档案号前段，仍可辨识。 */
function meetingLabel(suggestion: GlossarySuggestion, meetings: MeetingSummary[]): string {
  const meetingId = suggestion.meeting_id;
  if (!meetingId) return "编辑纪要时捕获";
  if (suggestion.meeting_title) return suggestion.meeting_title;
  const meeting = meetings.find((item) => item.id === meetingId);
  return meeting?.title ?? `会议 ${meetingId.slice(0, 8)}…`;
}

interface SuggestionsPaneProps {
  status: SuggestionStatus;
  onStatus: (status: SuggestionStatus) => void;
  counts: Record<SuggestionStatus, number>;
  items: GlossarySuggestion[];
  state: LoadState;
  canWrite: boolean;
  busy: boolean;
  projects: Project[];
  meetings: MeetingSummary[];
  shortPicks: Record<string, boolean>;
  onShortPick: (id: string, on: boolean) => void;
  onConfirm: (suggestion: GlossarySuggestion, target: GlossaryTarget) => void;
  onUndo: (suggestion: GlossarySuggestion) => void;
  onReject: (suggestion: GlossarySuggestion) => void;
  onRestore: (suggestion: GlossarySuggestion) => void;
  onRetry: () => void;
  onBack: () => void;
}

/** 待确认队列：三个状态页签，一条一格，大字的「错写 → 正确写法」。 */
export function GlossarySuggestionsPane({
  status, onStatus, counts, items, state, canWrite, busy, projects, meetings, shortPicks, onShortPick,
  onConfirm, onUndo, onReject, onRestore, onRetry, onBack,
}: SuggestionsPaneProps) {
  const renderActions = (suggestion: GlossarySuggestion) => {
    if (!canWrite) return null;
    if (suggestion.status === "confirmed") {
      return (
        <>
          <span className="gw-sgstate gw-sgstate--ok">已确认</span>
          <button className="gw-btn gw-btn--sm" disabled={busy} onClick={() => onUndo(suggestion)} type="button">
            撤销
          </button>
        </>
      );
    }
    if (suggestion.status === "rejected") {
      return (
        <>
          <span className="gw-sgstate">已驳回</span>
          <button className="gw-btn gw-btn--sm" disabled={busy} onClick={() => onRestore(suggestion)} type="button">
            恢复
          </button>
        </>
      );
    }
    return (
      <>
        {suggestion.existing_term_id ? (
          <button className="gw-btn gw-btn--pri" disabled={busy} onClick={() => onConfirm(suggestion, "auto")} type="button">
            加到那条
          </button>
        ) : (
          <GlossaryTargetButton
            defaultProjectId={suggestion.target_project_id}
            defaultProjectName={suggestion.target_project_name}
            disabled={busy}
            onConfirm={(target) => onConfirm(suggestion, target)}
            projects={projects}
            variant="workbench"
          />
        )}
        <button className="gw-btn" disabled={busy} onClick={() => onReject(suggestion)} type="button">
          不是错字
        </button>
      </>
    );
  };

  const renderRow = (suggestion: GlossarySuggestion) => {
    const short = Boolean(shortPicks[suggestion.id]) && suggestion.status === "pending";
    const hasAlt = Boolean(suggestion.alt_wrong && suggestion.alt_correct);
    return (
      <div className="gw-sg" key={suggestion.id}>
        <div className="gw-pair">
          <span className="gw-pair__wr">{short ? suggestion.alt_wrong : suggestion.confirmed_wrong || suggestion.wrong}</span>
          <span aria-hidden="true" className="gw-pair__ar">→</span>
          <span>{short ? suggestion.alt_correct : suggestion.correct}</span>
        </div>
        <div className="gw-sgacts">{renderActions(suggestion)}</div>
        {canWrite && hasAlt && suggestion.status === "pending" && (
          <label className="gw-check">
            <input
              checked={short}
              onChange={(event) => onShortPick(suggestion.id, event.target.checked)}
              type="checkbox"
            />
            <span>只记 2 字</span>
          </label>
        )}
        {suggestion.context && <div className="gw-sgl gw-sgl--ctx">「{suggestion.context}」</div>}
        {suggestion.status === "pending" && (
          <div className="gw-sgl gw-sgl--ex">
            {suggestion.existing_term_id
              ? `词典里已有『${suggestion.correct}』（${targetName(suggestion.existing_term_project_name)}），会加到那条`
              : "词典里还没有这个词，记入后新建一条"}
          </div>
        )}
        <div className="gw-sgl">
          来自 {meetingLabel(suggestion, meetings)}
          {suggestion.status === "pending" && ` · 会议现在在 ${targetName(suggestion.target_project_name)}`}
          {suggestion.status === "confirmed" &&
            suggestion.existing_term_id &&
            ` · 记在 ${targetName(suggestion.existing_term_project_name)}`}
          {" · "}
          {formatFullDate(suggestion.created_at)}
        </div>
      </div>
    );
  };

  return (
    <div className="gw-col gw-sugwrap">
      <div className="gw-sughead">
        <button className="gw-btn gw-btn--sm gw-btn--ghost gw-mback" onClick={onBack} type="button">
          <IconBack />
          范围
        </button>
        <h2>待确认</h2>
        <div aria-label="建议状态" className="gw-seg" role="tablist">
          {STATUS_TABS.map((tab) => (
            <button aria-selected={status === tab.key} key={tab.key} onClick={() => onStatus(tab.key)} role="tab" type="button">
              {tab.label} <span className="gw-cnt">{counts[tab.key]}</span>
            </button>
          ))}
        </div>
      </div>
      <div className="gw-sugbody">
        {state === "loading" && <p className="gw-note">正在读取待确认建议…</p>}
        {state === "error" && (
          <div className="gw-empty" role="alert">
            <strong>待确认建议读取失败</strong>
            <button className="gw-btn" onClick={onRetry} type="button">
              重试
            </button>
          </div>
        )}
        {state !== "loading" && state !== "error" && items.length === 0 && (
          <div className="gw-sgempty">
            <IconInbox />
            <div>
              <strong>{EMPTY_TEXT[status][0]}</strong>
              <p>{EMPTY_TEXT[status][1]}</p>
            </div>
          </div>
        )}
        {state !== "loading" && state !== "error" && items.length > 0 && (
          <div aria-label="待确认建议" className="gw-sglist">
            {items.map(renderRow)}
          </div>
        )}
      </div>
    </div>
  );
}
