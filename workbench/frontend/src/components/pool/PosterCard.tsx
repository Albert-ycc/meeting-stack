import type { KeyboardEvent, MouseEvent } from "react";

import { formatDurationText, formatMonthDay, formatMonthDayClock, formatTime } from "../../format";
import type { PoolItem } from "../../types";
import { PosterWaveform } from "./PosterWaveform";
import "./PosterCard.css";

interface PosterCardProps {
  item: PoolItem;
  canWrite: boolean;
  /** 新增、认领页右侧的「墙上预览」：只看样子，按钮不响应 */
  preview?: boolean;
  /** 点海报：需求进详情，候选进认领页（R02-5） */
  onOpen?: (item: PoolItem) => void;
  /** 点波形或原话时间：打开这场会，从这一秒开始放（R02-3） */
  onOpenMeeting?: (meetingId: string, atMs: number) => void;
  onClaim?: (item: PoolItem) => void;
  onMerge?: (item: PoolItem) => void;
  onDrop?: (item: PoolItem) => void;
}

export function anchorLabel(milliseconds: number): string {
  return formatTime(milliseconds, true);
}

/** 海报底栏的日子：出自哪场会就写那场会的日子，没有来源写建的日子 */
export function posterDateLabel(item: PoolItem): { day: string; text: string } {
  if (item.source) return { day: formatMonthDay(item.source.recording_date), text: "会上提出" };
  return { day: formatMonthDay(item.created_at), text: "新建" };
}

function LinkIcon() {
  return (
    <svg aria-hidden="true" fill="none" height="12" stroke="currentColor" strokeLinecap="round" strokeWidth="1.6" viewBox="0 0 16 16" width="12">
      <path d="M6.6 9.4l2.8-2.8" />
      <path d="M7.4 4.6l1.1-1.1a2.8 2.8 0 014 4l-1.1 1.1" />
      <path d="M8.6 11.4l-1.1 1.1a2.8 2.8 0 01-4-4l1.1-1.1" />
    </svg>
  );
}

function stop(event: MouseEvent) {
  event.stopPropagation();
}

export function PosterCard({
  item,
  canWrite,
  preview = false,
  onOpen,
  onOpenMeeting,
  onClaim,
  onMerge,
  onDrop,
}: PosterCardProps) {
  const candidate = item.kind === "candidate";
  const source = item.source;
  const date = posterDateLabel(item);
  const open = () => {
    if (!preview) onOpen?.(item);
  };
  const openSource = () => {
    if (preview || !source || !onOpenMeeting) return;
    onOpenMeeting(source.meeting_id, source.anchor_ms ?? 0);
  };
  const meta = source
    ? [
        formatMonthDayClock(source.recording_date),
        source.duration_ms ? formatDurationText(source.duration_ms) : null,
        item.follow_up_count > 0 ? `跟进 ${item.follow_up_count} 场` : null,
      ]
        .filter(Boolean)
        .join(" · ")
    : "";
  const mergeFirst = candidate && item.default_action === "merge" && item.can_merge;

  return (
    <article
      aria-label={`${candidate ? "候选" : "需求"}：${item.title}`}
      className={`poster ${candidate ? "poster--candidate" : ""} ${preview ? "poster--preview" : ""}`}
      data-status={item.status}
      onClick={open}
      onKeyDown={(event: KeyboardEvent) => {
        if (event.target === event.currentTarget && (event.key === "Enter" || event.key === " ")) {
          event.preventDefault();
          open();
        }
      }}
      tabIndex={preview ? -1 : 0}
    >
      <div className="poster__mat">
        <header className="poster__head">
          <span className={`poster__project ${item.project_id ? "" : "is-unassigned"}`}>
            {item.project_seat !== null && <span className="poster__seat">{item.project_seat}</span>}
            <span className="poster__project-name">{item.project_name ?? "未归项目"}</span>
          </span>
          {candidate ? (
            <span className="poster__tag poster__tag--ai">AI 候选</span>
          ) : (
            item.priority && (
              <span className={`poster__tag poster__tag--${item.priority.toLowerCase()}`}>{item.priority}</span>
            )
          )}
        </header>

        <h3 className={`poster__title ${item.title ? "" : "is-placeholder"}`}>{item.title || "需求名"}</h3>
        {/* 说明区固定三行高：有「像已有需求」时说明收成两行、这一句占第三行，同一排的统计行对得齐 */}
        <div className="poster__body">
          <p
            className={`poster__summary ${item.summary ? "" : "is-placeholder"} ${
              candidate && item.similar_requirement ? "is-short" : ""
            }`}
          >
            {item.summary || (preview ? "说明" : "")}
          </p>
          {candidate && item.similar_requirement && (
            <p className="poster__similar">
              <LinkIcon />
              像已有需求：<b>{item.similar_requirement.title}</b>
            </p>
          )}
        </div>

        <dl className="poster__stats">
          <div>
            <dt>待办</dt>
            <dd>{item.open_task_count}</dd>
          </div>
          <div>
            <dt>会议</dt>
            <dd>{item.meeting_count}</dd>
          </div>
          <div>
            <dt>材料</dt>
            <dd>{item.folder_count}</dd>
          </div>
        </dl>

        <section className="poster__source">
          {source && (
            <>
              <div className="poster__source-head">
                <span>出自录音</span>
                {source.anchor_ms !== null && (
                  <button
                    className="poster__anchor"
                    onClick={(event) => {
                      stop(event);
                      openSource();
                    }}
                    tabIndex={preview ? -1 : 0}
                    type="button"
                  >
                    <span aria-hidden="true" className="poster__anchor-play" />
                    原话 {anchorLabel(source.anchor_ms)}
                  </button>
                )}
              </div>
              <PosterWaveform
                artifactId={source.audio_artifact_id}
                label={`${source.meeting_title} 的录音波形`}
                markers={source.anchor_ms !== null ? [{ atMs: source.anchor_ms }] : []}
                onActivate={preview || !onOpenMeeting ? undefined : openSource}
              />
              <p className="poster__meeting">{source.meeting_title}</p>
              <p className="poster__meeting-meta">{meta}</p>
            </>
          )}
        </section>

        <footer className="poster__foot">
          <span className="poster__date">
            <b>{date.day}</b> {date.text}
          </span>
          {candidate ? (
            canWrite && (
              <span className="poster__actions" onClick={stop}>
                <button className="poster__text-action" disabled={preview} onClick={() => onDrop?.(item)} type="button">
                  丢掉
                </button>
                {item.can_merge && (
                  <button
                    className={`poster__pill ${mergeFirst ? "is-primary" : ""}`}
                    disabled={preview}
                    onClick={() => onMerge?.(item)}
                    type="button"
                  >
                    合并
                  </button>
                )}
                <button
                  className={`poster__pill ${mergeFirst ? "" : "is-primary"}`}
                  disabled={preview}
                  onClick={() => onClaim?.(item)}
                  type="button"
                >
                  认领 <span aria-hidden="true">→</span>
                </button>
              </span>
            )
          ) : (
            <span className="poster__actions" onClick={stop}>
              <button className="poster__text-action" disabled={preview} onClick={open} type="button">
                查看
              </button>
              <button className="poster__pill" disabled={preview} onClick={open} type="button">
                接下 <span aria-hidden="true">→</span>
              </button>
            </span>
          )}
        </footer>
      </div>
    </article>
  );
}
