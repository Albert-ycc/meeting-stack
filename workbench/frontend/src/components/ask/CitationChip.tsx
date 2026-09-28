import { formatTime } from "../../format";
import type { AskSource, PreviewTarget } from "../../types";
import type { MiniPlayerHandle } from "../graph/MiniPlayer";

/*
 * 出处小块（4g）：会议、纪要、决议写「9/21 初审规则沟通 12:34」加 ▶，没有时间的纪要行写
 * 「9/21 初审规则沟通 · 纪要」；决议后来改了时后面灰字「后来改了 9/28」。材料写「报价单 v3.xlsx · 表『预算』」，
 * 点了打开预览抽屉到「回答引用的这段」；资料盘没插时变灰、悬停写「资料盘未连接」。悬停显示原话。
 */

export const OFFLINE_TITLE = "资料盘未连接";

/** 「9/21」；不是今年的写「2025/12/30」 */
export function shortDate(value: string | undefined, today: Date = new Date()): string {
  const match = /^(\d{4})-(\d{2})-(\d{2})/.exec(value ?? "");
  if (!match) return "";
  const text = `${Number(match[2])}/${Number(match[3])}`;
  return Number(match[1]) === today.getFullYear() ? text : `${match[1]}/${text}`;
}

/** 小块上的字 */
export function sourceLabel(source: AskSource, today?: Date): string {
  if (source.kind === "material") {
    return source.loc ? `${source.name ?? ""} · ${source.loc}` : source.name ?? "";
  }
  const head = `${shortDate(source.date, today)} ${source.title ?? ""}`.trim();
  if (source.start_ms === null || source.start_ms === undefined) {
    return source.kind === "decision" ? head : `${head} · 纪要`;
  }
  return `${head} ${formatTime(source.start_ms)}`;
}

/** 读屏名：「出处：9/21 初审规则沟通 12:34」「出处：报价单 v3.xlsx 表『预算』」 */
export function sourceAria(source: AskSource, today?: Date): string {
  if (source.kind === "material") {
    return `出处：${[source.name, source.loc].filter(Boolean).join(" ")}`;
  }
  return `出处：${sourceLabel(source, today)}`;
}

export interface ChipHandlers {
  onOpenMeeting: (meetingId: string, seekMs?: number, tab?: "transcript" | "minutes") => void;
  onOpenPreview: (target: PreviewTarget) => void;
  player: MiniPlayerHandle;
  /** 加亮的词（预览抽屉里用） */
  highlight: string[];
}

export function CitationChip({ source, handlers }: { source: AskSource; handlers: ChipHandlers }) {
  const label = sourceLabel(source);
  if (source.kind === "material") {
    const offline = source.root_online === false;
    return (
      <span className={`ask-chip ask-chip--material${offline ? " is-offline" : ""}`}>
        <button
          aria-label={sourceAria(source)}
          className="ask-chip__open"
          disabled={offline || source.file_id === undefined}
          onClick={() =>
            handlers.onOpenPreview({
              fileId: source.file_id as number,
              startMs: source.playable && source.start_ms !== null ? source.start_ms : undefined,
              passage: {
                contentKey: source.content_key ?? "",
                ordinal: source.ordinal ?? 0,
                from: "answer",
                words: handlers.highlight,
              },
            })
          }
          title={offline ? OFFLINE_TITLE : source.quote}
          type="button"
        >
          {label}
        </button>
      </span>
    );
  }
  const meetingId = source.meeting_id ?? "";
  const at = source.start_ms ?? undefined;
  const canPlay = Boolean(source.audio_url) && source.start_ms !== null && source.start_ms !== undefined;
  return (
    <span className="ask-chip">
      <button
        aria-label={sourceAria(source)}
        className="ask-chip__open"
        onClick={() => handlers.onOpenMeeting(meetingId, at, source.kind === "meeting" ? undefined : "minutes")}
        title={source.quote}
        type="button"
      >
        {label}
      </button>
      {canPlay && (
        <button
          aria-label={`从 ${formatTime(source.start_ms ?? 0)} 播放`}
          className="ask-chip__play"
          onClick={() =>
            handlers.player.play(
              source.audio_url as string,
              source.start_ms ?? 0,
              `${source.title ?? ""} ${formatTime(source.start_ms ?? 0)}`,
            )
          }
          type="button"
        >
          ▶
        </button>
      )}
      {source.kind === "decision" && source.later_changed && (
        <span className="ask-chip__later">后来改了 {shortDate(source.later_changed.date)}</span>
      )}
    </span>
  );
}
