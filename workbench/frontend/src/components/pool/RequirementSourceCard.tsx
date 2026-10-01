import { useEffect, useLayoutEffect, useReducer, useRef, useState, type CSSProperties } from "react";

import { formatDurationText, formatMonthDayClock, formatTime } from "../../format";
import type { RequirementSource } from "../../types";
import { EMPTY_FLAG_LAYOUT, FLAG_ROW_HEIGHT, packFlags, sameFlagLayout, type FlagLayout } from "./flagLayout";
import { anchorLabel } from "./PosterCard";
import { PosterWaveform, usePeaks, type Peaks } from "./PosterWaveform";
import "./RequirementSourceCard.css";

interface RequirementSourceCardProps {
  /** 提出它的那句（头部波形取这场会）；没有时取来源里的第一场会 */
  origin: RequirementSource | null;
  /** 提出它的和合并进来的原话，按会议时间先后（R05-2） */
  sources: RequirementSource[];
  /** 点波形、时间签、原话时间：打开那场会，从这一秒开始放（同 R02） */
  onOpenMeeting: (meetingId: string, atMs?: number) => void;
  /** 合并进来的原话还在撤销时限内（R01-14）时，那一行给［撤销合并］；不传就不画这个按钮 */
  onUndoMerge?: (candidateId: string) => void;
  /** 撤销在进行中：按钮置灰，免得连点 */
  undoBusy?: boolean;
}

function sourceLabel(source: RequirementSource, waveMeetingId: string): string {
  const where = source.meeting_id === waveMeetingId ? "" : ` · ${source.meeting_title}`;
  // 选了来源会议、没挑原话：不能写「会上原话」，那一行底下没有原话
  if (source.kind === "origin") return `提出 · ${(source.quote || "").trim() ? "会上原话" : "只关联了会议"}${where}`;
  return source.via_candidate_title ? `合并自候选「${source.via_candidate_title}」${where}` : `合并进来的原话${where}`;
}

function flagText(source: RequirementSource): string {
  const tail = source.kind === "origin" ? "提出" : `合并${source.via_candidate_title ? ` · ${source.via_candidate_title}` : ""}`;
  return `${anchorLabel(source.anchor_ms ?? 0)} ${tail}`;
}

// setTimeout 的上限是 2^31-1 毫秒；撤销时限只有 10 分钟，这里只防数据里给了个很远的时间
const MAX_TIMER_MS = 2_147_483_647;

/**
 * 返回「现在」，并在最近一个截止时刻到来时重绘一次：撤销合并的按钮过了时限自己消失，不用等刷新页面。
 * 按本机时间判断（Date.now）。
 */
function useExpiryClock(untils: string[]): number {
  const [ticks, tick] = useReducer((count: number) => count + 1, 0);
  const key = untils.join("|");
  const rendered = Date.now();
  useEffect(() => {
    // 渲染那一刻还没到点的才需要等：渲染到副作用执行之间时钟又走了一截、已经过点的，马上补一次重绘
    const upcoming = untils.map((until) => Date.parse(until)).filter((time) => time > rendered);
    if (upcoming.length === 0) return;
    const wait = Math.max(0, Math.min(...upcoming) - Date.now());
    // 多等 50 毫秒：个别浏览器的定时器会比系统时钟早一点点触发，早了就白等一轮
    const timer = window.setTimeout(tick, Math.min(wait + 50, MAX_TIMER_MS));
    return () => window.clearTimeout(timer);
    // untils 的内容已经在 key 里，rendered 跟着 ticks 一起换
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, ticks]);
  return rendered;
}

interface SourceWaveProps {
  wave: RequirementSource;
  marks: RequirementSource[];
  /** 还在取峰值时为 null：先占住位置，免得取到了把下面的内容顶下去 */
  peaks: Peaks | null;
  firstAnchor: number | undefined;
  onOpenMeeting: (meetingId: string, atMs?: number) => void;
}

/**
 * 波形、波形上方的时间签、底下的 00:00～时长坐标轴。只在这场会有录音、峰值取得到时才画（调用处判断）。
 * 时间签的位置按锚点算，贴着锚点又不伸出波形的左右边；挨得近的错开成几行（见 flagLayout）。
 */
function SourceWave({ wave, marks, peaks, firstAnchor, onOpenMeeting }: SourceWaveProps) {
  // 时间签和波形上的竖线用同一个时长（取到的峰值的时长），免得两边差几秒对不上
  const durationMs = peaks ? peaks.duration_seconds * 1000 : 0;
  const areaRef = useRef<HTMLDivElement>(null);
  const flagRefs = useRef(new Map<number, HTMLButtonElement>());
  const [layout, setLayout] = useState<FlagLayout>(EMPTY_FLAG_LAYOUT);
  const percentOf = (source: RequirementSource) =>
    durationMs > 0 ? Math.min(100, Math.max(0, ((source.anchor_ms ?? 0) / durationMs) * 100)) : 0;
  const signature = `${durationMs}|${marks.map((source) => `${source.id}:${source.anchor_ms}:${flagText(source)}`).join("|")}`;

  // 量出每个签的实际宽度和波形的宽度再排：字体、窗口宽度变了都会重排
  useLayoutEffect(() => {
    const area = areaRef.current;
    if (!area) return;
    const measure = () => {
      const next = packFlags(
        marks.map((source) => ({
          id: source.id,
          percent: percentOf(source),
          width: flagRefs.current.get(source.id)?.offsetWidth ?? 0,
        })),
        area.clientWidth,
      );
      setLayout((current) => (sameFlagLayout(current, next) ? current : next));
    };
    measure();
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(measure);
    observer.observe(area);
    flagRefs.current.forEach((element) => observer.observe(element));
    return () => observer.disconnect();
    // marks 和 durationMs 的内容已经在 signature 里
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [signature, peaks !== null]);

  if (!peaks) return <div aria-hidden="true" className="source-card__wave is-loading" />;
  return (
    <div className="source-card__wave" style={{ "--flag-rows": layout.rows } as CSSProperties}>
      <div aria-hidden="true" className="source-card__flags" ref={areaRef}>
        {marks.map((source) => {
          const percent = percentOf(source);
          const row = layout.rowOf.get(source.id) ?? 0;
          return (
            <button
              className={`source-card__flag ${source.kind === "origin" ? "is-origin" : ""}`}
              key={source.id}
              onClick={() => onOpenMeeting(source.meeting_id, source.anchor_ms ?? 0)}
              ref={(element) => {
                if (element) flagRefs.current.set(source.id, element);
                else flagRefs.current.delete(source.id);
              }}
              style={{
                left: `${percent}%`,
                top: (layout.rows - 1 - row) * FLAG_ROW_HEIGHT,
                transform: `translateX(-${percent}%)`,
              }}
              tabIndex={-1}
              title={flagText(source)}
              type="button"
            >
              {flagText(source)}
            </button>
          );
        })}
      </div>
      <PosterWaveform
        artifactId={wave.audio_artifact_id}
        bars={420}
        height={64}
        label={`${wave.meeting_title} 的录音波形`}
        markers={marks.map((source) => ({ atMs: source.anchor_ms ?? 0 }))}
        onActivate={() => onOpenMeeting(wave.meeting_id, firstAnchor)}
        peaks={peaks}
      />
      <div className="source-card__axis">
        <span>00:00</span>
        <span>{formatTime(durationMs)}</span>
      </div>
    </div>
  );
}

/**
 * 需求详情的「出自录音」（R05-1、R05-2，S12/S13）：提出它的那场会的真实波形，这场会里的原话在波形上打标记
 * （提出的实心、合并进来的空心），底下按会议时间先后列出全部原话。没有来源时不画（同 R02）。
 * 这场会没有录音文件、或峰值接口取不到时，只显示会议信息和原话时间锚：波形、时间签、坐标轴都不画，也不留空位（R02 异常）。
 */
export function RequirementSourceCard({ origin, sources, onOpenMeeting, onUndoMerge, undoBusy = false }: RequirementSourceCardProps) {
  const wave = origin ?? sources[0] ?? null;
  const peaks = usePeaks(wave?.audio_artifact_id ?? null);
  const now = useExpiryClock(sources.flatMap((source) => (source.undo_merge ? [source.undo_merge.until] : [])));
  if (!wave) return null;
  const marks = sources.filter((source) => source.meeting_id === wave.meeting_id && source.anchor_ms !== null);
  // 提出的那句排第一：波形上它之前的柱子加深
  marks.sort((left, right) => (left.kind === "origin" ? -1 : right.kind === "origin" ? 1 : 0));
  const firstAnchor = origin?.anchor_ms ?? marks[0]?.anchor_ms ?? undefined;

  return (
    <section aria-label="出自录音" className="requirement-detail__card source-card">
      <header className="source-card__head">
        <strong>出自录音</strong>
        <span className="source-card__meeting">{wave.meeting_title}</span>
        <span className="source-card__meta">
          {formatMonthDayClock(wave.recording_date)}
          {wave.duration_ms ? ` · ${formatDurationText(wave.duration_ms)}` : ""}
        </span>
        <button className="source-card__open" onClick={() => onOpenMeeting(wave.meeting_id, firstAnchor)} type="button">
          打开会议 <span aria-hidden="true">→</span>
        </button>
      </header>

      {peaks.status !== "unavailable" && (
        <SourceWave
          firstAnchor={firstAnchor}
          marks={marks}
          onOpenMeeting={onOpenMeeting}
          peaks={peaks.status === "ready" ? peaks.peaks : null}
          wave={wave}
        />
      )}

      <ol className="source-card__quotes">
        {sources.map((source) => (
          <li key={source.id}>
            {source.anchor_ms !== null ? (
              <button
                aria-label={`从 ${anchorLabel(source.anchor_ms)} 开始放 ${source.meeting_title}`}
                className="source-card__time"
                onClick={() => onOpenMeeting(source.meeting_id, source.anchor_ms ?? 0)}
                type="button"
              >
                ▶ {anchorLabel(source.anchor_ms)}
              </button>
            ) : (
              <button className="source-card__time is-meeting" onClick={() => onOpenMeeting(source.meeting_id)} type="button">
                打开会议
              </button>
            )}
            <div className="source-card__quote">
              <div className="source-card__quote-head">
                <small>{sourceLabel(source, wave.meeting_id)}</small>
                {onUndoMerge && source.undo_merge && Date.parse(source.undo_merge.until) > now && (
                  <button
                    className="source-card__undo"
                    disabled={undoBusy}
                    onClick={() => onUndoMerge(source.undo_merge!.candidate_id)}
                    type="button"
                  >
                    撤销合并
                  </button>
                )}
              </div>
              <p>{source.quote ? `「${source.quote}」` : "（只关联了这场会，没挑原话）"}</p>
            </div>
          </li>
        ))}
      </ol>
    </section>
  );
}
