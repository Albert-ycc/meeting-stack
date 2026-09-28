/* 4f：局部图和来龙去脉的右侧面板：按选中的文件、会议、决议、任务、需求、线给内容；没选时是中心那份文件
   和「还有 N 个没画出来」。文件面板沿用星图的 FilePanelBody（4e 的问题排第一）。 */
import { formatTime } from "../../format";
import type { GraphPanelProps } from "./GraphPanel";
import { FilePanelBody } from "./FilePanels";
import type { GraphLocal, LocalEdge, LocalGraph, LocalNode, TracePayload } from "./graphTypes";
import { Section, TASK_STATUS } from "./panelParts";
import { nodeDay } from "../links/TraceList";

interface LocalGraphPanelProps {
  panel: GraphPanelProps;
  payload: LocalGraph | TracePayload;
  selectedId: string | null;
  onSelect: (id: string | null) => void;
  onRecenter: (fileId: number) => void;
  onOpenLocal: (local: GraphLocal) => void;
  onExpandMeeting: (meetingId: string) => void;
  onOpenTask?: (taskId: string) => void;
  onClose: () => void;
}

function PlayAt({
  panel,
  audioUrl,
  atMs,
  label,
}: {
  panel: GraphPanelProps;
  audioUrl: string | null | undefined;
  atMs: number | null | undefined;
  label: string;
}) {
  if (!audioUrl || atMs === null || atMs === undefined) return null;
  return (
    <button
      aria-label={`从 ${formatTime(atMs, true)} 播放`}
      className="graph-play"
      onClick={() => panel.player.play(audioUrl, atMs, label)}
      type="button"
    >
      ▶ {formatTime(atMs, true)}
    </button>
  );
}

export function LocalGraphPanel({
  panel,
  payload,
  selectedId,
  onSelect,
  onRecenter,
  onOpenLocal,
  onExpandMeeting,
  onOpenTask,
  onClose,
}: LocalGraphPanelProps) {
  const center = payload.center;
  const nodes = new Map<string, LocalNode>([[center.id, center], ...payload.nodes.map((node) => [node.id, node] as const)]);
  const edge = selectedId ? payload.edges.find((item) => item.id === selectedId) : undefined;
  const node = selectedId ? nodes.get(selectedId) : undefined;
  const hidden = "hidden" in payload ? payload.hidden : [];
  const hiddenCount = "hidden_count" in payload ? payload.hidden_count : 0;
  const centerFileId = center.kind === "file" ? center.file_id ?? null : null;
  const target = node ?? (edge ? undefined : center);

  const trace = (id: string) => onOpenLocal({ kind: "trace", node: id });

  const edgeBody = (item: LocalEdge) => {
    const from = nodes.get(item.from);
    const meeting = item.meeting_id ? nodes.get(`m:${item.meeting_id}`) : undefined;
    const audio = meeting?.audio_url ?? from?.audio_url ?? null;
    const fileEnd = [item.from, item.to].map((id) => nodes.get(id)).find((end) => end?.kind === "file");
    return (
      <>
        <p className="graph-panel__meta">{item.label}</p>
        {(item.quote || item.at_ms !== undefined) && (
          <p className="graph-panel__quote">
            <PlayAt atMs={item.at_ms} audioUrl={audio} label={meeting?.title ?? ""} panel={panel} />
            {item.quote && `『${item.quote}』`}
          </p>
        )}
        {/* 在问的线下面是 4e 的问题（这条线的排第一） */}
        {item.state === "ask" && fileEnd?.file_id !== undefined && (
          <FilePanelBody fileId={fileEnd.file_id} fromMeetingId={null} key={item.id} props={panel} sortFirst={item.relation_id ?? null} />
        )}
      </>
    );
  };

  let title = "";
  let body = null;
  if (edge) {
    title = "为什么相连";
    body = edgeBody(edge);
  } else if (target) {
    switch (target.kind) {
      case "file":
        title = target.name ?? "";
        body = (
          <FilePanelBody
            fileId={target.file_id ?? 0}
            fromMeetingId={null}
            isCenter={target.file_id === centerFileId}
            key={target.id}
            props={panel}
          />
        );
        break;
      case "meeting": {
        title = target.title ?? "";
        const mention = payload.edges.find((item) => item.kind === "mentioned" && item.from === target.id);
        body = (
          <>
            <p className="graph-panel__meta">{target.caption ?? nodeDay(target.at)}</p>
            {mention && (
              <p className="graph-panel__quote">
                {mention.label}
                <PlayAt atMs={mention.at_ms} audioUrl={target.audio_url} label={target.title ?? ""} panel={panel} />
              </p>
            )}
            <div className="graph-panel__actions graph-panel__actions--start">
              <button className="ghost-button" onClick={() => panel.onOpenMeeting(target.meeting_id ?? target.id.slice(2))} type="button">
                打开会议页 →
              </button>
              <button className="ghost-button" onClick={() => onExpandMeeting(target.meeting_id ?? target.id.slice(2))} type="button">
                展开这场会
              </button>
              <button className="ghost-button" onClick={() => trace(target.id)} type="button">
                来龙去脉
              </button>
            </div>
          </>
        );
        break;
      }
      case "decision":
        title = "决议";
        body = (
          <>
            <p>{target.text}</p>
            <p className="graph-panel__meta">
              {target.meeting_caption ? `${target.meeting_caption} 定的` : "这场会定的"}
              <PlayAt atMs={target.start_ms} audioUrl={target.audio_url} label={target.meeting_caption ?? ""} panel={panel} />
            </p>
            <div className="graph-panel__actions graph-panel__actions--start">
              {target.meeting_id && (
                <button className="ghost-button" onClick={() => panel.onOpenMeeting(target.meeting_id!)} type="button">
                  打开会议页 →
                </button>
              )}
              <button className="ghost-button" onClick={() => trace(target.id)} type="button">
                来龙去脉
              </button>
            </div>
          </>
        );
        break;
      case "task":
        title = target.title ?? "";
        body = (
          <>
            <p className="graph-panel__meta">
              {TASK_STATUS[target.status ?? ""] ?? target.status}
              {target.meeting_caption && ` · ${target.meeting_caption}`}
              <PlayAt atMs={target.anchor_ms} audioUrl={target.audio_url} label={target.title ?? ""} panel={panel} />
            </p>
            <div className="graph-panel__actions graph-panel__actions--start">
              {onOpenTask && target.task_id && (
                <button className="ghost-button" onClick={() => onOpenTask(target.task_id!)} type="button">
                  打开任务
                </button>
              )}
              <button className="ghost-button" onClick={() => trace(target.id)} type="button">
                来龙去脉
              </button>
            </div>
          </>
        );
        break;
      case "requirement":
        title = target.title ?? "";
        body = (
          <button className="ghost-button" onClick={() => panel.onOpenRequirement(target.requirement_id ?? target.id.slice(2))} type="button">
            打开需求页 →
          </button>
        );
        break;
      default:
        body = null;
    }
  }

  const openHidden = (nodeId: string) => {
    const [prefix, value] = [nodeId.slice(0, nodeId.indexOf(":")), nodeId.slice(nodeId.indexOf(":") + 1)];
    if (prefix === "file") onRecenter(Number(value));
    else if (prefix === "m") panel.onOpenMeeting(value);
    else if (prefix === "task") onOpenTask?.(value);
    else if (prefix === "r") panel.onOpenRequirement(value);
  };

  return (
    <aside aria-label="详情面板" className="graph-panel">
      <header className="graph-panel__head">
        <div>
          <h2>{title}</h2>
        </div>
        <div className="graph-panel__nav">
          {selectedId && (
            <button className="text-button" onClick={() => onSelect(null)} type="button">
              ← 回到中心
            </button>
          )}
          <button aria-label="关闭面板" className="graph-panel__close" onClick={onClose} type="button">
            ✕
          </button>
        </div>
      </header>
      {panel.playerNode}
      <div className="graph-panel__body">
        {body}
        {!selectedId && hiddenCount > 0 && (
          <Section title={`还有 ${hiddenCount} 个没画出来`}>
            <ul className="graph-panel__list">
              {hidden.map((row) => (
                <li key={row.edge_id}>
                  <button className="text-button" onClick={() => openHidden(row.node_id)} type="button">
                    {row.node_label} · {row.label}
                  </button>
                </li>
              ))}
            </ul>
          </Section>
        )}
      </div>
    </aside>
  );
}
