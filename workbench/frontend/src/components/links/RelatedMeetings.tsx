import type { ApiClient, RelationQuestion as Question } from "../../api";
import { formatTime } from "../../format";
import type { RelatedMeeting } from "../../types";
import type { NoticeFn } from "../graph/panelParts";
import { useLinksFlags } from "./LinksFlagsContext";
import "./related.css";
import { RecentAnswerLine } from "./RelationQuestion";
import { useRelationAnswer, type RelationAnswering } from "./useRelationAnswer";

/*
 * 文件面板和预览抽屉的「内容相关的会」（4d），最多 5 条：会名和日期、「共同词：…」、▶ 原话、［不相关］。
 * 这里的行有 relation_id，［不相关］走 4a 的 answerRelation(id, {answer: "no"})；回答以后这一行收成
 * 灰字撤销行（useRelationAnswer 的 recent），留到 undo_until。不算进「在 N 场会上被提到」的 N。
 */

export const RELATED_MEETINGS_TITLE = "内容相关的会";

/** 「9月21日」 */
export function monthDay(date: string): string {
  const match = /^(\d{4})-(\d{2})-(\d{2})/.exec(date);
  if (!match) return date;
  return `${Number(match[2])}月${Number(match[3])}日`;
}

interface RelatedMeetingsProps {
  apiClient: Partial<Pick<ApiClient, "answerRelation" | "undoRelation">>;
  fileId: number;
  fileName: string;
  rows: RelatedMeeting[] | undefined;
  canWrite: boolean;
  onNotice: NoticeFn;
  onChanged?: () => void | Promise<void>;
  onOpenMeeting: (meetingId: string, atMs?: number) => void;
  onPlay?: (audioUrl: string, atMs: number, label: string) => void;
  /** 宿主已经有一个 useRelationAnswer 时传进来（撤销和提示走同一个） */
  answering?: RelationAnswering;
}

export function RelatedMeetings({
  apiClient,
  fileId,
  fileName,
  rows,
  canWrite,
  onNotice,
  onChanged,
  onOpenMeeting,
  onPlay,
  answering: shared,
}: RelatedMeetingsProps) {
  const flags = useLinksFlags();
  const own = useRelationAnswer({ apiClient, scope: { fileId }, onNotice, onChanged });
  const answering = shared ?? own;
  if (!Array.isArray(rows) || flags === null) return null;
  const recent = answering.recent.filter((entry) => entry.kind === "related" && entry.fileId === fileId);
  const answered = new Set(recent.map((entry) => entry.relationId));
  const visible = rows.filter((row) => !answered.has(row.relation_id) && !answering.gone.has(row.relation_id));
  if (!visible.length && !recent.length) return null;
  const canAnswer = canWrite && typeof apiClient.answerRelation === "function" && !answering.oldBackend;

  const reject = (row: RelatedMeeting) => {
    const question = {
      relation_id: row.relation_id,
      kind: "related",
      file: { id: fileId, name: fileName, folder: "" },
      task: null,
      decision: null,
    } as unknown as Question;
    void answering.answer(question, "no");
  };

  return (
    <section className="related-meetings">
      <h3>{RELATED_MEETINGS_TITLE}</h3>
      <ul>
        {visible.map((row) => (
          <li key={row.relation_id}>
            <button className="text-button" onClick={() => onOpenMeeting(row.meeting_id, row.at_ms ?? undefined)} type="button">
              {row.title} · {monthDay(row.date)}
            </button>
            {row.words.length > 0 && <small>共同词：{row.words.join("、")}</small>}
            {row.quote && (
              <span className="related-meetings__quote">
                {onPlay && row.audio_url && row.at_ms !== null && (
                  <button
                    aria-label={`从 ${formatTime(row.at_ms)} 播放原话`}
                    className="text-button"
                    onClick={() => onPlay(row.audio_url!, row.at_ms ?? 0, row.title)}
                    type="button"
                  >
                    ▶
                  </button>
                )}
                {row.quote}
              </span>
            )}
            {canAnswer && (
              <button
                className="text-button"
                disabled={answering.sending !== null}
                onClick={() => reject(row)}
                type="button"
              >
                不相关
              </button>
            )}
          </li>
        ))}
      </ul>
      {recent.map((entry) => (
        <RecentAnswerLine
          canWrite={canWrite}
          disabled={answering.sending !== null}
          entry={entry}
          key={entry.relationId}
          onUndo={() => void answering.undo(entry.relationId)}
        />
      ))}
    </section>
  );
}
