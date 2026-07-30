import { useMemo } from "react";

import { AsyncState } from "./AsyncState";
import { CopyFolderPathButton } from "./CopyFolderPathButton";
import {
  dayStamp,
  formatClock,
  formatDurationText,
  formatTime,
  isDoneStatus,
  isUntitled,
  statusLabel,
  statusTone,
  type DayStamp,
} from "../format";
import type {
  LoadState,
  MeetingFilters,
  MeetingSummary,
  Project,
  Tag,
} from "../types";

interface LibraryPageProps {
  filters: MeetingFilters;
  limit: number;
  meetings: MeetingSummary[];
  offset: number;
  onFilter: (filters: MeetingFilters) => void;
  onOpen: (meetingId: string) => void;
  onPageChange: (offset: number) => void;
  projects: Project[];
  state: LoadState;
  tags: Tag[];
  total: number;
}

const statusOptions = [
  ["", "全部状态"],
  ["done", "已完成"],
  ["failed", "失败"],
];

interface DayGroup {
  stamp: DayStamp;
  meetings: MeetingSummary[];
  durationMs: number;
}

function groupByDay(meetings: MeetingSummary[]): DayGroup[] {
  const groups: DayGroup[] = [];
  let current: DayGroup | null = null;
  for (const meeting of meetings) {
    const stamp = dayStamp(meeting.recording_date);
    if (!current || current.stamp.key !== stamp.key) {
      current = { stamp, meetings: [], durationMs: 0 };
      groups.push(current);
    }
    current.meetings.push(meeting);
    current.durationMs += meeting.duration_ms ?? 0;
  }
  return groups;
}

export function LibraryPage({
  filters,
  limit,
  meetings,
  offset,
  onFilter,
  onOpen,
  onPageChange,
  projects,
  state,
  tags,
  total,
}: LibraryPageProps) {
  const groups = useMemo(() => groupByDay(meetings), [meetings]);
  const thisYear = new Date().getFullYear();
  const update = (key: keyof MeetingFilters, value: string | number | undefined) =>
    onFilter({ ...filters, [key]: value || undefined });

  return (
    <div className="page-grid library-page">
      <section className="page-content">
        <header className="page-heading">
          <div>
            <span className="eyebrow">ARCHIVE / 资料库</span>
            <h1>会议录音档案</h1>
            <p>按录音日期倒序排列。原音频、逐字稿与纪要都在同一个文件夹里。</p>
          </div>
          <div className="record-count">
            <strong>{total}</strong>
            <span>全部结果</span>
          </div>
        </header>

        <div className="quick-filters" aria-label="快捷筛选">
          <select
            aria-label="筛选项目"
            onChange={(event) => update("project_id", event.target.value)}
            value={filters.project_id ?? ""}
          >
            <option value="">全部项目</option>
            {projects.map((project) => (
              <option key={project.id} value={project.id}>
                {project.name}
              </option>
            ))}
          </select>
          <select
            aria-label="筛选标签"
            onChange={(event) => update("tag_id", event.target.value)}
            value={filters.tag_id ?? ""}
          >
            <option value="">全部标签</option>
            {tags.map((tag) => (
              <option key={tag.id} value={tag.id}>
                {tag.name}
              </option>
            ))}
          </select>
          <select
            aria-label="筛选状态"
            onChange={(event) => update("status", event.target.value)}
            value={filters.status ?? ""}
          >
            {statusOptions.map(([value, label]) => (
              <option key={value || "all"} value={value}>
                {label}
              </option>
            ))}
          </select>
          {(Object.values(filters).some(Boolean)) && (
            <button className="text-button" onClick={() => onFilter({})} type="button">
              清除筛选
            </button>
          )}
        </div>

        {state === "loading" && <AsyncState state="loading" />}
        {state === "error" && <AsyncState message="资料库读取失败" state="error" />}
        {state === "empty" && <AsyncState state="empty" />}
        {state === "ready" && (
          <div className="archive-timeline">
            {groups.map((group, groupIndex) => (
              <section className="archive-day" key={group.stamp.key}>
                <header className="archive-day__head">
                  <div className="archive-day__date">
                    <strong>{group.stamp.monthDay}</strong>
                    {group.stamp.weekday && <span>{group.stamp.weekday}</span>}
                    {group.stamp.relative && (
                      <em className="archive-day__relative">{group.stamp.relative}</em>
                    )}
                    {group.stamp.year > 0 && group.stamp.year !== thisYear && (
                      <span className="archive-day__year">{group.stamp.year}</span>
                    )}
                  </div>
                  <div className="archive-day__count">
                    <strong>{group.meetings.length}</strong>
                    <span>场 · {formatDurationText(group.durationMs)}</span>
                  </div>
                </header>
                <div className="archive-list">
                  {group.meetings.map((meeting, index) => {
                    const untitled = isUntitled(meeting.title, meeting.id);
                    const done = isDoneStatus(meeting.status);
                    return (
                      <div
                        className="archive-row-shell"
                        key={meeting.id}
                        style={{ "--row-index": groupIndex * 4 + index } as React.CSSProperties}
                      >
                        <button
                          className="archive-row"
                          onClick={() => onOpen(meeting.id)}
                          type="button"
                        >
                          <span className="archive-row__primary">
                            <span className="archive-row__clock mono">
                              {formatClock(meeting.recording_date) || "--:--"}
                            </span>
                            <strong id={`meeting-title-${meeting.id}`}>{meeting.title}</strong>
                            {untitled && <em className="untitled-chip">标题待生成</em>}
                          </span>
                          <span className="archive-row__project">
                            {meeting.project_name ? (
                              <span className="project-mark">
                                <i style={{ background: meeting.project_color || "#64736e" }} />
                                {meeting.project_name}
                              </span>
                            ) : (
                              <span className="muted">未归项目</span>
                            )}
                            <span className="tag-line">
                              {meeting.tags.slice(0, 3).map((tag) => (
                                <em key={tag.id}>{tag.name}</em>
                              ))}
                            </span>
                          </span>
                          <span className="archive-row__duration mono">
                            {formatTime(meeting.duration_ms, true)}
                            <small>{meeting.segment_count ?? 0} 段</small>
                          </span>
                          <span className="archive-row__status">
                            {!done && (
                              <span className={`status-badge status-badge--${statusTone(meeting.status)}`}>
                                {statusLabel(meeting.status)}
                              </span>
                            )}
                          </span>
                        </button>
                        <CopyFolderPathButton
                          describedById={`meeting-title-${meeting.id}`}
                          path={meeting.canonical_dir}
                        />
                      </div>
                    );
                  })}
                </div>
              </section>
            ))}
          </div>
        )}
        {total > limit && (
          <nav aria-label="资料库分页" className="pagination-bar">
            <button
              disabled={offset <= 0}
              onClick={() => onPageChange(Math.max(0, offset - limit))}
              type="button"
            >
              上一页
            </button>
            <span>
              第 {Math.floor(offset / limit) + 1} / {Math.ceil(total / limit)} 页
            </span>
            <button
              disabled={offset + limit >= total}
              onClick={() => onPageChange(offset + limit)}
              type="button"
            >
              下一页
            </button>
          </nav>
        )}
      </section>

      <aside className="filter-drawer">
        <div className="filter-drawer__heading">
          <span className="eyebrow">REFINE</span>
          <h2>精确缩小范围</h2>
        </div>
        <div className="field-pair">
          <label>
            <span>开始日期</span>
            <input
              onChange={(event) => update("date_from", event.target.value)}
              type="date"
              value={filters.date_from ?? ""}
            />
          </label>
          <label>
            <span>结束日期</span>
            <input
              onChange={(event) => update("date_to", event.target.value)}
              type="date"
              value={filters.date_to ?? ""}
            />
          </label>
        </div>
        <label>
          <span>录音时长</span>
          <select
            onChange={(event) => {
              const value = event.target.value;
              if (value === "short") onFilter({ ...filters, min_duration_ms: undefined, max_duration_ms: 30 * 60_000 });
              else if (value === "medium") onFilter({ ...filters, min_duration_ms: 30 * 60_000, max_duration_ms: 60 * 60_000 });
              else if (value === "long") onFilter({ ...filters, min_duration_ms: 60 * 60_000, max_duration_ms: undefined });
              else onFilter({ ...filters, min_duration_ms: undefined, max_duration_ms: undefined });
            }}
            value={
              filters.min_duration_ms === 60 * 60_000
                ? "long"
                : filters.min_duration_ms === 30 * 60_000
                  ? "medium"
                  : filters.max_duration_ms === 30 * 60_000
                    ? "short"
                    : ""
            }
          >
            <option value="">不限时长</option>
            <option value="short">30 分钟以内</option>
            <option value="medium">30–60 分钟</option>
            <option value="long">60 分钟以上</option>
          </select>
        </label>
        <div className="filter-note">
          <strong>搜索口径</strong>
          <p>只检索每场会议的最新工作版本，历史版本仍保留但不混入结果。</p>
        </div>
      </aside>
    </div>
  );
}
