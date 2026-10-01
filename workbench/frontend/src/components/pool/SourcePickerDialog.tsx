import { useEffect, useMemo, useRef, useState, type MouseEvent } from "react";

import type { ApiClient } from "../../api";
import { formatDurationText, formatMonthDayClock } from "../../format";
import type { MeetingSummary, RequirementSource, Segment } from "../../types";
import { useDialogFocus } from "../useDialog";
import { anchorLabel } from "./PosterCard";
import "./PoolDialogs.css";

/** 表单上的来源：提出它的会议、会上原话、时间锚，加上画来源面板要用的会名、时间、时长和录音 */
export type SourceDraft = Omit<RequirementSource, "id" | "kind" | "via_candidate_title">;

/** 和后端一致：原话最多 1000 字 */
export const QUOTE_MAX = 1000;

const MEETING_LIMIT = 30;

/** 选中的几句连成原话：跨了多句时原话取选中的文字，时间锚取第一句的开头（R01 异常与边界） */
export function quoteOf(segments: Segment[], from: number, to: number): { quote: string; anchor_ms: number } {
  const picked = segments.slice(Math.min(from, to), Math.max(from, to) + 1);
  return {
    quote: picked.map((segment) => segment.text.trim()).join(""),
    anchor_ms: picked[0]?.start_ms ?? 0,
  };
}

interface SourcePickerDialogProps {
  apiClient: ApiClient;
  /** 需求所属项目：默认只列这个项目的会，可以放开看全部 */
  projectId: string | null;
  projectName: string | null;
  onClose: () => void;
  onPicked: (source: SourceDraft) => void;
}

/**
 * 选来源（R04-3）：先选来源会议，再从这场会的逐字稿里挑原话，时间锚随原话带出。
 * 点一句选一句，按住 Shift 再点一句选中间连着的几句；也可以只关联这场会、不挑原话。
 */
export function SourcePickerDialog({ apiClient, projectId, projectName, onClose, onPicked }: SourcePickerDialogProps) {
  const dialogRef = useRef<HTMLDivElement>(null);
  useDialogFocus(dialogRef);
  const [query, setQuery] = useState("");
  const [onlyProject, setOnlyProject] = useState(Boolean(projectId));
  const [meetings, setMeetings] = useState<MeetingSummary[] | null>(null);
  const [meetingsError, setMeetingsError] = useState("");
  const [meeting, setMeeting] = useState<MeetingSummary | null>(null);
  const [segments, setSegments] = useState<Segment[] | null>(null);
  const [segmentsError, setSegmentsError] = useState("");
  const [find, setFind] = useState("");
  const [range, setRange] = useState<{ from: number; to: number } | null>(null);

  // 会议列表：搜索打字停 300ms 再查
  useEffect(() => {
    if (meeting) return;
    let active = true;
    const timer = window.setTimeout(() => {
      apiClient
        .meetings({
          q: query.trim() || undefined,
          project_id: onlyProject && projectId ? projectId : undefined,
          limit: MEETING_LIMIT,
        })
        .then((payload) => {
          if (!active) return;
          setMeetings(payload.items);
          setMeetingsError("");
        })
        .catch((error: unknown) => {
          if (active) setMeetingsError(error instanceof Error ? error.message : "会议读取失败");
        });
    }, query ? 300 : 0);
    return () => {
      active = false;
      window.clearTimeout(timer);
    };
  }, [apiClient, meeting, onlyProject, projectId, query]);

  // 选了会：读这场会的逐字稿
  useEffect(() => {
    if (!meeting) return;
    let active = true;
    setSegments(null);
    setSegmentsError("");
    apiClient
      .meeting(meeting.id)
      .then((detail) => {
        if (active) setSegments(detail.segments);
      })
      .catch((error: unknown) => {
        if (active) setSegmentsError(error instanceof Error ? error.message : "逐字稿读取失败");
      });
    return () => {
      active = false;
    };
  }, [apiClient, meeting]);

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !event.isComposing) onClose();
    };
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [onClose]);

  const shown = useMemo(() => {
    if (!segments) return [];
    const needle = find.trim().toLowerCase();
    return segments
      .map((segment, index) => ({ segment, index }))
      .filter(({ segment }) => !needle || segment.text.toLowerCase().includes(needle));
  }, [find, segments]);

  const picked = segments && range ? quoteOf(segments, range.from, range.to) : null;
  const tooLong = picked !== null && [...picked.quote].length > QUOTE_MAX;

  const pickSegment = (index: number, event: MouseEvent) => {
    setRange((current) => (event.shiftKey && current ? { from: current.from, to: index } : { from: index, to: index }));
  };

  const finish = (withQuote: boolean) => {
    if (!meeting) return;
    onPicked({
      meeting_id: meeting.id,
      meeting_title: meeting.title,
      recording_date: meeting.recording_date ?? null,
      duration_ms: meeting.duration_ms ?? null,
      audio_artifact_id: meeting.audio_artifact_id ?? null,
      quote: withQuote && picked ? picked.quote : "",
      anchor_ms: withQuote && picked ? picked.anchor_ms : null,
    });
  };

  const inRange = (index: number) =>
    range !== null && index >= Math.min(range.from, range.to) && index <= Math.max(range.from, range.to);

  return (
    <div className="pool-dialog__overlay">
      <div aria-label="选来源" aria-modal="true" className="pool-dialog source-picker" ref={dialogRef} role="dialog">
        <header className="pool-dialog__head">
          <div>
            <h2>{meeting ? "挑会上原话" : "选来源会议"}</h2>
            <p>{meeting ? `${meeting.title} · 点一句选一句，按住 Shift 再点可以连着选几句` : "提出这条需求的那场会，选定后同时加进关联会议"}</p>
          </div>
          <button aria-label="关闭" className="pool-dialog__close" onClick={onClose} type="button">
            ✕
          </button>
        </header>

        {!meeting ? (
          <>
            <div className="source-picker__filters">
              <input
                aria-label="搜会议"
                onChange={(event) => setQuery(event.target.value)}
                placeholder="搜会名"
                value={query}
              />
              {projectId && (
                <label>
                  <input checked={onlyProject} onChange={(event) => setOnlyProject(event.target.checked)} type="checkbox" />
                  只看「{projectName ?? "这个项目"}」的会
                </label>
              )}
            </div>
            {meetingsError ? (
              <p className="pool-dialog__error" role="alert">
                {meetingsError}
              </p>
            ) : meetings === null ? (
              <p className="pool-dialog__state">正在读取…</p>
            ) : meetings.length === 0 ? (
              <p className="pool-dialog__state">没有找到会议</p>
            ) : (
              <ul aria-label="会议" className="source-picker__meetings">
                {meetings.map((item) => (
                  <li key={item.id}>
                    <button onClick={() => setMeeting(item)} type="button">
                      <strong>{item.title}</strong>
                      <span>
                        {[
                          item.recording_date ? formatMonthDayClock(item.recording_date) : null,
                          item.duration_ms ? formatDurationText(item.duration_ms) : null,
                          onlyProject ? null : item.project_name ?? "未归项目",
                        ]
                          .filter(Boolean)
                          .join(" · ")}
                      </span>
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </>
        ) : (
          <>
            <div className="source-picker__filters">
              <input
                aria-label="在逐字稿中查找"
                onChange={(event) => setFind(event.target.value)}
                placeholder="在逐字稿中查找"
                value={find}
              />
            </div>
            {segmentsError ? (
              <p className="pool-dialog__error" role="alert">
                {segmentsError}
              </p>
            ) : segments === null ? (
              <p className="pool-dialog__state">正在读取逐字稿…</p>
            ) : segments.length === 0 ? (
              <p className="pool-dialog__state">这场会还没有逐字稿，只能关联这场会</p>
            ) : (
              <ol aria-label="逐字稿" className="source-picker__lines">
                {shown.map(({ segment, index }) => (
                  <li key={segment.id}>
                    <button
                      aria-pressed={inRange(index)}
                      className={inRange(index) ? "is-picked" : undefined}
                      onClick={(event) => pickSegment(index, event)}
                      type="button"
                    >
                      <span className="source-picker__time">{anchorLabel(segment.start_ms)}</span>
                      <span>{segment.text}</span>
                    </button>
                  </li>
                ))}
              </ol>
            )}
            <p className={`source-picker__preview ${tooLong ? "is-over" : ""}`}>
              {picked
                ? tooLong
                  ? `原话最多 ${QUOTE_MAX} 字，少选几句`
                  : `▶ ${anchorLabel(picked.anchor_ms)}「${picked.quote}」`
                : "还没挑原话"}
            </p>
          </>
        )}

        <footer className="pool-dialog__foot">
          {meeting ? (
            <>
              <button
                className="pool-dialog__cancel"
                onClick={() => {
                  setMeeting(null);
                  setRange(null);
                  setFind("");
                }}
                type="button"
              >
                换一场会
              </button>
              <button className="pool-dialog__cancel" onClick={() => finish(false)} type="button">
                只关联这场会
              </button>
              <button
                className="pool-dialog__submit"
                disabled={!picked || tooLong}
                onClick={() => finish(true)}
                type="button"
              >
                用这句
              </button>
            </>
          ) : (
            <button className="pool-dialog__cancel" onClick={onClose} type="button">
              取消
            </button>
          )}
        </footer>
      </div>
    </div>
  );
}
