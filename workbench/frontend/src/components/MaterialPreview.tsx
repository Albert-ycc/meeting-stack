import { useCallback, useEffect, useRef, useState } from "react";

import { materialMediaUrl, type ApiClient } from "../api";
import { copyText } from "../clipboard";
import { formatBytes, formatDate, formatTime } from "../format";
import type { MaterialFilePreview, MaterialPreviewContent } from "../types";
import { AsyncState } from "./AsyncState";
import type { MiniPlayerHandle, PlayOptions } from "./graph/MiniPlayer";
import { claimSound } from "./soundFocus";
import { useDialogFocus } from "./useDialog";
import "./MaterialPreview.css";

/** 缩略图、PDF 第一页读不出来（盘拔了、生成超时）时换成这一行灰字，不显示破图 */
export const PICTURE_FALLBACK = "资料盘未连接，先看上次读到的";
export const UNPLAYABLE_TEXT = "这种格式浏览器放不了，在访达里打开";

function Picture({ src, alt }: { src: string; alt: string }) {
  const [failed, setFailed] = useState(false);
  useEffect(() => setFailed(false), [src]);
  if (failed) return <p className="material-preview__muted">{PICTURE_FALLBACK}</p>;
  return <img alt={alt} className="material-preview__picture" onError={() => setFailed(true)} src={src} />;
}

function Lines({ lines, more }: { lines: string[]; more: boolean }) {
  if (lines.length === 0) return null;
  return (
    <>
      <pre className="material-preview__lines">{lines.join("\n")}</pre>
      {more && <p className="material-preview__muted">……只显示前 40 行</p>}
    </>
  );
}

interface PreviewBlockProps {
  data: Pick<MaterialFilePreview, "file" | "state" | "preview">;
  player: MiniPlayerHandle;
  onOpenMeeting?: (meetingId: string) => void;
}

/** 状态那一句加预览。预览抽屉和关系图的文件面板共用。 */
export function PreviewBlock({ data, player, onOpenMeeting }: PreviewBlockProps) {
  const { file, state, preview } = data;
  return (
    <div className="material-preview__block">
      {state.text && (
        <p className="material-preview__state">
          {state.text}
          {state.meeting && onOpenMeeting && (
            <button
              className="material-preview__inline-button"
              onClick={() => onOpenMeeting(state.meeting!.id)}
              type="button"
            >
              打开会议
            </button>
          )}
        </p>
      )}
      <PreviewBody file={file} player={player} preview={preview} />
    </div>
  );
}

function PreviewBody({
  file,
  preview,
  player,
}: {
  file: MaterialFilePreview["file"];
  preview: MaterialPreviewContent;
  player: MiniPlayerHandle;
}) {
  switch (preview.kind) {
    case "text":
      return <Lines lines={preview.lines} more={preview.more} />;
    case "table":
      if (preview.rows.length === 0) return null;
      return (
        <div className="material-preview__table-wrap">
          {preview.sheet && <p className="material-preview__caption">{preview.sheet}</p>}
          <div className="material-preview__table-scroll">
            <table className="material-preview__table">
              <tbody>
                {preview.rows.map((row, rowIndex) => (
                  <tr key={rowIndex}>
                    {row.map((cell, cellIndex) => (
                      <td key={cellIndex}>{cell}</td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {preview.more && <p className="material-preview__muted">……只显示前 5 行</p>}
        </div>
      );
    case "image":
      return (
        <>
          {preview.image_url ? (
            <Picture alt={file.name} src={preview.image_url} />
          ) : (
            !file.gone && !file.root_online && <p className="material-preview__muted">{PICTURE_FALLBACK}</p>
          )}
          <Lines lines={preview.lines} more={preview.more} />
        </>
      );
    case "pdf":
      return (
        <>
          {preview.page_url ? (
            <Picture alt={`${file.name} 第一页`} src={preview.page_url} />
          ) : (
            !file.gone && !file.root_online && <p className="material-preview__muted">{PICTURE_FALLBACK}</p>
          )}
          <Lines lines={preview.lines} more={preview.more} />
        </>
      );
    case "media": {
      const url = preview.media_url;
      return (
        <>
          {!preview.playable && <p className="material-preview__muted">{UNPLAYABLE_TEXT}</p>}
          {preview.playable && url && (
            <button
              className="material-preview__play-all"
              onClick={() => player.play(url, 0, file.name, { clip: false })}
              type="button"
            >
              ▶ 从头放{preview.duration_ms ? `（${formatTime(preview.duration_ms)}）` : ""}
            </button>
          )}
          {preview.transcript.length > 0 && (
            <ol className="material-preview__transcript">
              {preview.transcript.map((item, index) => (
                <li key={index}>
                  {url && item.start_ms !== null ? (
                    <button
                      aria-label={`从 ${formatTime(item.start_ms)} 放`}
                      className="material-preview__seek"
                      onClick={() => player.play(url, item.start_ms ?? 0, file.name, { clip: false })}
                      type="button"
                    >
                      ▶ {formatTime(item.start_ms)}
                    </button>
                  ) : (
                    item.start_ms !== null && <span className="material-preview__time">{formatTime(item.start_ms)}</span>
                  )}
                  <span>{item.text}</span>
                </li>
              ))}
            </ol>
          )}
        </>
      );
    }
    default:
      return null;
  }
}

/**
 * 抽屉自带的播放器：接口和关系图的迷你播放器一样，只挂一个 <audio>。blocked 为真时（手机上浏览器拦了
 * 自动播放）显示「▶ 从 01:32 放」让你再点一下。
 */
export function useDrawerPlayer() {
  const audioRef = useRef<HTMLAudioElement>(null);
  const stopAtRef = useRef(Number.POSITIVE_INFINITY);
  const pendingRef = useRef<(() => void) | null>(null);
  const [label, setLabel] = useState("");
  const [playing, setPlaying] = useState(false);
  const [positionMs, setPositionMs] = useState(0);
  const [blocked, setBlocked] = useState<{ url: string; atMs: number; label: string } | null>(null);

  const play = useCallback((url: string, atMs: number, nextLabel: string, options?: PlayOptions) => {
    const clipped = options?.clip === true;
    const start = clipped ? Math.max(0, atMs - 3_000) : Math.max(0, atMs);
    stopAtRef.current = clipped ? atMs + 15_000 : Number.POSITIVE_INFINITY;
    setLabel(nextLabel);
    setPositionMs(start);
    setBlocked(null);
    const audio = audioRef.current;
    if (!audio) return;
    const begin = () => {
      if (pendingRef.current === begin) pendingRef.current = null;
      audio.currentTime = start / 1000;
      const attempt = audio.play?.();
      attempt?.catch?.(() => {
        setPlaying(false);
        setBlocked({ url, atMs: start, label: nextLabel });
      });
    };
    // 换地址还没加载完又点了一下：只留最后一次的起点
    if (pendingRef.current) audio.removeEventListener("loadedmetadata", pendingRef.current);
    pendingRef.current = null;
    if (audio.getAttribute("src") !== url) {
      pendingRef.current = begin;
      audio.setAttribute("src", url);
      audio.addEventListener("loadedmetadata", begin, { once: true });
      audio.load?.();
    } else {
      begin();
    }
  }, []);

  useEffect(() => {
    const audio = audioRef.current;
    if (!audio) return;
    const onTime = () => {
      const now = audio.currentTime * 1000;
      setPositionMs(now);
      if (now >= stopAtRef.current) audio.pause();
    };
    const onPlay = () => {
      claimSound(audio);
      setPlaying(true);
    };
    const onPause = () => setPlaying(false);
    audio.addEventListener("timeupdate", onTime);
    audio.addEventListener("play", onPlay);
    audio.addEventListener("pause", onPause);
    return () => {
      audio.removeEventListener("timeupdate", onTime);
      audio.removeEventListener("play", onPlay);
      audio.removeEventListener("pause", onPause);
      if (!audio.paused) audio.pause?.();
    };
  }, []);

  const toggle = () => {
    const audio = audioRef.current;
    if (!audio || !label) return;
    if (playing) audio.pause();
    else void audio.play?.()?.catch?.(() => setPlaying(false));
  };

  const handle: MiniPlayerHandle = { play };
  const node = (
    <div className="material-preview__player" role="group" aria-label="播放器">
      <audio preload="none" ref={audioRef} />
      {blocked ? (
        <button
          className="material-preview__play-all"
          onClick={() => play(blocked.url, blocked.atMs, blocked.label, { clip: false })}
          type="button"
        >
          ▶ 从 {formatTime(blocked.atMs)} 放
        </button>
      ) : (
        label && (
          <>
            <button
              aria-label={playing ? "暂停" : "播放"}
              className="material-preview__toggle"
              onClick={toggle}
              type="button"
            >
              {playing ? "❚❚" : "▶"}
            </button>
            <span className="material-preview__time">
              {label} · {formatTime(positionMs)}
            </span>
          </>
        )
      )}
    </div>
  );
  return { handle, node };
}

interface MaterialPreviewDrawerProps {
  apiClient: ApiClient;
  fileId: number;
  /** 从搜索的 ▶ 进来：不等预览数据，直接从这个时间放 */
  startMs?: number | null;
  canReveal: boolean;
  /** 手机上没有关系图 */
  isMobile: boolean;
  onClose: () => void;
  onOpenMeeting: (meetingId: string, seekMs?: number) => void;
  onOpenTask?: (taskId: string) => void;
  onOpenInGraph?: (projectId: string, fileId: number) => void;
}

/** 材料预览抽屉：像任务抽屉那样挂在 App 根部。手机上也能打开，只读。 */
export function MaterialPreviewDrawer({
  apiClient,
  fileId,
  startMs = null,
  canReveal,
  isMobile,
  onClose,
  onOpenMeeting,
  onOpenTask,
  onOpenInGraph,
}: MaterialPreviewDrawerProps) {
  const drawerRef = useRef<HTMLDivElement>(null);
  useDialogFocus(drawerRef);
  const [data, setData] = useState<MaterialFilePreview | null>(null);
  const [state, setState] = useState<"loading" | "ready" | "error">("loading");
  const [notice, setNotice] = useState("");
  const player = useDrawerPlayer();
  const { play } = player.handle;

  const load = useCallback(async () => {
    setState("loading");
    try {
      setData(await apiClient.getMaterialPreview(fileId));
      setState("ready");
    } catch {
      setState("error");
    }
  }, [apiClient, fileId]);

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    if (startMs !== null && startMs !== undefined) play(materialMediaUrl(fileId), startMs, "", { clip: false });
  }, [fileId, play, startMs]);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !event.isComposing) onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const copyPath = async () => {
    if (!data) return;
    try {
      await copyText(data.file.path);
      setNotice("路径已复制");
    } catch {
      setNotice("复制失败，请手动选中路径");
    }
  };

  const reveal = async () => {
    if (!data) return;
    try {
      await apiClient.revealMaterial(data.file.path);
      setNotice("");
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "打不开访达");
    }
  };

  const mentions = data?.mentions ?? [];
  const deliverables = data?.deliverables ?? [];
  return (
    <div aria-label="材料预览" aria-modal="true" className="material-drawer" ref={drawerRef} role="dialog">
      <div aria-hidden="true" className="material-drawer__scrim" onClick={onClose} />
      <aside className="material-drawer__panel">
        <header className="material-drawer__head">
          <div className="material-drawer__title">
            {data && (
              <>
                {data.file.ext && <span className="material-drawer__ext">{data.file.ext.toUpperCase()}</span>}
                <h2>{data.file.name}</h2>
              </>
            )}
          </div>
          <button aria-label="关闭" className="material-drawer__close" onClick={onClose} type="button">
            ✕
          </button>
        </header>
        <div className="material-drawer__body">
          {player.node}
          {state === "loading" && <AsyncState state="loading" />}
          {state === "error" && (
            <div className="material-drawer__error">
              <AsyncState message="预览读取失败" state="error" />
              <button onClick={() => void load()} type="button">
                重试
              </button>
            </div>
          )}
          {state === "ready" && data && (
            <>
              <p className="material-drawer__where">
                <span>{data.file.folder_path}</span>
                {data.file.modified_at && <span>修改于 {formatDate(data.file.modified_at)}</span>}
                {data.file.size !== null && <span>{formatBytes(data.file.size)}</span>}
              </p>
              <PreviewBlock data={data} onOpenMeeting={(id) => onOpenMeeting(id)} player={player.handle} />
              {mentions.length > 0 && (
                <section className="material-drawer__section">
                  <h3>在 {data.mentioned_meetings ?? mentions.length} 场会上被提到</h3>
                  <ul className="material-drawer__mentions">
                    {mentions.map((item) => (
                      <li key={item.meeting_id}>
                        <button
                          className="material-drawer__link"
                          onClick={() => onOpenMeeting(item.meeting_id, item.first_ms ?? undefined)}
                          type="button"
                        >
                          {item.title}
                        </button>
                        <span className="material-preview__muted">
                          {item.date} · {item.count} 次
                        </span>
                        {item.quote && (
                          <span className="material-drawer__quote">
                            {item.audio_url && item.first_ms !== null && (
                              <button
                                aria-label={`从 ${formatTime(item.first_ms)} 播放原话`}
                                className="material-preview__seek"
                                onClick={() => play(item.audio_url!, item.first_ms ?? 0, item.title, { clip: true })}
                                type="button"
                              >
                                ▶
                              </button>
                            )}
                            {item.quote}
                          </span>
                        )}
                      </li>
                    ))}
                  </ul>
                </section>
              )}
              {deliverables.length > 0 && (
                <section className="material-drawer__section">
                  <h3>交付物</h3>
                  <ul className="material-drawer__deliverables">
                    {deliverables.map((item) => (
                      <li key={item.deliverable_id}>
                        {onOpenTask ? (
                          <button className="material-drawer__link" onClick={() => onOpenTask(item.task_id)} type="button">
                            {item.title}
                          </button>
                        ) : (
                          <span>{item.title}</span>
                        )}
                      </li>
                    ))}
                  </ul>
                </section>
              )}
              {notice && (
                <p className="material-preview__muted" role="status">
                  {notice}
                </p>
              )}
            </>
          )}
        </div>
        {state === "ready" && data && (
          <footer className="material-drawer__foot">
            <button onClick={() => void copyPath()} type="button">
              复制路径
            </button>
            {canReveal && !data.file.gone && (
              <button onClick={() => void reveal()} type="button">
                在访达中显示
              </button>
            )}
            {!isMobile && onOpenInGraph && (
              <button onClick={() => onOpenInGraph(data.file.project_id, data.file.id)} type="button">
                在关系图里看
              </button>
            )}
          </footer>
        )}
      </aside>
    </div>
  );
}
