import type { ApiClient, RelationAnswer, RelationKind, RelationQuestion as Question } from "../../api";
import { formatTime } from "../../format";
import type { NoticeFn } from "../graph/panelParts";
import { useLinksFlags } from "./LinksFlagsContext";
import { inScope, useRelationAnswer, type RecentAnswer, type RelationAnswering, type RelationScope } from "./useRelationAnswer";
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
  /** 4e：产出的问法换成这一句（任务抽屉里是「是这条任务的交付物吗？」） */
  ask?: string;
  /** 4e：文件名（影响是第一行那句）点了打开这份文件（预览抽屉） */
  onOpenFile?: (fileId: number) => void;
  /** 4e：只画第一行（需求卡：决议本身就是那一行，不再列片段） */
  compact?: boolean;
}

/**
 * 一个待回答的问题：左边琥珀色竖线。可能过时：第一行 text（有录音带 ▶），第二行材料位置加片段；
 * 产出（4e）：第一行是问法「是任务『…』的交付物吗？」，第二行是（文件名和）证据。
 */
export function RelationQuestionBlock({
  question,
  canWrite,
  sending,
  disabled = false,
  onAnswer,
  onPlay,
  ask,
  onOpenFile,
  compact = false,
}: RelationQuestionBlockProps) {
  const decision = question.decision;
  const playable = Boolean(onPlay && decision?.audio_url && decision.start_ms !== null && decision.start_ms !== undefined);
  const buttons = question.answers.flatMap((answer) => {
    const label = answerLabel(question.kind, answer);
    return label ? [{ answer, label }] : [];
  });
  const openFile = onOpenFile ? () => onOpenFile(question.file.id) : undefined;
  const asking = question.kind === "produced" ? ask ?? question.ask ?? null : null;
  return (
    <div aria-busy={sending || undefined} aria-label={question.text} className="relation-question" role="group">
      <div className="relation-question__body">
        {asking ? (
          <div className="relation-question__lines">
            <p className="relation-question__text">{asking}</p>
            {!compact && (
              <p className="relation-question__ask">
                {openFile && (
                  <>
                    <button className="relation-question__file" onClick={openFile} type="button">
                      {question.file.name}
                    </button>
                    {" · "}
                  </>
                )}
                {question.text}
              </p>
            )}
          </div>
        ) : (
        <div className="relation-question__lines">
          <p className="relation-question__text">
            {openFile ? (
              <button className="relation-question__file" onClick={openFile} type="button">
                {question.text}
              </button>
            ) : (
              question.text
            )}
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
          {compact ? null : question.passage ? (
            <p className="relation-question__passage">
              {question.passage.loc && `${question.passage.loc}：`}『{question.passage.text}』
            </p>
          ) : (
            question.ask && <p className="relation-question__ask">{question.ask}</p>
          )}
        </div>
        )}
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
  /** 宿主已经有一个 useRelationAnswer 时传进来（提示里的［撤销］和收成的那一行走同一个） */
  answering?: RelationAnswering;
  /** 4e：产出的问法换成这一句（任务抽屉） */
  ask?: string;
  /** 4e：文件名点了打开预览抽屉（任务抽屉、需求卡） */
  onOpenFile?: (fileId: number) => void;
  /** 4e：只画第一行（需求卡） */
  compact?: boolean;
}

/**
 * 宿主（文件面板、预览抽屉、任务抽屉、需求卡）用的问题块。回答以后这一块收成一行，
 * 宿主重取、问题从数据里没了以后，这一行从 recent 里接着画，直到 undo_until。
 * 旧后台（useLinksFlags 为 null、没有 answerRelation、回答撞上旧后台）时整块不画。
 */
export function RelationQuestion({
  apiClient,
  questions,
  scope,
  canWrite,
  onNotice,
  onChanged,
  onPlay,
  answering: shared,
  ask,
  onOpenFile,
  compact,
}: RelationQuestionProps) {
  const flags = useLinksFlags();
  const own = useRelationAnswer({ apiClient, scope, onNotice, onChanged });
  const answering = shared ?? own;
  if (!flags || typeof apiClient.answerRelation !== "function" || !Array.isArray(questions) || answering.oldBackend) {
    return null;
  }
  // 共用的 useRelationAnswer 也记着别的块（相关、决议标记、需求卡里别的决议）收成的行：这里只接自己范围里
  // 产出和可能过时的
  const mine = answering.recent.filter(
    (entry) => (entry.kind === "produced" || entry.kind === "affects") && inScope(entry, scope),
  );
  const recentById = new Map(mine.map((entry) => [entry.relationId, entry]));
  const listed = new Set(questions.map((question) => question.relation_id));
  const leftover = mine.filter((entry) => !listed.has(entry.relationId));
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
            ask={ask}
            canWrite={canWrite}
            compact={compact}
            disabled={answering.sending !== null}
            key={question.relation_id}
            onAnswer={(answer) => void answering.answer(question, answer)}
            onOpenFile={onOpenFile}
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
