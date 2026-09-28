import type { ApiClient, RelationAnswer, RelationKind, RelationQuestion as Question } from "../../api";
import { formatTime } from "../../format";
import type { NoticeFn } from "../graph/panelParts";
import { useLinksFlags } from "./LinksFlagsContext";
import { useRelationAnswer, type RecentAnswer, type RelationScope } from "./useRelationAnswer";
import "./links.css";

/** 按钮按 answers 映射：yes、no 是［是］［不是］，updated、no 是［已更新］［不相关］；pick、restore 不在问题块上 */
const NO_LABEL: Record<RelationKind, string> = {
  produced: "不是",
  affects: "不相关",
  related: "不相关",
  mention: "不是这份文件",
  later_changed: "不是一回事",
  restated: "不是一回事",
};

function answerLabel(kind: RelationKind, answer: RelationAnswer): string | null {
  if (answer === "yes") return "是";
  if (answer === "updated") return "已更新";
  if (answer === "no") return NO_LABEL[kind] ?? "不相关";
  return null;
}

type PlayFn = (audioUrl: string, atMs: number, label: string) => void;

interface RelationQuestionBlockProps {
  question: Question;
  canWrite: boolean;
  /** 这一块正在发送 */
  sending: boolean;
  /** 别的块在发送时也变灰，一次只发一个 */
  disabled?: boolean;
  onAnswer: (answer: RelationAnswer) => void;
  onPlay?: PlayFn;
}

/** 一个待回答的问题：左边琥珀色竖线；第一行 text（有录音带 ▶），第二行材料位置加片段或 ask */
export function RelationQuestionBlock({
  question,
  canWrite,
  sending,
  disabled = false,
  onAnswer,
  onPlay,
}: RelationQuestionBlockProps) {
  const decision = question.decision;
  const playable = Boolean(onPlay && decision?.audio_url && decision.start_ms !== null && decision.start_ms !== undefined);
  const buttons = question.answers.flatMap((answer) => {
    const label = answerLabel(question.kind, answer);
    return label ? [{ answer, label }] : [];
  });
  return (
    <div aria-busy={sending || undefined} aria-label={question.text} className="relation-question" role="group">
      <div className="relation-question__body">
        <div className="relation-question__lines">
          <p className="relation-question__text">
            {question.text}
            {playable && decision && (
              <button
                aria-label={`从 ${formatTime(decision.start_ms)} 播放`}
                className="relation-question__play"
                onClick={() => onPlay?.(decision.audio_url ?? "", decision.start_ms ?? 0, decision.meeting_title)}
                type="button"
              >
                ▶
              </button>
            )}
          </p>
          {question.passage ? (
            <p className="relation-question__passage">
              {question.passage.loc && `${question.passage.loc}：`}『{question.passage.text}』
            </p>
          ) : (
            question.ask && <p className="relation-question__ask">{question.ask}</p>
          )}
        </div>
        {canWrite && buttons.length > 0 && (
          <div className="relation-question__actions">
            {buttons.map((button) => (
              <button
                className="ghost-button"
                disabled={sending || disabled}
                key={button.answer}
                onClick={() => onAnswer(button.answer)}
                type="button"
              >
                {button.label}
              </button>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

/** 回答以后收成的一行灰字：那句提示加［撤销］，留到 undo_until */
export function RecentAnswerLine({
  entry,
  canWrite,
  disabled,
  onUndo,
}: {
  entry: RecentAnswer;
  canWrite: boolean;
  disabled: boolean;
  onUndo: () => void;
}) {
  return (
    <p className="relation-recent">
      <span>{entry.text}</span>
      {canWrite && (
        <button className="text-button" disabled={disabled} onClick={onUndo} type="button">
          撤销
        </button>
      )}
    </p>
  );
}

interface RelationQuestionProps {
  apiClient: Partial<Pick<ApiClient, "answerRelation" | "undoRelation">>;
  /** 宿主数据里的 questions；不是数组（旧后台）时整块不画 */
  questions: Question[] | null | undefined;
  scope: RelationScope;
  canWrite: boolean;
  onNotice: NoticeFn;
  /** 回答、撤销以后宿主重取 */
  onChanged?: () => void | Promise<void>;
  onPlay?: PlayFn;
}

/**
 * 宿主（文件面板、预览抽屉、任务抽屉、需求卡）用的问题块。回答以后这一块收成一行，
 * 宿主重取、问题从数据里没了以后，这一行从 recent 里接着画，直到 undo_until。
 * 旧后台（useLinksFlags 为 null、没有 answerRelation、回答撞上旧后台）时整块不画。
 */
export function RelationQuestion({ apiClient, questions, scope, canWrite, onNotice, onChanged, onPlay }: RelationQuestionProps) {
  const flags = useLinksFlags();
  const answering = useRelationAnswer({ apiClient, scope, onNotice, onChanged });
  if (!flags || typeof apiClient.answerRelation !== "function" || !Array.isArray(questions) || answering.oldBackend) {
    return null;
  }
  const recentById = new Map(answering.recent.map((entry) => [entry.relationId, entry]));
  const listed = new Set(questions.map((question) => question.relation_id));
  const leftover = answering.recent.filter((entry) => !listed.has(entry.relationId));
  const rows = questions.filter((question) => !answering.gone.has(question.relation_id));
  if (!rows.length && !leftover.length) return null;
  const undoRow = (entry: RecentAnswer) => (
    <RecentAnswerLine
      canWrite={canWrite}
      disabled={answering.sending !== null}
      entry={entry}
      key={`recent-${entry.relationId}`}
      onUndo={() => void answering.undo(entry.relationId)}
    />
  );
  return (
    <div className="relation-questions">
      {rows.map((question) => {
        const recent = recentById.get(question.relation_id);
        if (recent) return undoRow(recent);
        return (
          <RelationQuestionBlock
            canWrite={canWrite}
            disabled={answering.sending !== null}
            key={question.relation_id}
            onAnswer={(answer) => void answering.answer(question, answer)}
            onPlay={onPlay}
            question={question}
            sending={answering.sending === question.relation_id}
          />
        );
      })}
      {leftover.map(undoRow)}
    </div>
  );
}
