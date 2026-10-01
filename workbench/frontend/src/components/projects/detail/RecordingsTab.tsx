import { useCallback, useEffect, useMemo, useState } from "react";

import type { ApiClient } from "../../../api";
import { dayStamp, formatClock, formatDurationText, type DayStamp } from "../../../format";
import type { ProjectRecordingRow } from "../../../types";
import { AsyncState } from "../../AsyncState";
import { CopyFolderPathButton } from "../../CopyFolderPathButton";
import "./detail.css";

/** 先露出最近几天，其余收在「更早 N 场」里 */
export const RECORDING_DAYS_VISIBLE = 8;

interface DayGroup {
  stamp: DayStamp;
  rows: ProjectRecordingRow[];
  durationMs: number;
}

/** 接口已按录音时间倒序，同一天的挨在一起，顺着切就行；分组方式同录音档案 */
function groupByDay(rows: ProjectRecordingRow[]): DayGroup[] {
  const groups: DayGroup[] = [];
  for (const row of rows) {
    const stamp = dayStamp(row.recording_date);
    let group = groups[groups.length - 1];
    if (!group || group.stamp.key !== stamp.key) {
      group = { stamp, rows: [], durationMs: 0 };
      groups.push(group);
    }
    group.rows.push(row);
    group.durationMs += row.duration_ms ?? 0;
  }
  return groups;
}

interface RecordingsTabProps {
  apiClient: ApiClient;
  projectId: string;
  reloadKey: number;
  onOpenMeeting: (meetingId: string) => void;
  onOpenRequirement: (id: string) => void;
}

/** 项目详情「录音」（R06-10）：本项目的会议按录音日期倒序、按日分组 */
export function RecordingsTab({ apiClient, projectId, reloadKey, onOpenMeeting, onOpenRequirement }: RecordingsTabProps) {
  const [rows, setRows] = useState<ProjectRecordingRow[] | null>(null);
  const [state, setState] = useState<"loading" | "ready" | "error">("loading");
  const [showAll, setShowAll] = useState(false);

  const load = useCallback(async () => {
    setState("loading");
    try {
      setRows(await apiClient.projectRecordings(projectId));
      setState("ready");
    } catch {
      setState("error");
    }
  }, [apiClient, projectId]);

  useEffect(() => {
    void load();
  }, [load, reloadKey]);

  const groups = useMemo(() => groupByDay(rows ?? []), [rows]);

  if (state === "loading") return <AsyncState state="loading" />;
  if (state === "error") {
    return (
      <div className="detail-error">
        <span>录音读取失败</span>
        <button onClick={() => void load()} type="button">
          重试
        </button>
      </div>
    );
  }
  if (groups.length === 0) return <AsyncState message="这个项目还没有录音" state="empty" />;

  const shown = showAll ? groups : groups.slice(0, RECORDING_DAYS_VISIBLE);
  const hidden = groups.slice(shown.length).reduce((sum, group) => sum + group.rows.length, 0);
  const thisYear = new Date().getFullYear();

  return (
    <div className="rec-tab archive-timeline">
      {shown.map((group) => (
        <section aria-label={group.stamp.monthDay} className="archive-day" key={group.stamp.key}>
          <header className="archive-day__head">
            <div className="archive-day__date">
              <strong>{group.stamp.monthDay}</strong>
              {group.stamp.weekday && <span>{group.stamp.weekday}</span>}
              {group.stamp.relative && <em className="archive-day__relative">{group.stamp.relative}</em>}
              {group.stamp.year > 0 && group.stamp.year !== thisYear && (
                <span className="archive-day__year">{group.stamp.year}</span>
              )}
            </div>
            <div className="archive-day__count">
              <strong>{group.rows.length}</strong>
              <span>场 · {formatDurationText(group.durationMs)}</span>
            </div>
          </header>
          <ul className="rec-list">
            {group.rows.map((row) => (
              <li className="rec-row" key={row.id} onClick={() => onOpenMeeting(row.id)}>
                <span className="rec-row__clock">{formatClock(row.recording_date) || "--:--"}</span>
                <span className="rec-row__title" title={row.title}>
                  {row.title}
                </span>
                <span className="rec-row__duration">{row.duration_ms != null ? formatDurationText(row.duration_ms) : "--"}</span>
                <span className="rec-row__tasks">任务 {row.task_count}</span>
                <span className="rec-row__reqs">
                  {row.requirements.map((requirement) => (
                    <button
                      className="rec-row__req"
                      key={requirement.id}
                      onClick={(event) => {
                        event.stopPropagation();
                        onOpenRequirement(requirement.id);
                      }}
                      title={requirement.title}
                      type="button"
                    >
                      <svg aria-hidden="true" fill="none" height="11" stroke="currentColor" strokeLinecap="round" strokeWidth="1.5" viewBox="0 0 16 16" width="11">
                        <path d="M3.5 14V2.5" />
                        <path d="M3.5 2.5h8l-1.6 3 1.6 3h-8" />
                      </svg>
                      <span>{requirement.title}</span>
                    </button>
                  ))}
                </span>
                <span className="rec-row__ops" onClick={(event) => event.stopPropagation()}>
                  <button
                    aria-label={`打开会议：${row.title}`}
                    className="rec-row__open"
                    onClick={() => onOpenMeeting(row.id)}
                    type="button"
                  >
                    打开
                  </button>
                  <CopyFolderPathButton path={row.canonical_dir} withLabel />
                </span>
              </li>
            ))}
          </ul>
        </section>
      ))}
      {hidden > 0 && (
        <button className="rec-tab__more" onClick={() => setShowAll(true)} type="button">
          更早 {hidden} 场 <span aria-hidden="true">⌄</span>
        </button>
      )}
    </div>
  );
}
