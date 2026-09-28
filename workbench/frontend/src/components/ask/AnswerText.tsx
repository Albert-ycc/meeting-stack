import type { AskSource } from "../../types";
import { CitationChip, type ChipHandlers } from "./CitationChip";

/*
 * 回答按纯文本画（4g）：按 [D1]、[T2] 这样的标记切开，文字是 React 文本节点（white-space: pre-wrap），
 * 标记换成出处小块；认不出的标记照原样当文字。从不用 dangerouslySetInnerHTML，从不出 <a>，不渲染 Markdown：
 * 回答里的网址和 **粗** 都原样显示。
 */

const MARK = /\[([DNTM]\d{1,2})\]/g;

export type AnswerPiece = { kind: "text"; text: string } | { kind: "cite"; id: string };

/** 切成文字和标记（相邻的标记、开头的标记都各成一块） */
export function splitAnswer(text: string): AnswerPiece[] {
  const pieces: AnswerPiece[] = [];
  let last = 0;
  for (const match of text.matchAll(MARK)) {
    const at = match.index ?? 0;
    if (at > last) pieces.push({ kind: "text", text: text.slice(last, at) });
    pieces.push({ kind: "cite", id: match[1] });
    last = at + match[0].length;
  }
  if (last < text.length) pieces.push({ kind: "text", text: text.slice(last) });
  return pieces;
}

export function AnswerText({
  text,
  sources,
  handlers,
}: {
  text: string;
  sources: AskSource[];
  handlers: ChipHandlers;
}) {
  const byId = new Map(sources.map((source) => [source.id, source]));
  return (
    <p className="ask-answer__text">
      {splitAnswer(text).map((piece, index) => {
        if (piece.kind === "text") return <span key={index}>{piece.text}</span>;
        const source = byId.get(piece.id);
        if (!source) return <span key={index}>{`[${piece.id}]`}</span>;
        return <CitationChip handlers={handlers} key={index} source={source} />;
      })}
    </p>
  );
}
