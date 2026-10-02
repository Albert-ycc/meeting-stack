import { formatMonthDay } from "../../../format";
import type { LinkOption, ProjectWorkRequirement, Task } from "../../../types";

/** 任务面板最多露出的行数，其余折成「还有 N 项」 */
export const PANEL_VISIBLE = 6;

/** 截止是日历日（YYYY-MM-DD）：直接截字符串，不过浏览器时区 */
export function dueText(due: string | null | undefined): string {
  if (!due) return "截止未定";
  return /^\d{4}-\d{2}-\d{2}/.test(due) ? due.slice(5, 10) : formatMonthDay(due);
}

/** 面板标题「任务 N · 未完成 M」，全完成时「任务 N · 已完成 N」 */
export function panelHeading(tasks: Task[]): { total: number; note: string } {
  const open = tasks.filter((task) => task.status !== "done").length;
  if (tasks.length === 0) return { total: 0, note: "" };
  return { total: tasks.length, note: open === 0 ? `已完成 ${tasks.length}` : `未完成 ${open}` };
}

/** 选了需求、候选，或 null（不挂）要写给 updateTask 的字段 */
export function linkBody(option: LinkOption | null) {
  if (option === null) return { requirement_id: null, candidate_id: null };
  return option.kind === "requirement" ? { requirement_id: option.id } : { candidate_id: option.id };
}

/** 撤销挂接：把任务原来挂着的写回去。linked 是挂完后端回的任务：挂上别的项目的需求时后端会把项目一并改掉，
 *  这时项目也要退回去（显式写 null 才会清项目）；挂回原需求时项目跟着需求走，不用另给 */
export function originalLink(task: Task, linked: Pick<Task, "project_id">) {
  const before = task.project_id ?? null;
  const project = linked.project_id !== undefined && linked.project_id !== before ? { project_id: before } : {};
  if (task.requirement_id) return { requirement_id: task.requirement_id };
  if (task.candidate_id) return { candidate_id: task.candidate_id, ...project };
  return { requirement_id: null, candidate_id: null, ...project };
}

/** 拖到某条需求上，等同于在选择器里选了它 */
export function requirementOption(requirement: ProjectWorkRequirement, projectId: string): LinkOption {
  return {
    kind: "requirement",
    id: requirement.id,
    title: requirement.title,
    priority: requirement.priority,
    project_id: projectId,
    project_name: null,
    meeting_id: null,
  };
}
