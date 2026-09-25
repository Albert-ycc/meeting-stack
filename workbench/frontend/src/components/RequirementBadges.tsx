import type { RequirementPriority, RequirementStatus } from "../types";
import "./RequirementBadges.css";

export const REQUIREMENT_PRIORITIES: RequirementPriority[] = ["P0", "P1", "P2", "P3"];

export const REQUIREMENT_STATUS_LABELS: Record<RequirementStatus, string> = {
  active: "进行中",
  done: "已完成",
  shelved: "已搁置",
};

/** 优先级只挂在需求上；任务上显示的是所属需求的优先级，没挂需求时显示「—」。 */
export function PriorityBadge({ priority }: { priority?: RequirementPriority | null }) {
  if (!priority) return <span className="priority-badge priority-badge--none">—</span>;
  return (
    <span className={`priority-badge priority-badge--${priority.toLowerCase()}`}>{priority}</span>
  );
}

export function RequirementStatusBadge({ status }: { status: RequirementStatus }) {
  return (
    <span className={`requirement-status requirement-status--${status}`}>
      {REQUIREMENT_STATUS_LABELS[status] ?? status}
    </span>
  );
}
