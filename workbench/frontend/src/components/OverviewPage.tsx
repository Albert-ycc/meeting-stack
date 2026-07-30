import { useMemo } from "react";

import {
  dayStamp,
  formatClock,
  formatDurationText,
  isUntitled,
  statusLabel,
  statusTone,
} from "../format";
import type { HealthPayload, Job, MeetingSummary } from "../types";

interface OverviewPageProps {
  health: HealthPayload | null;
  jobs: Job[];
  jobsAvailable: boolean;
  jobsInteractive?: boolean;
  meetings: MeetingSummary[];
  onOpenJobs: () => void;
  onOpenLibrary: () => void;
  onOpenMeeting?: (meetingId: string) => void;
}

const serviceLabels: Record<string, string> = {
  database: "资料索引",
  archive: "正式归档",
  staging: "转写暂存",
  semantic: "语义检索",
  relay: "录音流水线",
  scanner: "资料扫描",
};

const RECENT_LIMIT = 6;
// 后端各服务的正常取值不统一，只有落在这个集合外的才值得占版面。
const HEALTHY_SERVICE_STATES = new Set(["healthy", "ok", "ready", "enabled"]);

function startOfWeek(reference: Date): Date {
  const start = new Date(reference.getFullYear(), reference.getMonth(), reference.getDate());
  const weekdayFromMonday = (start.getDay() + 6) % 7;
  start.setDate(start.getDate() - weekdayFromMonday);
  return start;
}

export function OverviewPage({
  health,
  jobs,
  jobsAvailable,
  jobsInteractive = true,
  meetings,
  onOpenJobs,
  onOpenLibrary,
  onOpenMeeting,
}: OverviewPageProps) {
  const activeJobs = jobs.filter(
    (job) =>
      !["completed_unreviewed", "draft_modified", "published", "cancelled", "failed", "interrupted"].includes(
        job.state,
      ),
  );
  const now = new Date();
  const { weekCount, weekDuration, monthCount, recent } = useMemo(() => {
    const weekStart = startOfWeek(now).getTime();
    const monthStart = new Date(now.getFullYear(), now.getMonth(), 1).getTime();
    let week = 0;
    let weekMs = 0;
    let month = 0;
    for (const meeting of meetings) {
      const time = meeting.recording_date ? new Date(meeting.recording_date).getTime() : NaN;
      if (Number.isNaN(time)) continue;
      if (time >= weekStart) {
        week += 1;
        weekMs += meeting.duration_ms ?? 0;
      }
      if (time >= monthStart) month += 1;
    }
    return {
      weekCount: week,
      weekDuration: weekMs,
      monthCount: month,
      recent: meetings.slice(0, RECENT_LIMIT),
    };
    // meetings 变化即重算；now 每次渲染新建，不作为依赖。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [meetings]);

  const failedJobs = health?.counts.failed_jobs ?? 0;
  const degradedServices = Object.entries(health?.services ?? {}).filter(
    ([, status]) => !HEALTHY_SERVICE_STATES.has(status),
  );

  return (
    <section className="overview-page page-content">
      <header className="page-heading overview-heading">
        <div>
          <span className="eyebrow">RECENT / 最近</span>
          <h1>最近的录音</h1>
          <p>按时间线回忆某场会；需要翻更早的记录就进资料库。</p>
        </div>
        <div className="today-stamp">
          <span>LOCAL TIME</span>
          <strong>
            {new Intl.DateTimeFormat("zh-CN", { month: "2-digit", day: "2-digit" }).format(now)}
          </strong>
        </div>
      </header>

      <div className="metric-strip">
        <button onClick={onOpenLibrary} type="button">
          <span>本周录音</span>
          <strong>{weekCount}</strong>
          <small>{weekCount ? formatDurationText(weekDuration) : "本周还没有新录音"}</small>
        </button>
        <button onClick={onOpenLibrary} type="button">
          <span>本月录音</span>
          <strong>{monthCount}</strong>
          <small>按录音日期统计</small>
        </button>
        <button onClick={onOpenLibrary} type="button">
          <span>会议档案</span>
          <strong>{health?.counts.meetings ?? "—"}</strong>
          <small>已进入索引</small>
        </button>
        {jobsInteractive ? (
          <button disabled={!jobsAvailable} onClick={onOpenJobs} type="button">
            <span>需要处理</span>
            <strong>{jobsAvailable ? failedJobs + activeJobs.length : "—"}</strong>
            <small>
              {!jobsAvailable
                ? "接口待接入"
                : failedJobs
                  ? `${failedJobs} 个异常 · ${activeJobs.length} 个处理中`
                  : activeJobs.length
                    ? `${activeJobs.length} 个处理中`
                    : "没有待办"}
            </small>
          </button>
        ) : (
          <div className="metric-static">
            <span>需要处理</span>
            <strong>{jobsAvailable ? failedJobs + activeJobs.length : "—"}</strong>
            <small>桌面端处理</small>
          </div>
        )}
      </div>

      <div className="overview-columns overview-columns--recent">
        <section className="recent-board">
          <div className="section-heading">
            <div>
              <span className="eyebrow">TIMELINE</span>
              <h2>最近几场会</h2>
            </div>
            <button className="text-button" onClick={onOpenLibrary} type="button">
              进资料库
            </button>
          </div>
          {recent.length === 0 ? (
            <div className="contract-empty">
              <span>∅</span>
              <div>
                <strong>还没有录音进入资料库</strong>
                <p>新录音会在转写完成后自动出现在这里。</p>
              </div>
            </div>
          ) : (
            <ol className="recent-list">
              {recent.map((meeting) => {
                const stamp = dayStamp(meeting.recording_date);
                return (
                  <li key={meeting.id}>
                    <button onClick={() => onOpenMeeting?.(meeting.id)} type="button">
                      <span className="recent-list__when">
                        <strong>{stamp.relative ?? stamp.monthDay}</strong>
                        <small className="mono">{formatClock(meeting.recording_date)}</small>
                      </span>
                      <span className="recent-list__title">
                        {meeting.title}
                        {isUntitled(meeting.title, meeting.id) && (
                          <em className="untitled-chip">标题待生成</em>
                        )}
                      </span>
                      <span className="recent-list__duration mono">
                        {formatDurationText(meeting.duration_ms)}
                      </span>
                    </button>
                  </li>
                );
              })}
            </ol>
          )}
        </section>

        <section className="pipeline-board">
          <div className="section-heading">
            <div>
              <span className="eyebrow">PIPELINE</span>
              <h2>正在处理</h2>
            </div>
            {jobsAvailable && jobsInteractive && (
              <button className="text-button" onClick={onOpenJobs} type="button">查看全部</button>
            )}
          </div>
          {!jobsAvailable ? (
            <div className="contract-empty">
              <span>↯</span>
              <div>
                <strong>任务控制接口尚未开放</strong>
                <p>资料库可正常使用；这里不会根据数据库推测或伪造任务成功状态。</p>
              </div>
            </div>
          ) : activeJobs.length === 0 ? (
            <div className="contract-empty">
              <span>✓</span>
              <div>
                <strong>没有正在处理的录音</strong>
                <p>{failedJobs ? `有 ${failedJobs} 个任务失败，纪要会自动重试一次。` : "新录音出现后会显示在这里。"}</p>
              </div>
            </div>
          ) : (
            <div className="mini-job-list">
              {activeJobs.slice(0, 5).map((job) => (
                <div key={job.id}>
                  <span className={`job-pulse job-pulse--${job.state}`} />
                  <code>{job.id}</code>
                  <strong>{statusLabel(statusTone(job.state))}</strong>
                </div>
              ))}
            </div>
          )}
          <div className="service-summary">
            {!health ? (
              <span className="muted">等待后端健康回执…</span>
            ) : degradedServices.length === 0 ? (
              <span className="service-summary__ok">本地服务全部正常</span>
            ) : (
              <ul>
                {degradedServices.map(([name, status]) => (
                  <li key={name}>
                    <span className={`service-light service-light--${status}`} />
                    {serviceLabels[name] ?? name}
                    <small>{status}</small>
                  </li>
                ))}
              </ul>
            )}
          </div>
        </section>
      </div>
    </section>
  );
}
