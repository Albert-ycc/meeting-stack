import { useEffect, useMemo, useRef, useState, type KeyboardEvent } from "react";

import type { ApiClient } from "../../api";
import type { GraphPayload } from "./graphTypes";
import type { LaidNode, StarLayout } from "./layout";

/** 两个字起才去翻逐字稿，一个字太泛 */
const FULLTEXT_MIN = 2;
const DEBOUNCE_MS = 250;

function searchableText(node: LaidNode): string {
  switch (node.kind) {
    case "meeting":
    case "doorstep":
    case "ghost":
      return node.data.title;
    case "requirement":
      return node.data.title;
    case "folder":
      return node.data.name;
    case "cue":
      return node.data.text;
    case "beacon":
      return node.data.project_name;
    case "collapsed":
      return node.data.label;
    default:
      return "";
  }
}

/** 图上标题、需求、文件夹、线索词里含这个词的节点，按从上到下、从左到右排 */
export function localMatches(layout: StarLayout, query: string): string[] {
  const needle = query.trim().toLowerCase();
  if (!needle) return [];
  return layout.nodes
    .filter((node) => searchableText(node).toLowerCase().includes(needle))
    .sort((a, b) => a.y - b.y || a.x - b.x)
    .map((node) => node.id);
}

/** 逐字稿命中的会落在图上的哪个节点：会议、门口，或者折叠的那一组 */
export function meetingNodeId(graph: GraphPayload, layout: StarLayout, meetingId: string): string | null {
  if (layout.byId.has(`m:${meetingId}`)) return `m:${meetingId}`;
  if (layout.byId.has(`d:${meetingId}`)) return `d:${meetingId}`;
  const group = graph.collapsed.find((item) => item.meeting_ids.includes(meetingId));
  return group && layout.byId.has(group.id) ? group.id : null;
}

export interface GraphSearchProps {
  apiClient: ApiClient;
  graph: GraphPayload;
  layout: StarLayout;
  /** 点亮命中的节点；null 表示不点亮 */
  onHighlight: (ids: string[] | null) => void;
  onSelect: (id: string) => void;
}

/**
 * 底栏的「在图上找」：标题、需求、文件夹、线索词和逐字稿里说到这个词的会一起点亮，回车逐个跳。
 * 逐字稿不受时间窗限制，图上没画出来的会只算在数里。
 */
export function GraphSearch({ apiClient, graph, layout, onHighlight, onSelect }: GraphSearchProps) {
  const [query, setQuery] = useState("");
  const [transcript, setTranscript] = useState<{ query: string; ids: string[]; meetings: number } | null>(null);
  const [cursor, setCursor] = useState(-1);
  const inputRef = useRef<HTMLInputElement>(null);
  const trimmed = query.trim();

  useEffect(() => {
    setCursor(-1);
    if (trimmed.length < FULLTEXT_MIN) {
      setTranscript(null);
      return;
    }
    let active = true;
    const timer = window.setTimeout(() => {
      apiClient
        .graphFulltext(graph.project.id, { q: trimmed })
        .then((payload) => {
          if (!active) return;
          const ids = payload.meetings
            .map((item) => meetingNodeId(graph, layout, item.meeting_id))
            .filter((id): id is string => Boolean(id));
          setTranscript({ query: trimmed, ids, meetings: payload.meeting_count });
        })
        .catch(() => active && setTranscript({ query: trimmed, ids: [], meetings: 0 }));
    }, DEBOUNCE_MS);
    return () => {
      active = false;
      window.clearTimeout(timer);
    };
    // 图每 30 秒会刷新一次，不因此重新查
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [apiClient, graph.project.id, trimmed]);

  const matches = useMemo(() => {
    const local = localMatches(layout, trimmed);
    const extra = transcript && transcript.query === trimmed ? transcript.ids.filter((id) => !local.includes(id)) : [];
    return [...local, ...new Set(extra)];
  }, [layout, transcript, trimmed]);

  const highlightKey = trimmed ? matches.join("|") : "";
  useEffect(() => {
    onHighlight(trimmed ? matches : null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [highlightKey, trimmed]);

  const onKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    if (event.nativeEvent.isComposing) return;
    if (event.key === "Escape") {
      event.preventDefault();
      setQuery("");
      inputRef.current?.blur();
      return;
    }
    if (event.key !== "Enter" || !matches.length) return;
    event.preventDefault();
    const next = event.shiftKey
      ? (cursor <= 0 ? matches.length : cursor) - 1
      : (cursor + 1) % matches.length;
    setCursor(next);
    onSelect(matches[next]);
  };

  const pending = trimmed.length >= FULLTEXT_MIN && (!transcript || transcript.query !== trimmed);
  let note = "";
  if (trimmed) {
    if (!matches.length) note = pending ? "正在找…" : "图上没找到";
    else note = `${cursor >= 0 ? `${cursor + 1}/` : ""}${matches.length} 个，回车逐个跳`;
    if (transcript && transcript.query === trimmed && transcript.meetings) {
      note += `；逐字稿里 ${transcript.meetings} 场会说到`;
    }
  }

  return (
    <div className="project-graph__search" role="search">
      <input
        aria-label="在图上找"
        onChange={(event) => setQuery(event.target.value)}
        onKeyDown={onKeyDown}
        placeholder="在图上找"
        ref={inputRef}
        type="search"
        value={query}
      />
      {note && (
        <span aria-live="polite" className="project-graph__search-note">
          {note}
        </span>
      )}
    </div>
  );
}
