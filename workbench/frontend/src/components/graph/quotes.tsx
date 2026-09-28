/* 前后各 20 秒的原话：展开一场会的面板和需求页「决议」卡、时间线共用（4c 从 FocusPanel.tsx 原样挪出来） */
import { useEffect, useState } from "react";

import type { ApiClient } from "../../api";
import type { QuotesPayload } from "./graphTypes";
import type { MiniPlayerHandle } from "./MiniPlayer";
import { PlayButton } from "./panelParts";
import "./GraphPanel.css";
import "./MeetingFocus.css";

type Segment = QuotesPayload["quotes"][number]["segments"][number];

/** 前后各 20 秒的原话；换一条决议或任务时重读 */
export function useWideQuotes(apiClient: Pick<ApiClient, "meetingQuotes">, meetingId: string, atMs: number | null) {
  const [state, setState] = useState<{ at: number | null; segments: Segment[] | null; error: string }>({
    at: null,
    segments: null,
    error: "",
  });
  useEffect(() => {
    if (atMs === null) return;
    let active = true;
    setState({ at: atMs, segments: null, error: "" });
    apiClient
      .meetingQuotes(meetingId, [atMs], "wide")
      .then((payload) => active && setState({ at: atMs, segments: payload.quotes[0]?.segments ?? [], error: "" }))
      .catch(
        (reason: unknown) =>
          active && setState({ at: atMs, segments: [], error: reason instanceof Error ? reason.message : "原话读取失败" }),
      );
    return () => {
      active = false;
    };
  }, [apiClient, atMs, meetingId]);
  return state.at === atMs ? state : { at: atMs, segments: null, error: "" };
}

export interface QuotesProps {
  apiClient: Pick<ApiClient, "meetingQuotes">;
  meetingId: string;
  audioUrl: string | null;
  /** 迷你播放器上写的名字（会名） */
  label: string;
  player: MiniPlayerHandle;
  atMs: number | null;
}

export function Quotes({ apiClient, meetingId, audioUrl, label, player, atMs }: QuotesProps) {
  const { segments, error } = useWideQuotes(apiClient, meetingId, atMs);
  if (atMs === null) return <p className="graph-panel__muted">没有时间点，找不到原话</p>;
  if (error) return <p className="graph-panel__error">{error}</p>;
  if (!segments) return <p className="graph-panel__muted">正在读原话…</p>;
  if (!segments.length) return <p className="graph-panel__muted">这段时间没有逐字稿</p>;
  return (
    <ul className="focus-panel__quotes">
      {segments.map((segment) => (
        <li className={segment.start_ms <= atMs && atMs < segment.end_ms ? "is-anchor" : ""} key={segment.segment_id}>
          <PlayButton atMs={segment.start_ms} audioUrl={audioUrl} label={label} player={player} />
          <span>
            {segment.speaker && <small>{segment.speaker}：</small>}
            {segment.text}
          </span>
        </li>
      ))}
    </ul>
  );
}
