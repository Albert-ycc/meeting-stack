import type { RelationKind, RelationQuestion } from "../../api";

/**
 * 决议标记（后来改了、后来又提到）回答时交给 useRelationAnswer 的问题：没有文件这一端，
 * 按决议 id 记收成的那一行（file.id 用 -1，不会对上任何文件面板）。
 */
export function markQuestion(
  relationId: number,
  kind: Extract<RelationKind, "later_changed" | "restated">,
  decisionId: string,
): RelationQuestion {
  return {
    relation_id: relationId,
    kind,
    text: "",
    file: { id: -1, name: "" },
    answers: ["no"],
    decision: {
      id: decisionId,
      text: "",
      date: "",
      meeting_id: "",
      meeting_title: "",
      start_ms: null,
      audio_url: null,
    },
  };
}
