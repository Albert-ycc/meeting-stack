import { formatMonthDay } from "../../format";
import type { Project } from "../../types";

/** 面板和列表行共用的口径：进行中需求数不含候选；未完成任务沿用 open_task_count */
export function projectFacts(project: Project, meetingTotal: number) {
  const titles = (project.active_requirement_titles ?? []).slice(0, 2);
  const weeks = project.weeks_since_last_meeting;
  return {
    titles,
    requirementCount: project.active_requirement_count ?? project.requirement_counts?.active ?? 0,
    pending: project.pending_candidate_count ?? 0,
    openTasks: project.open_task_count ?? 0,
    meetings: meetingTotal,
    recordingMs: project.recording_ms ?? 0,
    latestTitle: project.latest_meeting_title ?? null,
    latestDate: project.latest_meeting_date ? formatMonthDay(project.latest_meeting_date) : null,
    // 距最近一场会满 3 周才提醒
    staleText: weeks != null && weeks >= 3 ? `已 ${weeks} 周没有会议` : null,
  };
}
