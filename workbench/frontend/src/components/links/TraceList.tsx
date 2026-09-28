/* 4f：［来龙去脉］的列表（预览抽屉里，任何页面，手机上也有）。从早到晚，中心那一行加粗；行和行之间一行
   小字写这一步是什么；▶ 用宿主（抽屉）自己的播放器；电脑上最后加［在关系图上看 →］。只在按了按钮以后才取。 */
import { useCallback, useEffect, useState } from "react";

import { ApiError, isOldBackend, type ApiClient } from "../../api";
import { formatTime } from "../../format";
import type { LocalEdge, LocalNode, TracePayload } from "../graph/graphTypes";
import { OLD_BACKEND_TEXT } from "./useRelationAnswer";
import "./links.css";

export const TRACE_LOADING = "正在取这份文件的关系";
export const TRACE_FAILED = "来龙去脉没取到";
export const TRACE_BACK_CUT = "往前走到 3 步为止，更早的没展开";
export const TRACE_FORWARD_CUT = "往后走到 3 步为止，更晚的没展开";

/** 两边都没有时的那一句：按中心是什么说 */
export function traceEmptyText(center: { kind: string }): string {
  if (center.kind === "meeting") return "这场会还没有带原话的来龙去脉";
  if (center.kind === "decision") return "这条决议还没有带原话的来龙去脉";
  if (center.kind === "task") return "这条任务还没有带原话的来龙去脉";
  return "这份文件还没有带原话的来龙去脉";
}

/** 节点的日子：「9/14」 */
export function nodeDay(at: string | null | undefined): string {
  if (!at) return "";
  const moment = new Date(at);
  if (Number.isNaN(moment.getTime())) return "";
  return `${moment.getMonth() + 1}/${moment.getDate()}`;
}

/** 节点的名字（不带日子）：会名、决议『…』、任务『…』、文件名、需求名 */
export function nodeName(node: LocalNode): string {
  if (node.kind === "meeting") return node.title ?? "";
  if (node.kind === "decision") return `决议『${node.text ?? ""}』`;
  if (node.kind === "task") return `任务『${node.title ?? ""}』`;
  if (node.kind === "requirement") return `需求『${node.title ?? ""}』`;
  return node.name ?? "";
}

/** 两行之间那一行小字：这一步是什么 */
export function stepText(edge: LocalEdge | undefined): string {
  if (!edge) return "";
  switch (edge.kind) {
    case "same_name":
      return edge.label;
    case "task_from":
      return "会上提到这条任务";
    case "deliverable":
      return "交付物";
    case "affects":
      return "之后没改过";
    case "mentioned":
      return "会上提到这份文件";
    case "later_changed":
      return "后来改了";
    case "restated":
      return "后来又提到";
    case "in_meeting":
      return "这场会定的";
    default:
      return edge.label;
  }
}

/** 链上相邻两个节点之间的那条线（先找链本身的线） */
export function chainEdge(payload: TracePayload, a: string, b: string): LocalEdge | undefined {
  const between = payload.edges.filter(
    (edge) => (edge.from === a && edge.to === b) || (edge.from === b && edge.to === a),
  );
  return between.find((edge) => edge.on_chain) ?? between[0];
}

/** 会议一行后面的「会上说『报价单』· 00:12:34」和 ▶ 的时刻：取连着它的提到线 */
function said(payload: TracePayload, node: LocalNode): { text: string; atMs: number | null } {
  if (node.kind === "decision") return { text: "", atMs: node.start_ms ?? null };
  if (node.kind === "task") return { text: "", atMs: node.anchor_ms ?? null };
  if (node.kind !== "meeting") return { text: "", atMs: null };
  const mention = payload.edges.find((edge) => edge.kind === "mentioned" && edge.from === node.id);
  if (!mention) return { text: "", atMs: null };
  return { text: mention.label, atMs: mention.at_ms ?? null };
}

export interface TraceRow {
  id: string;
  node: LocalNode;
  text: string;
  atMs: number | null;
  center: boolean;
  /** 到下一行的那一步 */
  step: string;
}

export function traceRows(payload: TracePayload): TraceRow[] {
  const nodes = new Map<string, LocalNode>([[payload.center.id, payload.center], ...payload.nodes.map((node) => [node.id, node] as const)]);
  return payload.chain.flatMap((id, index) => {
    const node = nodes.get(id);
    if (!node) return [];
    const day = nodeDay(node.at);
    const extra = said(payload, node);
    const clock = node.kind === "decision" && extra.atMs !== null ? `· ${formatTime(extra.atMs, true)}` : "";
    const text = `${day ? `${day} ` : ""}${nodeName(node)}${extra.text ? ` · ${extra.text}` : ""}${clock}`;
    const next = payload.chain[index + 1];
    return [
      {
        id,
        node,
        text,
        atMs: extra.atMs,
        center: index === payload.center_index,
        step: next ? stepText(chainEdge(payload, id, next)) : "",
      },
    ];
  });
}

export type TracePlay = (audioUrl: string, atMs: number, label: string) => void;

export function TraceList({
  apiClient,
  node,
  onPlay,
  onOpenInGraph,
}: {
  apiClient: Pick<ApiClient, "graphTrace">;
  /** file:<id>、m:<id>、dec:<id>、task:<id> */
  node: string;
  onPlay: TracePlay;
  /** 电脑上才传：［在关系图上看 →］ */
  onOpenInGraph?: () => void;
}) {
  const [payload, setPayload] = useState<TracePayload | null>(null);
  const [error, setError] = useState<{ text: string; retry: boolean } | null>(null);
  const load = useCallback(async () => {
    setError(null);
    try {
      setPayload(await apiClient.graphTrace(node));
    } catch (reason) {
      if (isOldBackend(reason)) setError({ text: OLD_BACKEND_TEXT, retry: false });
      else if (reason instanceof ApiError && [404, 409].includes(reason.status)) setError({ text: reason.message, retry: false });
      else setError({ text: TRACE_FAILED, retry: true });
    }
  }, [apiClient, node]);
  useEffect(() => {
    void load();
  }, [load]);

  return (
    <section aria-label="来龙去脉" className="material-drawer__section trace-list">
      <h3>来龙去脉</h3>
      {error ? (
        <p className="trace-list__state" role="status">
          {error.text}
          {error.retry && (
            <button className="text-button" onClick={() => void load()} type="button">
              重试
            </button>
          )}
        </p>
      ) : !payload ? (
        <p className="trace-list__state">{TRACE_LOADING}</p>
      ) : (
        <TraceRows onPlay={onPlay} payload={payload} />
      )}
      {onOpenInGraph && payload && (
        <button className="text-button trace-list__graph" onClick={onOpenInGraph} type="button">
          在关系图上看 →
        </button>
      )}
    </section>
  );
}

export function TraceRows({ payload, onPlay }: { payload: TracePayload; onPlay: TracePlay }) {
  const rows = traceRows(payload);
  if (rows.length <= 1) return <p className="trace-list__state">{traceEmptyText(payload.center)}</p>;
  return (
    <>
      {payload.cut.back && <p className="trace-list__cut">{TRACE_BACK_CUT}</p>}
      <ol className="trace-list__rows">
        {rows.map((row) => (
          <li className={row.center ? "is-center" : undefined} key={row.id}>
            <span className="trace-list__row">
              {row.center ? <strong>{row.text}</strong> : row.text}
              {row.node.audio_url && row.atMs !== null && (
                <button
                  aria-label={`从 ${formatTime(row.atMs, true)} 播放`}
                  className="material-preview__seek"
                  onClick={() => onPlay(row.node.audio_url!, row.atMs ?? 0, nodeName(row.node))}
                  type="button"
                >
                  ▶
                </button>
              )}
            </span>
            {row.step && <small className="trace-list__step">↓ {row.step}</small>}
          </li>
        ))}
      </ol>
      {payload.cut.forward && <p className="trace-list__cut">{TRACE_FORWARD_CUT}</p>}
    </>
  );
}
