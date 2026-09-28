import { useState, type ReactNode } from "react";

import type { ApiClient, DecisionDismissed, DecisionLinkRef, DecisionRestatedRef, RelationKind } from "../../api";
import { formatTime } from "../../format";
import type { MiniPlayerHandle } from "../graph/MiniPlayer";
import { Quotes } from "../graph/quotes";
import { dismissedMark, earlierMark, laterMark, restatedMark } from "./decisionText";
import "./decisions.css";

/** 一条决议要画的：台账跟上时有 id 和标记；现读兜底（id 为 null）时只有文字和时间点 */
export interface DecisionRowData {
  id: string | null;
  text: string;
  detail?: string;
  start_ms: number | null;
  later?: DecisionLinkRef[];
  earlier?: DecisionLinkRef[];
  restated?: DecisionRestatedRef[];
  dismissed?: DecisionDismissed[];
}

export interface DecisionRowProps {
  decision: DecisionRowData;
  /** 这条决议所在的会；▶ 用它的录音 */
  meeting: { id: string; title: string; audio_url: string | null };
  /** 宿主持有的那一个播放器（需求卡、时间线各调一次 useMiniPlayer，关系图用画布的） */
  player: MiniPlayerHandle;
  apiClient: Pick<ApiClient, "meetingQuotes">;
  /** 标记行上的［不是一回事］；没给或 canWrite 为假时不出按钮 */
  onDismiss?: (relationId: number, kind: Extract<RelationKind, "later_changed" | "restated">, decisionId: string) => void;
  /** 标过的那一行上的［撤销］（发 restore） */
  onRestore?: (item: DecisionDismissed) => void;
  onOpenMeeting?: (meetingId: string, seekMs?: number) => void;
  canWrite: boolean;
  /** 正在发送时按钮变灰 */
  busy?: boolean;
  /** 行上另外的按钮（［不属于这个需求］、［放到这个需求］），怎么出现看 actionsShown */
  actions?: ReactNode;
  /**
   * hover（默认，［不属于这个需求］）：电脑上悬停或聚焦时出现，手机上放在「原话」展开里；这条没有「原话」
   * （没有时间点）时画在行尾一直显示，手机上也点得到。always（没归到的那几条后面的［放到这个需求］）：
   * 一直画在行尾。
   */
  actionsShown?: "hover" | "always";
  /** 行后面的小签（时间线［决议］里的需求名） */
  tag?: ReactNode;
  /** 不画「打开会议」（展开一场会的面板里就是这场会） */
  hideOpenMeeting?: boolean;
  /** 不画「原话」（展开一场会的面板里另有一节） */
  hideQuotes?: boolean;
  /** 4e：标记行下面的问题块（需求卡的「『报价单 v3』之后没改过，可能过时」） */
  questions?: ReactNode;
}

function PlayAt({
  player,
  audioUrl,
  atMs,
  label,
}: {
  player: MiniPlayerHandle;
  audioUrl: string | null;
  atMs: number | null;
  label: string;
}) {
  // 没有录音或没有时间点时不出 ▶
  if (!audioUrl || atMs === null || atMs === undefined) return null;
  const time = formatTime(atMs);
  return (
    <button
      aria-label={`从 ${time} 听这条`}
      className="decision-row__play"
      onClick={() => player.play(audioUrl, atMs, label)}
      type="button"
    >
      ▶ {time}
    </button>
  );
}

/**
 * 一条决议：文字；▶「12:34」从锚点前 3 秒放到后 15 秒；「原话」展开前后各 20 秒；「打开会议」；下面是标记行
 * （后来改了、这次改了、后来又提到，都用［不是一回事］；标过的灰字加［撤销］）。后来改了的早的那条字用浅色，不划掉。
 */
export function DecisionRow({
  decision,
  meeting,
  player,
  apiClient,
  onDismiss,
  onRestore,
  onOpenMeeting,
  canWrite,
  busy = false,
  actions,
  actionsShown = "hover",
  tag,
  hideOpenMeeting = false,
  hideQuotes = false,
  questions,
}: DecisionRowProps) {
  const [open, setOpen] = useState(false);
  const later = decision.id ? decision.later ?? [] : [];
  const earlier = decision.id ? decision.earlier ?? [] : [];
  const restated = decision.id ? decision.restated ?? [] : [];
  const dismissed = decision.id ? decision.dismissed ?? [] : [];
  const canDismiss = canWrite && Boolean(onDismiss) && Boolean(decision.id);
  const canRestore = canWrite && Boolean(onRestore);
  const hasQuotes = !hideQuotes && decision.start_ms !== null;
  // 一直显示：要求常显，或没有「原话」展开可放（手机上否则就点不到）
  const pinned = Boolean(actions) && (actionsShown === "always" || !hasQuotes);

  return (
    <div className={`decision-row${later.length ? " is-changed" : ""}`}>
      <div className="decision-row__main">
        <span className="decision-row__text">{decision.text}</span>
        {tag}
        <span className="decision-row__tools">
          <PlayAt atMs={decision.start_ms} audioUrl={meeting.audio_url} label={meeting.title} player={player} />
          {hasQuotes && (
            <button aria-expanded={open} className="text-button" onClick={() => setOpen((value) => !value)} type="button">
              原话
            </button>
          )}
          {!hideOpenMeeting && onOpenMeeting && (
            <button
              className="text-button"
              onClick={() => onOpenMeeting(meeting.id, decision.start_ms ?? undefined)}
              type="button"
            >
              打开会议
            </button>
          )}
          {actions && !pinned && <span className="decision-row__hover-actions">{actions}</span>}
          {pinned && <span className="decision-row__end-actions">{actions}</span>}
        </span>
      </div>
      {open && (
        <div className="decision-row__quotes">
          <Quotes
            apiClient={apiClient}
            atMs={decision.start_ms}
            audioUrl={meeting.audio_url}
            label={meeting.title}
            meetingId={meeting.id}
            player={player}
          />
          {actions && !pinned && <span className="decision-row__mobile-actions">{actions}</span>}
        </div>
      )}
      <DecisionMarks
        busy={busy}
        dismissed={dismissed}
        earlier={earlier}
        later={later}
        onDismiss={canDismiss ? (relationId, kind) => onDismiss?.(relationId, kind, decision.id as string) : undefined}
        onRestore={canRestore ? onRestore : undefined}
        player={player}
        restated={restated}
      />
      {questions && <div className="decision-row__questions">{questions}</div>}
    </div>
  );
}

export interface DecisionMarksProps {
  later: DecisionLinkRef[];
  earlier: DecisionLinkRef[];
  restated: DecisionRestatedRef[];
  dismissed: DecisionDismissed[];
  player: MiniPlayerHandle;
  /** 没给时不出［不是一回事］（手机上 canWrite 为假、旧后台） */
  onDismiss?: (relationId: number, kind: Extract<RelationKind, "later_changed" | "restated">) => void;
  onRestore?: (item: DecisionDismissed) => void;
  busy?: boolean;
}

/**
 * 标记行：早的那条「后来改了：…」、晚的那条「这次改了 … 定的『…』」、「后来又提到：…」，三种都用［不是一回事］；
 * 标过的灰字「你标过和 … 那条不是一回事」［撤销］。别的会的 ▶ 用那一条的 audio_url。需求卡和展开一场会共用。
 */
export function DecisionMarks({ later, earlier, restated, dismissed, player, onDismiss, onRestore, busy = false }: DecisionMarksProps) {
  if (!later.length && !earlier.length && !restated.length && !dismissed.length) return null;
  const dismissButton = (relationId: number | null, kind: "later_changed" | "restated") =>
    onDismiss && relationId !== null ? (
      <button className="text-button decision-row__answer" disabled={busy} onClick={() => onDismiss(relationId, kind)} type="button">
        不是一回事
      </button>
    ) : null;
  return (
    <ul className="decision-row__marks">
      {later.map((ref) => (
        <li key={`later-${ref.relation_id}`}>
          <span>{laterMark(ref)}</span>
          <PlayAt atMs={ref.start_ms} audioUrl={ref.audio_url} label={ref.meeting.title} player={player} />
          {dismissButton(ref.relation_id, "later_changed")}
        </li>
      ))}
      {earlier.map((ref) => (
        <li key={`earlier-${ref.relation_id}`}>
          <span>{earlierMark(ref)}</span>
          <PlayAt atMs={ref.start_ms} audioUrl={ref.audio_url} label={ref.meeting.title} player={player} />
          {dismissButton(ref.relation_id, "later_changed")}
        </li>
      ))}
      {restated.map((ref) => (
        <li key={`restated-${ref.decision_id}`}>
          <span>{restatedMark(ref)}</span>
          <PlayAt atMs={ref.start_ms} audioUrl={ref.audio_url} label={ref.meeting.title} player={player} />
          {dismissButton(ref.relation_id, "restated")}
        </li>
      ))}
      {dismissed.map((item) => (
        <li className="decision-row__dismissed" key={`dismissed-${item.relation_id}`}>
          <span>{dismissedMark(item)}</span>
          {onRestore && (
            <button className="text-button" disabled={busy} onClick={() => onRestore(item)} type="button">
              撤销
            </button>
          )}
        </li>
      ))}
    </ul>
  );
}
