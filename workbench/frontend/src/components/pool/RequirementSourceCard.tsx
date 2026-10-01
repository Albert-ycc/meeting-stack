import { formatDurationText, formatMonthDayClock, formatTime } from "../../format";
import type { RequirementSource } from "../../types";
import { anchorLabel } from "./PosterCard";
import { PosterWaveform } from "./PosterWaveform";
import "./RequirementSourceCard.css";

interface RequirementSourceCardProps {
  /** 提出它的那句（头部波形取这场会）；没有时取来源里的第一场会 */
  origin: RequirementSource | null;
  /** 提出它的和合并进来的原话，按会议时间先后（R05-2） */
  sources: RequirementSource[];
  /** 点波形、时间签、原话时间：打开那场会，从这一秒开始放（同 R02） */
  onOpenMeeting: (meetingId: string, atMs?: number) => void;
}

function sourceLabel(source: RequirementSource, waveMeetingId: string): string {
  const where = source.meeting_id === waveMeetingId ? "" : ` · ${source.meeting_title}`;
  if (source.kind === "origin") return `提出 · 会上原话${where}`;
  return source.via_candidate_title ? `合并自候选「${source.via_candidate_title}」${where}` : `合并进来的原话${where}`;
}

/**
 * 需求详情的「出自录音」（R05-1、R05-2，S12/S13）：提出它的那场会的真实波形，这场会里的原话在波形上打标记
 * （提出的实心、合并进来的空心），底下按会议时间先后列出全部原话。没有来源时不画（同 R02）。
 */
export function RequirementSourceCard({ origin, sources, onOpenMeeting }: RequirementSourceCardProps) {
  const wave = origin ?? sources[0] ?? null;
  if (!wave) return null;
  const marks = sources.filter((source) => source.meeting_id === wave.meeting_id && source.anchor_ms !== null);
  // 提出的那句排第一：波形上它之前的柱子加深
  marks.sort((left, right) => (left.kind === "origin" ? -1 : right.kind === "origin" ? 1 : 0));
  const duration = wave.duration_ms ?? 0;
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

      <div className="source-card__wave">
        {duration > 0 && (
          <div aria-hidden="true" className="source-card__flags">
            {marks.map((source) => (
              <button
                className={`source-card__flag ${source.kind === "origin" ? "is-origin" : ""}`}
                key={source.id}
                onClick={() => onOpenMeeting(source.meeting_id, source.anchor_ms ?? 0)}
                style={{ left: `${Math.min(100, ((source.anchor_ms ?? 0) / duration) * 100)}%` }}
                tabIndex={-1}
                type="button"
              >
                {anchorLabel(source.anchor_ms ?? 0)}{" "}
                {source.kind === "origin" ? "提出" : `合并${source.via_candidate_title ? ` · ${source.via_candidate_title}` : ""}`}
              </button>
            ))}
          </div>
        )}
        <PosterWaveform
          artifactId={wave.audio_artifact_id}
          bars={420}
          height={64}
          label={`${wave.meeting_title} 的录音波形`}
          markers={marks.map((source) => ({ atMs: source.anchor_ms ?? 0 }))}
          onActivate={() => onOpenMeeting(wave.meeting_id, firstAnchor)}
        />
        {duration > 0 && (
          <div className="source-card__axis">
            <span>00:00</span>
            <span>{formatTime(duration)}</span>
          </div>
        )}
      </div>

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
              <small>{sourceLabel(source, wave.meeting_id)}</small>
              <p>{source.quote ? `「${source.quote}」` : "（只关联了这场会，没挑原话）"}</p>
            </div>
          </li>
        ))}
      </ol>
    </section>
  );
}
