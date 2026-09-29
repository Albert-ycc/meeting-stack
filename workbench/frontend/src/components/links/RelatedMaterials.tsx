import { useCallback, useEffect, useMemo, useRef, useState, type MouseEvent, type ReactNode } from "react";

import {
  ApiError,
  isOldBackend,
  type ApiClient,
  type RelatedItem,
  type RelatedMaterials as RelatedPayload,
  type RelatedRejected,
} from "../../api";
import type { PreviewTarget } from "../../types";
import { usePersistentState } from "../../viewState";
import type { NoticeAction, NoticeTone } from "../Notice";
import { itemsAt, nearestTimes, slotStart, clock, summaryLine } from "./relatedWindows";
import { OLD_BACKEND_TEXT, useRelationAnswer } from "./useRelationAnswer";
import "./related.css";

/*
 * 会议页的「相关材料」栏（4d）。电脑上是右栏最上面一节，手机上是标签页和逐字稿之间一条收起的横条。
 * 位置取 viewMs ?? currentMs（手机上只跟 currentMs），取覆盖这个时刻的两个窗，最多 3 条。
 * 不写分数、不写路径；每个状态一句话、最多一个按钮；waiting 时每 15 秒重取，页面隐藏时停。
 */

/** waiting 时的重取间隔，和项目页的 INDEX_POLL_MS 同一个节奏 */
export const RELATED_POLL_MS = 15_000;
export const FETCH_FAILED_TEXT = "相关材料没取到";

type RelatedApi = Partial<
  Pick<
    ApiClient,
    "relatedMaterials" | "relatedRejected" | "rejectRelatedMaterial" | "undoRelation" | "answerRelation" | "openMaterialFile"
  >
>;

export interface RelatedMaterialsProps {
  apiClient: RelatedApi;
  meetingId: string;
  /**
   * 这场会当前所属的项目（没归项目时 null）。相关材料只在项目文件夹里找，接口返回的
   * 内容跟着项目走；只当依赖项用来触发重取（见下面存疑 1 的说明），取数本身不用它。
   */
  projectId?: string | null;
  currentMs: number;
  /** 用户自己滚逐字稿时读到的那一行；null 时跟播放位置 */
  viewMs: number | null;
  isMobile: boolean;
  /** 会议页的 canWriteTasks（!isMobile || mobileTaskWrite） */
  canWrite: boolean;
  onSeek: (milliseconds: number) => void;
  onOpenPreview?: (target: PreviewTarget) => void;
  onOpenProject?: (projectId: string) => void;
  onNotice: (message: string, tone?: NoticeTone, actions?: NoticeAction[]) => void;
}

/** 共同词加亮（多个词一起） */
export function highlightWords(text: string, words: string[]): ReactNode {
  const needles = words.filter(Boolean).sort((a, b) => b.length - a.length);
  if (!needles.length) return text;
  const escaped = needles.map((word) => word.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"));
  const parts = text.split(new RegExp(`(${escaped.join("|")})`, "giu"));
  return parts.map((part, index) =>
    needles.some((word) => word.toLocaleLowerCase() === part.toLocaleLowerCase()) ? (
      <mark key={`${part}-${index}`}>{part}</mark>
    ) : (
      part
    ),
  );
}

function failureText(reason: unknown, fallback: string): [string, NoticeTone] {
  if (isOldBackend(reason)) return [OLD_BACKEND_TEXT, "warning"];
  if (reason instanceof ApiError && [404, 409, 415, 422, 403, 503].includes(reason.status)) return [reason.message, "warning"];
  return [reason instanceof Error && reason.message ? reason.message : fallback, "error"];
}

export function RelatedMaterials({
  apiClient,
  meetingId,
  projectId,
  currentMs,
  viewMs,
  isMobile,
  canWrite,
  onSeek,
  onOpenPreview,
  onOpenProject,
  onNotice,
}: RelatedMaterialsProps) {
  const [data, setData] = useState<RelatedPayload | null>(null);
  const [failed, setFailed] = useState(false);
  const [oldBackend, setOldBackend] = useState(false);
  const [hidden, setHidden] = useState<ReadonlySet<string>>(() => new Set());
  const [rejected, setRejected] = useState<RelatedRejected["items"] | null>(null);
  const [collapsed, setCollapsed] = usePersistentState("meeting.related.collapsed", false);
  const [mobileOpen, setMobileOpen] = usePersistentState("meeting.related.mobileOpen", false);
  const requestRef = useRef(0);

  const load = useCallback(async () => {
    if (typeof apiClient.relatedMaterials !== "function") return;
    const ticket = ++requestRef.current;
    try {
      const payload = await apiClient.relatedMaterials(meetingId);
      if (ticket !== requestRef.current) return;
      setData(payload);
      setFailed(false);
    } catch (reason) {
      if (ticket !== requestRef.current) return;
      if (isOldBackend(reason)) setOldBackend(true);
      else setFailed(true);
    }
  }, [apiClient, meetingId]);

  // 换会、或这场会改了归属项目：清掉旧的，重新取。
  // 存疑 1：改归属没让这里重取过，右栏会一直留着旧项目的材料统计——projectId 变了但
  // meetingId 没变，load 本身也不读 projectId，所以单靠 [load] 触发不了，要显式带上它。
  useEffect(() => {
    setData(null);
    setFailed(false);
    setOldBackend(false);
    setHidden(new Set());
    setRejected(null);
    void load();
    return () => {
      requestRef.current += 1;
    };
  }, [load, projectId]);

  // waiting 时每 15 秒重取；页面隐藏时不取，回来立刻取一次；ok、stopped、失败都停
  const waiting = !failed && data?.state.kind === "waiting";
  useEffect(() => {
    if (!waiting) return;
    const timer = window.setTimeout(() => {
      if (!document.hidden) void load();
    }, RELATED_POLL_MS);
    const onVisible = () => {
      if (!document.hidden) void load();
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      window.clearTimeout(timer);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [waiting, data, load]);

  const answering = useRelationAnswer({
    apiClient,
    scope: {},
    onNotice: (message, _undo, tone) => onNotice(message, tone ?? "success"),
    onChanged: load,
  });

  const position = isMobile ? currentMs : (viewMs ?? currentMs);
  const slot = slotStart(position);
  const windows = useMemo(
    () =>
      (data?.windows ?? []).map((window) => ({
        ...window,
        items: window.items.filter((item) => !hidden.has(item.content_key)),
      })),
    [data, hidden],
  );
  // 同一个 45 秒里移动时结果不变（按格子的开头取）
  const items = useMemo(() => itemsAt(windows, slot), [windows, slot]);
  const others = useMemo(() => (items.length ? [] : nearestTimes(windows, slot)), [items.length, windows, slot]);

  if (oldBackend || typeof apiClient.relatedMaterials !== "function") return null;

  const files = data?.files ?? {};

  const reject = async (item: RelatedItem) => {
    const file = files[item.content_key];
    if (!file || typeof apiClient.rejectRelatedMaterial !== "function") return;
    setHidden((current) => new Set(current).add(item.content_key));
    try {
      const result = await apiClient.rejectRelatedMaterial(meetingId, { content_key: item.content_key, file_id: file.file_id });
      const relationId = result.relation.id;
      onNotice(`已记下：『${file.name}』和这场会不相关`, "success", [
        {
          label: "撤销",
          onClick: () => {
            void answering.undo(relationId).then((done) => {
              if (done) {
                setHidden((current) => {
                  const next = new Set(current);
                  next.delete(item.content_key);
                  return next;
                });
              }
            });
          },
        },
      ]);
      void load();
    } catch (reason) {
      setHidden((current) => {
        const next = new Set(current);
        next.delete(item.content_key);
        return next;
      });
      const [text, tone] = failureText(reason, "操作失败");
      onNotice(text, tone);
    }
  };

  const openFile = async (fileId: number) => {
    if (typeof apiClient.openMaterialFile !== "function") return;
    try {
      await apiClient.openMaterialFile(fileId);
    } catch (reason) {
      const [text, tone] = failureText(reason, "打开文件失败");
      onNotice(text, tone);
    }
  };

  const showRejected = async () => {
    if (typeof apiClient.relatedRejected !== "function") return;
    try {
      setRejected((await apiClient.relatedRejected(meetingId)).items);
    } catch (reason) {
      const [text, tone] = failureText(reason, FETCH_FAILED_TEXT);
      onNotice(text, tone);
    }
  };

  const restore = async (relationId: number, name: string) => {
    if (typeof apiClient.answerRelation !== "function") return;
    try {
      await apiClient.answerRelation(relationId, { answer: "restore" });
      onNotice(`已改回相关：『${name}』`);
      setRejected((current) => current?.filter((row) => row.relation_id !== relationId) ?? null);
      void load();
    } catch (reason) {
      const [text, tone] = failureText(reason, "操作失败");
      onNotice(text, tone);
    }
  };

  const preview = (item: RelatedItem) => {
    const file = files[item.content_key];
    if (!file || !onOpenPreview) return;
    onOpenPreview({
      fileId: file.file_id,
      passage: { contentKey: item.content_key, ordinal: item.ordinal, from: "related", words: item.words },
    });
  };

  const summary = summaryLine(position, items.length);

  const stateLine = failed ? (
    <p className="related-state" role="status">
      {FETCH_FAILED_TEXT}
      <button className="text-button" onClick={() => void load()} type="button">
        重试
      </button>
    </p>
  ) : data?.state.text ? (
    <p className="related-state" role="status">
      {data.state.text}
      {data.state.action?.kind === "open_project" && onOpenProject && (
        <button className="text-button" onClick={() => onOpenProject(data.state.action!.project_id)} type="button">
          {data.state.action.label}
        </button>
      )}
    </p>
  ) : null;

  const renderItem = (item: RelatedItem) => {
    const file = files[item.content_key];
    if (!file) return null;
    const where = file.playable && item.start_ms !== null ? `录音 ${clock(item.start_ms)}` : (item.loc ?? "");
    const stop = (event: MouseEvent) => event.stopPropagation();
    return (
      <li className="related-item" key={item.content_key} onClick={() => preview(item)}>
        <button
          aria-label={`预览 ${file.name}${where ? ` ${where}` : ""}`}
          className="related-item__open"
          onClick={(event) => {
            stop(event);
            preview(item);
          }}
          type="button"
        >
          <span className="related-item__file">
            <span className="related-item__name">{file.name}</span>
            {where && <small>{where}</small>}
            {file.state_text && <small>{file.state_text}</small>}
          </span>
          <span className="related-item__text">{highlightWords(item.text, item.words)}</span>
          {item.words.length > 0 && <span className="related-item__words">共同词：{item.words.join("、")}</span>}
        </button>
        <span className="related-item__row">
          <button
            aria-label={`从 ${clock(item.at_ms)} 播放会上这段`}
            className="text-button related-item__play"
            onClick={(event) => {
              stop(event);
              onSeek(item.at_ms);
            }}
            type="button"
          >
            {clock(item.at_ms)} ▶
          </button>
          <span className="related-item__actions">
            {canWrite && typeof apiClient.rejectRelatedMaterial === "function" && (
              <button
                className="text-button"
                onClick={(event) => {
                  stop(event);
                  void reject(item);
                }}
                type="button"
              >
                不相关
              </button>
            )}
            {!isMobile && file.can_open === true && (
              <button
                className="text-button"
                onClick={(event) => {
                  stop(event);
                  void openFile(file.file_id);
                }}
                type="button"
              >
                用本机应用打开
              </button>
            )}
          </span>
        </span>
      </li>
    );
  };

  const body = (
    <>
      {stateLine}
      {data && data.copies.length > 0 && (
        <p className="related-copy">
          这场会的另一份记录：{data.copies.map((copy) => copy.name).join("、")}
          {onOpenPreview && (
            <button className="text-button" onClick={() => onOpenPreview({ fileId: data.copies[0].file_id })} type="button">
              预览
            </button>
          )}
        </p>
      )}
      {items.length > 0 ? (
        <ul className="related-list">{items.map(renderItem)}</ul>
      ) : data && windows.some((window) => window.items.length > 0) ? (
        <div className="related-empty">
          <p>这一段没找到相关材料</p>
          {others.length > 0 && (
            <p className="related-times">
              别的时间有：
              {others.map((start) => (
                <button className="related-time" key={start} onClick={() => onSeek(start)} type="button">
                  {clock(start)}
                </button>
              ))}
            </p>
          )}
        </div>
      ) : null}
      {data && data.rejected > 0 && (
        <div className="related-rejected">
          <p>
            有 {data.rejected} 份材料标过不相关
            {rejected === null && (
              <button className="text-button" onClick={() => void showRejected()} type="button">
                看看
              </button>
            )}
          </p>
          {rejected !== null && (
            <ul>
              {rejected.map((row) => (
                <li key={row.relation_id}>
                  <span>{row.name}</span>
                  {canWrite && (
                    <button className="text-button" onClick={() => void restore(row.relation_id, row.name)} type="button">
                      改回相关
                    </button>
                  )}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </>
  );

  if (isMobile) {
    return (
      <section className="related-bar" aria-label="相关材料">
        <button
          aria-expanded={mobileOpen}
          className="related-bar__toggle"
          onClick={() => setMobileOpen((open) => !open)}
          type="button"
        >
          {summary} {mobileOpen ? "▾" : "▸"}
        </button>
        {mobileOpen && <div className="related-bar__body">{body}</div>}
      </section>
    );
  }

  if (collapsed) {
    return (
      <div className="inspector-section related-materials related-materials--collapsed">
        <div className="related-head">
          <span className="related-head__summary">{summary}</span>
          <button className="text-button" onClick={() => setCollapsed(false)} type="button">
            展开
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className="inspector-section related-materials">
      <div className="related-head">
        <h2>
          相关材料 <small>{clock(slot)} 前后</small>
        </h2>
        <button className="text-button" onClick={() => setCollapsed(true)} type="button">
          收起
        </button>
      </div>
      <div className="related-materials__body">{body}</div>
    </div>
  );
}
