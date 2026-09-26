import { useCallback, useEffect, useMemo, useState } from "react";

import type { ApiClient } from "../api";
import {
  dayStamp,
  formatClock,
  formatDurationText,
  isUntitled,
  statusLabel,
  statusTone,
  serviceStateLabel,
} from "../format";
import type { HealthPayload, Job, MeetingSummary, Task } from "../types";
import { DitherArea, DitherCalendar, type CalendarCell } from "./charts/DitherChart";
import { BlurText } from "./motion/BlurText";
import { CountUp } from "./motion/CountUp";
import { NoticeBanner, useNotice } from "./Notice";

interface OverviewPageProps {
  health: HealthPayload | null;
  jobs: Job[];
  jobsAvailable: boolean;
  jobsInteractive?: boolean;
  meetings: MeetingSummary[];
  onOpenJobs: () => void;
  onOpenLibrary: () => void;
  onOpenMeeting?: (meetingId: string) => void;
  apiClient: ApiClient;
  onOpenTasks: () => void;
  /** 确认待办之后通知外层刷新侧栏「任务池」角标。 */
  onTasksChanged?: () => void;
}

const ATTENTION_KIND_TEXT: Record<string, string> = {
  transcription: "转写失败",
  minutes: "纪要没生成",
  archive: "归档未完成",
  other: "处理中断",
};

/** 按失败阶段说清楚是哪一类问题：转写阶段失败时不能说「纪要会自动重试」。 */
export function describeAttention(byKind: Record<string, number> | null, total: number): string {
  if (!total) return "新录音出现后会显示在这里。";
  const parts = Object.entries(byKind ?? {})
    .filter(([, count]) => count > 0)
    .map(([kind, count]) => `${count} 个${ATTENTION_KIND_TEXT[kind] ?? "异常"}`);
  const head = parts.length ? parts.join("、") : `${total} 个录音处理失败`;
  return `${head}，到资料库「需要处理」里查看。`;
}

const serviceLabels: Record<string, string> = {
  database: "资料索引",
  archive: "正式归档",
  staging: "转写暂存",
  semantic: "语义检索",
  relay: "录音流水线",
  relay_worker: "转写执行器",
  qwen_worker: "Qwen 对照转写",
  scanner: "资料扫描",
  backup: "数据备份",
  process: "服务进程",
};

const RECENT_LIMIT = 6;
// 图表要按天聚合，工作台主列表只加载一页（50 条），跨度盖不住 30 天，
// 缺的日子会被画成「当天没有会议」。所以图表单独取一次更大范围。
const CHART_FETCH_LIMIT = 400;
const CHART_DAYS = 30;
const CHART_WEEKS = 16;
const DAY_MS = 86_400_000;

/** y 轴刻度取整，避免出现 0 / 57 / 114 这种读不出来的数。 */
function niceTicks(max: number): number[] {
  if (max <= 0) return [0, 1, 2, 3];
  const rough = max / 3;
  const step = Math.pow(10, Math.floor(Math.log10(rough)));
  const norm = rough / step;
  const mult = norm < 1.5 ? 1 : norm < 3 ? 2 : norm < 7 ? 5 : 10;
  const s = step * mult;
  return [0, s, s * 2, s * 3];
}
// 工作台只展示最近 5 条待确认，超出引导进任务池。
const PENDING_LIMIT = 5;
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
  apiClient,
  onOpenTasks,
  onTasksChanged,
}: OverviewPageProps) {
  const activeJobs = jobs.filter(
    (job) =>
      !["completed_unreviewed", "draft_modified", "published", "cancelled", "failed", "interrupted"].includes(
        job.state,
      ),
  );
  const now = new Date();
  const { weekCount, weekDuration, recent } = useMemo(() => {
    const weekStart = startOfWeek(now).getTime();
    let week = 0;
    let weekMs = 0;
    for (const meeting of meetings) {
      const time = meeting.recording_date ? new Date(meeting.recording_date).getTime() : NaN;
      if (Number.isNaN(time)) continue;
      if (time >= weekStart) {
        week += 1;
        weekMs += meeting.duration_ms ?? 0;
      }
    }
    return {
      weekCount: week,
      weekDuration: weekMs,
      recent: meetings.slice(0, RECENT_LIMIT),
    };
    // meetings 变化即重算；now 每次渲染新建，不作为依赖。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [meetings]);

  const failedJobs = health?.counts.attention_jobs ?? health?.counts.failed_jobs ?? 0;
  const attentionText = describeAttention(health?.details?.attention?.by_kind ?? null, failedJobs);
  const degradedServices = Object.entries(health?.services ?? {}).filter(
    ([, status]) => !HEALTHY_SERVICE_STATES.has(status),
  );

  const [chartSource, setChartSource] = useState<MeetingSummary[] | null>(null);

  useEffect(() => {
    let alive = true;
    void apiClient
      .meetings({ limit: CHART_FETCH_LIMIT })
      .then((payload) => {
        if (alive) setChartSource(payload.items);
      })
      .catch(() => {
        if (alive) setChartSource([]);
      });
    return () => {
      alive = false;
    };
  }, [apiClient]);

  const chart = useMemo(() => {
    if (!chartSource || chartSource.length === 0) return null;
    const today = new Date();
    today.setHours(0, 0, 0, 0);
    const todayMs = today.getTime();

    const minutes = new Array<number>(CHART_DAYS).fill(0);
    let activeDays = 0;

    // 热力图按 GitHub 那种排法：一列一周，行是周一到周日
    const weekdayFromMonday = (today.getDay() + 6) % 7;
    const thisMonday = todayMs - weekdayFromMonday * DAY_MS;
    const calStart = thisMonday - (CHART_WEEKS - 1) * 7 * DAY_MS;
    const counts = new Array<number>(CHART_WEEKS * 7).fill(0);

    for (const item of chartSource) {
      if (!item.recording_date) continue;
      const day = new Date(item.recording_date);
      if (Number.isNaN(day.getTime())) continue;
      day.setHours(0, 0, 0, 0);
      const dayMs = day.getTime();

      const back = Math.round((todayMs - dayMs) / DAY_MS);
      if (back >= 0 && back < CHART_DAYS) {
        minutes[CHART_DAYS - 1 - back] += (item.duration_ms ?? 0) / 60_000;
      }
      const offset = Math.round((dayMs - calStart) / DAY_MS);
      if (offset >= 0 && offset < counts.length) {
        const col = Math.floor(offset / 7);
        counts[col * 7 + (offset % 7)] += 1;
      }
    }
    for (const v of minutes) if (v > 0) activeDays += 1;

    const stamp = (ms: number) => {
      const d = new Date(ms);
      return `${d.getMonth() + 1}/${d.getDate()} 周${"日一二三四五六"[d.getDay()]}`;
    };
    const shortStamp = (back: number) => {
      const d = new Date(todayMs - back * DAY_MS);
      return `${d.getMonth() + 1}/${d.getDate()}`;
    };

    const peak = Math.max(...counts, 1);
    const cells: CalendarCell[] = counts.map((count, index) => {
      const col = Math.floor(index / 7);
      const dayMs = calStart + (col * 7 + (index % 7)) * DAY_MS;
      return {
        intensity: count === 0 ? 0 : 0.25 + 0.75 * (count / peak),
        label: stamp(dayMs),
        count,
      };
    });

    return {
      minutes,
      cells,
      ticks: niceTicks(Math.max(...minutes)),
      labels: [shortStamp(CHART_DAYS - 1), shortStamp(20), shortStamp(10), shortStamp(0)],
      // 面积图 hover 时要能说出是哪一天，索引 0 是 30 天前
      pointLabels: Array.from({ length: CHART_DAYS }, (_, i) => stamp(todayMs - (CHART_DAYS - 1 - i) * DAY_MS)),
      totalHours: minutes.reduce((a, b) => a + b, 0) / 60,
      activeDays,
    };
  }, [chartSource]);

  const [pendingTasks, setPendingTasks] = useState<Task[]>([]);
  const [pendingTotal, setPendingTotal] = useState(0);
  const [todoState, setTodoState] = useState<"loading" | "ready" | "error">("loading");
  const [confirmBusy, setConfirmBusy] = useState(false);
  const { notice, setNotice, dismissNotice } = useNotice();

  const loadTodos = useCallback(async () => {
    try {
      const payload = await apiClient.tasks({ status: "pending_confirm", limit: PENDING_LIMIT });
      setPendingTasks(payload.items);
      setPendingTotal(payload.total);
      setTodoState("ready");
    } catch {
      setTodoState("error");
    }
  }, [apiClient]);

  useEffect(() => {
    void loadTodos();
  }, [loadTodos]);

  const confirmOne = async (task: Task) => {
    if (confirmBusy) return;
    setConfirmBusy(true);
    setNotice("");
    try {
      await apiClient.confirmTask(task.id, {});
      await loadTodos();
      onTasksChanged?.();
      setNotice("任务已确认");
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "确认失败，请稍后重试", "error");
    } finally {
      setConfirmBusy(false);
    }
  };

  return (
    <section className="overview-page page-content">
      <header className="page-heading overview-heading">
        <div>
          <span className="eyebrow">WORKBENCH / 工作台</span>
          <h1><BlurText text="工作台" /></h1>
          <p>先处理待确认的拍板事项，再扫一眼转写进度；回忆某场会走下面的时间线。</p>
        </div>
        <div className="today-stamp">
          <span>LOCAL TIME</span>
          <strong>
            {new Intl.DateTimeFormat("zh-CN", { month: "2-digit", day: "2-digit" }).format(now)}
          </strong>
        </div>
      </header>

      <div className="metric-strip">
        {jobsInteractive ? (
          <button onClick={onOpenTasks} type="button">
            <span>待确认任务</span>
            <strong><CountUp value={todoState === "loading" ? "…" : pendingTotal} /></strong>
            <small>
              {todoState === "loading"
                ? "读取待办中…"
                : todoState === "error"
                  ? "待办读取失败"
                  : pendingTotal
                    ? `${pendingTotal} 条拍板事项待确认`
                    : "没有待确认的任务"}
            </small>
          </button>
        ) : (
          <div className="metric-static">
            <span>待确认任务</span>
            <strong><CountUp value={todoState === "loading" ? "…" : pendingTotal} /></strong>
            <small>桌面端确认</small>
          </div>
        )}
        {jobsInteractive ? (
          <button disabled={!jobsAvailable} onClick={onOpenJobs} type="button">
            <span>进行中转写</span>
            <strong><CountUp value={jobsAvailable ? activeJobs.length : "—"} /></strong>
            <small>
              {!jobsAvailable
                ? "接口待接入"
                : failedJobs
                  ? `${failedJobs} 个异常 · ${activeJobs.length} 个处理中`
                  : activeJobs.length
                    ? `${activeJobs.length} 个录音处理中`
                    : "没有正在处理的录音"}
            </small>
          </button>
        ) : (
          <div className="metric-static">
            <span>进行中转写</span>
            <strong><CountUp value={jobsAvailable ? activeJobs.length : "—"} /></strong>
            <small>桌面端处理</small>
          </div>
        )}
        <button onClick={onOpenLibrary} type="button">
          <span>本周录音</span>
          <strong><CountUp value={weekCount} /></strong>
          <small>{weekCount ? formatDurationText(weekDuration) : "本周还没有新录音"}</small>
        </button>
        <button onClick={onOpenLibrary} type="button">
          <span>会议档案</span>
          <strong><CountUp value={health?.counts.meetings ?? "—"} /></strong>
          <small>已进入索引</small>
        </button>
      </div>

      {chart && (
        <section className="overview-charts">
          <article className="chart-board">
            <div className="section-heading">
              <div>
                <span className="eyebrow">VOLUME / 录音时长</span>
                <h2>近 30 天归档量</h2>
              </div>
              <div className="chart-readout">
                <strong>{chart.totalHours.toFixed(1)}h</strong>
                <small>{chart.activeDays} 天有录音</small>
              </div>
            </div>
            <div className="chart-stage">
              <DitherArea
                height={198}
                labels={chart.labels}
                pointLabels={chart.pointLabels}
                ticks={chart.ticks}
                unit=" 分钟"
                values={chart.minutes}
              />
            </div>
            <p className="chart-note">纵轴是当天录音分钟数，鼠标移上去看具体某天</p>
          </article>

          <article className="chart-board">
            <div className="section-heading">
              <div>
                <span className="eyebrow">RHYTHM / 归档节奏</span>
                <h2>近 16 周</h2>
              </div>
            </div>
            <div className="chart-stage chart-stage--center">
              <DitherCalendar cell={16} cells={chart.cells} cols={CHART_WEEKS} gap={3} rows={7} unit=" 场" />
            </div>
            <p className="chart-note">一格一天，网点越密当天归档越多，鼠标移上去看场次</p>
          </article>
        </section>
      )}

      <section className="todo-board">
        <div className="section-heading">
          <div>
            <span className="eyebrow">TODO / 待办</span>
            <h2>待办任务</h2>
          </div>
          <button className="text-button text-button--accent" onClick={onOpenTasks} type="button">
            进任务池
          </button>
        </div>

        <NoticeBanner notice={notice} onDismiss={dismissNotice} />

        {todoState === "loading" ? (
          <div className="contract-empty">
            <span>…</span>
            <div>
              <strong>正在读取待办任务</strong>
              <p>从任务台账拉取拍板事项。</p>
            </div>
          </div>
        ) : todoState === "error" ? (
          <div className="contract-empty">
            <span>↯</span>
            <div>
              <strong>待办任务读取失败</strong>
              <p>任务台账暂时不可用；其余功能不受影响。</p>
            </div>
          </div>
        ) : pendingTasks.length === 0 ? (
          <div className="contract-empty">
            <span>✓</span>
            <div>
              <strong>没有待确认的任务</strong>
              <p>会议纪要生成的拍板事项会先在这里等你确认。</p>
            </div>
          </div>
        ) : (
          <ol className="todo-list">
            {pendingTasks.map((task, index) => (
              <li className="todo-item" key={task.id} style={{ "--row-index": index } as React.CSSProperties}>
                <div className="todo-item__body">
                  <strong className="todo-item__title">{task.title}</strong>
                  {task.detail && <p className="todo-item__detail">{task.detail}</p>}
                  <span className="todo-item__meta">
                    {task.project_name && (
                      <>
                        <i
                          aria-hidden="true"
                          className="todo-dot"
                          style={{ background: task.project_color || "#3ecf8e" }}
                        />
                        <span>{task.project_name}</span>
                      </>
                    )}
                    {task.meeting_title && <span className="todo-item__meeting">会议 · {task.meeting_title}</span>}
                  </span>
                </div>
                {jobsInteractive && (
                  <button
                    className="todo-item__confirm"
                    disabled={confirmBusy}
                    onClick={() => void confirmOne(task)}
                    type="button"
                  >
                    确认
                  </button>
                )}
              </li>
            ))}
          </ol>
        )}

        {jobsInteractive && todoState === "ready" && pendingTotal > pendingTasks.length && (
          <div className="todo-board__more">
            <button className="text-button" onClick={onOpenTasks} type="button">
              还有 {pendingTotal - pendingTasks.length} 条，进任务池查看
            </button>
          </div>
        )}
      </section>

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
              {recent.map((meeting, index) => {
                const stamp = dayStamp(meeting.recording_date);
                return (
                  <li key={meeting.id} style={{ "--row-index": index } as React.CSSProperties}>
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
                <p>{attentionText}</p>
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
                    <small>{serviceStateLabel(status)}</small>
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
