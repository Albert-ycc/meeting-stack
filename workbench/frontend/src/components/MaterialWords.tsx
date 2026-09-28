import { useEffect, useRef, useState } from "react";

import { ApiError, isOldBackend, type ApiClient } from "../api";
import { formatTime } from "../format";
import type { MaterialPair, MaterialWord } from "../types";
import { useLinksFlags } from "./links/LinksFlagsContext";
import { OLD_BACKEND_TEXT } from "./links/useRelationAnswer";
import "./MaterialWords.css";

/**
 * 4h：从材料里找到的词，三种样子：
 * - board：项目页「词典」块里，前 6 个和［还有 N 个］；
 * - page：词典页选中项目时，这个项目的全部待认词；
 * - meeting：会议页词典小节，最多 2 行这场会听错的写法。
 * 每项［记入 项目］［不是］，回答以后块里自己显示一行提示和［撤销］（到 undo_until 收起）。
 * 没有「正在挖」、没有分数；列表没回来或是空的整块不出；canWrite 为假、旧后台时也不出。
 */

export const MATERIAL_WORDS_BOARD_TITLE = "从材料里找到的词";
export const MATERIAL_WORDS_FOOTNOTE = "记入后会随纪要生成交给 AI 纠错，没记入的词只留在声档里。";
export const WORD_GONE_TEXT = "这个词已经不在了";
const NAME_LIMIT = 8;

export function pageTitle(projectName: string): string {
  return `从${projectName}的材料里找到的词`;
}

/** 按钮上的项目名超过 8 个字截断加「…」 */
export function shortName(name: string): string {
  const chars = Array.from(name);
  return chars.length > NAME_LIMIT ? `${chars.slice(0, NAME_LIMIT).join("")}…` : name;
}

export function acceptLabel(item: { existing_term?: { term: string } | null }, projectName: string): string {
  return item.existing_term ? `记到『${item.existing_term.term}』` : `记入 ${shortName(projectName)}`;
}

function monthDay(date: string): string {
  const [, month, day] = date.split("-");
  return month && day ? `${Number(month)}/${Number(day)}` : date;
}

function answerFailure(reason: unknown): string {
  if (isOldBackend(reason)) return OLD_BACKEND_TEXT;
  if (reason instanceof ApiError && reason.message) return reason.message;
  return reason instanceof Error && reason.message ? reason.message : "没办成，稍后再试";
}

interface Hint {
  text: string;
  /** 带［撤销］时：撤销哪一项、到什么时候 */
  undoKey?: string;
  until?: string;
}

interface MaterialWordsProps {
  apiClient: ApiClient;
  variant: "board" | "page" | "meeting";
  projectId: string;
  projectName: string;
  canWrite: boolean;
  /** board、page：待认的词；board 不是数组时（旧后台）整块不出 */
  items?: MaterialWord[] | null;
  /** board：总数，算［还有 N 个］ */
  total?: number;
  /** meeting：这场会听错的写法 */
  pairs?: MaterialPair[] | null;
  onOpenMeeting?: (meetingId: string, seekMs: number) => void;
  /** board：［还有 N 个］去词典页并选中这个项目 */
  onMore?: () => void;
  /** 回答或撤销以后（重载词条列表、重查会议的词典） */
  onAnswered?: () => void | Promise<void>;
}

export function MaterialWords({
  apiClient,
  variant,
  projectId,
  projectName,
  canWrite,
  items,
  total,
  pairs,
  onOpenMeeting,
  onMore,
  onAnswered,
}: MaterialWordsProps) {
  const flags = useLinksFlags();
  const [words, setWords] = useState<MaterialWord[]>(Array.isArray(items) ? items : []);
  const [rows, setRows] = useState<MaterialPair[]>(Array.isArray(pairs) ? pairs : []);
  const [removed, setRemoved] = useState<Record<string, string[]>>({});
  // 请求进行中的项（可以同时有几项）：这几项的按钮变灰，别的项照常能点
  const [busyKeys, setBusyKeys] = useState<ReadonlySet<string>>(() => new Set());
  const [hint, setHint] = useState<Hint | null>(null);
  const timer = useRef<number | null>(null);

  useEffect(() => setWords(Array.isArray(items) ? items : []), [items]);
  useEffect(() => setRows(Array.isArray(pairs) ? pairs : []), [pairs]);
  // 过了撤销期，提示连同［撤销］一起收起
  useEffect(() => {
    if (!hint?.until) return;
    const left = Date.parse(hint.until) - Date.now();
    timer.current = window.setTimeout(() => setHint(null), Math.max(0, left));
    return () => {
      if (timer.current !== null) window.clearTimeout(timer.current);
    };
  }, [hint]);

  const methodsReady =
    typeof apiClient.acceptGlossaryCandidate === "function" &&
    typeof apiClient.rejectGlossaryCandidate === "function" &&
    typeof apiClient.undoGlossaryCandidate === "function";
  const source = variant === "meeting" ? pairs : items;
  if (!canWrite || !flags || !methodsReady || !Array.isArray(source)) return null;
  const count = variant === "meeting" ? rows.length : words.length;
  if (count === 0 && !hint) return null;

  const isBusy = (key: string) => busyKeys.has(key);
  const markBusy = (key: string, on: boolean) =>
    setBusyKeys((current) => {
      const next = new Set(current);
      if (on) next.add(key);
      else next.delete(key);
      return next;
    });

  /** 拿掉一项；会议页记入只记了一个写法时（wrong），只拿掉这一行 */
  const drop = (key: string, wrong?: string) => {
    setWords((current) => current.filter((item) => item.key !== key));
    setRows((current) => current.filter((row) => row.key !== key || (wrong !== undefined && row.wrong !== wrong)));
  };

  const answer = async (key: string, work: () => Promise<Hint>, wrong?: string) => {
    markBusy(key, true);
    try {
      const next = await work();
      drop(key, wrong);
      setHint(next);
      await onAnswered?.();
    } catch (reason) {
      if (reason instanceof ApiError && reason.status === 404 && !isOldBackend(reason)) {
        drop(key);
        setHint({ text: WORD_GONE_TEXT });
      } else {
        setHint({ text: answerFailure(reason) });
      }
    } finally {
      markBusy(key, false);
    }
  };

  const accept = (key: string, onlyWrong?: string) =>
    void answer(
      key,
      async () => {
        const body =
          onlyWrong === undefined ? { key, not_wrong: removed[key] ?? [] } : { key, only_wrong: onlyWrong };
        const result = await apiClient.acceptGlossaryCandidate(projectId, body);
        return result.already ? { text: result.text } : { text: result.text, undoKey: key, until: result.undo_until };
      },
      onlyWrong,
    );

  const reject = (key: string) =>
    void answer(key, async () => {
      const result = await apiClient.rejectGlossaryCandidate(projectId, { key });
      return { text: result.text, undoKey: key, until: result.undo_until };
    });

  const undo = (key: string) =>
    void (async () => {
      markBusy(key, true);
      try {
        const result = await apiClient.undoGlossaryCandidate(projectId, { key });
        setHint({ text: result.text });
        await onAnswered?.();
      } catch (reason) {
        setHint({ text: answerFailure(reason) });
      } finally {
        markBusy(key, false);
      }
    })();

  const toggleWrong = (key: string, wrong: string) =>
    setRemoved((current) => ({ ...current, [key]: [...(current[key] ?? []), wrong] }));

  const openAt = (meetingId: string, ms: number) => onOpenMeeting?.(meetingId, ms);

  /** blocked：记到已有词条、写法却全被去掉时，［记到『X』］没东西可记，置灰 */
  const buttons = (key: string, label: string, options: { onlyWrong?: string; blocked?: boolean } = {}) => (
    <span className="material-words__buttons">
      <button
        className="ghost-button"
        disabled={isBusy(key) || Boolean(options.blocked)}
        onClick={() => accept(key, options.onlyWrong)}
        type="button"
      >
        {label}
      </button>
      <button className="ghost-button" disabled={isBusy(key)} onClick={() => reject(key)} type="button">
        不是
      </button>
    </span>
  );

  const hintLine = hint && (
    <p className="material-words__hint" role="status">
      <span>{hint.text}</span>
      {hint.undoKey && (
        <button
          className="text-button"
          disabled={isBusy(hint.undoKey)}
          onClick={() => undo(hint.undoKey!)}
          type="button"
        >
          撤销
        </button>
      )}
    </p>
  );

  if (variant === "meeting") {
    return (
      <div className="material-words material-words--meeting">
        {rows.map((row) => (
          <p className="material-words__pair" key={`${row.key}+${row.wrong}`}>
            <span>
              材料里写作『{row.term}』，这场会听成了『{row.wrong}』
            </span>
            {buttons(row.key, acceptLabel({}, projectName), { onlyWrong: row.wrong })}
          </p>
        ))}
        {hintLine}
      </div>
    );
  }

  const shown = Array.isArray(items) ? items.length : 0;
  const hidden = variant === "board" ? Math.max(0, (total ?? shown) - shown) : 0;
  return (
    <section
      aria-label={variant === "board" ? MATERIAL_WORDS_BOARD_TITLE : pageTitle(projectName)}
      className={`material-words material-words--${variant}`}
    >
      <strong className="material-words__title">
        {variant === "board" ? MATERIAL_WORDS_BOARD_TITLE : pageTitle(projectName)}
      </strong>
      <ul className="material-words__list">
        {words.map((item) => {
          const gone = removed[item.key] ?? [];
          const wrongs = item.wrongs.filter((wrong) => !gone.includes(wrong.text));
          const quoteFile = item.file_quote
            ? item.file_names.find((entry) => entry.file_id === item.file_quote?.file_id)
            : undefined;
          return (
            <li key={item.key}>
              <p className="material-words__head">
                <strong>{item.term}</strong>
                {buttons(item.key, acceptLabel(item, projectName), {
                  blocked: Boolean(item.existing_term) && item.wrongs.length > 0 && wrongs.length === 0,
                })}
              </p>
              {wrongs.length > 0 && (
                <p className="material-words__wrongs">
                  <span>会上可能听成了：</span>
                  {wrongs.map((wrong) => (
                    <span className="material-words__chip" key={wrong.text} title={`${wrong.meetings} 场会里听到`}>
                      『{wrong.text}』
                      <button
                        aria-label={`不是听错：${wrong.text}`}
                        disabled={isBusy(item.key)}
                        onClick={() => toggleWrong(item.key, wrong.text)}
                        type="button"
                      >
                        ×
                      </button>
                    </span>
                  ))}
                </p>
              )}
              {item.heard.map((heard, index) => (
                <p className="material-words__heard" key={`${heard.meeting.id}-${heard.start_ms}`}>
                  {index === 0 && item.spoken > 0 && item.wrongs.length === 0 && `会上说过 ${item.spoken} 次 · `}
                  『{heard.quote}』 · {heard.meeting.title} {monthDay(heard.meeting.date)}{" "}
                  <button
                    className="text-button"
                    onClick={() => openAt(heard.meeting.id, heard.start_ms)}
                    type="button"
                  >
                    {formatTime(heard.start_ms, true)}
                  </button>
                </p>
              ))}
              {item.file_quote && quoteFile ? (
                <p className="material-words__files">
                  『…{item.file_quote.quote}…』 · {quoteFile.name}
                </p>
              ) : (
                item.files > 0 && (
                  <p className="material-words__files">
                    在 {item.files} 个文件里
                    {item.file_names.length > 0 && `：${item.file_names.map((entry) => entry.name).join("、")}`}
                    {item.files > 2 && item.file_names.length > 0 && " 等"}
                  </p>
                )
              )}
            </li>
          );
        })}
      </ul>
      {hintLine}
      {hidden > 0 && onMore && (
        <button className="text-button" onClick={onMore} type="button">
          还有 {hidden} 个
        </button>
      )}
      <p className="material-words__foot">{MATERIAL_WORDS_FOOTNOTE}</p>
    </section>
  );
}
