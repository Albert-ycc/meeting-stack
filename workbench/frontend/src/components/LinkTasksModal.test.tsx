import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { ApiError, type ApiClient } from "../api";
import type { RequirementDetail, Task } from "../types";
import { LinkTasksModal } from "./LinkTasksModal";

const task: Task = {
  id: "task-1",
  title: "自行接管EDC接入并推动上线",
  detail: "",
  status: "pending_confirm",
  origin: "ai",
  assignee: "me",
  meeting_title: "云图 EDC 接入与北辰安排跟进",
  stall_days: 0,
  stalled: false,
  status_changed_at: "2026-09-09T00:00:00Z",
  created_at: "2026-09-09T00:00:00Z",
  updated_at: "2026-09-09T00:00:00Z",
};

function detail(): RequirementDetail {
  return {
    id: "req-1",
    project_id: "project-a",
    project_name: "云图科研用药",
    project_color: "#2c8d83",
    title: "北辰仓快递配送",
    priority: "P0",
    status: "active",
    created_at: "2026-09-07T00:00:00Z",
    updated_at: "2026-09-07T00:00:00Z",
    open_task_count: 1,
    meeting_count: 0,
    latest_meeting_date: null,
    folder_count: 0,
    folders: [],
    meetings: [],
    tasks: [task],
  };
}

describe("LinkTasksModal", () => {
  it("only queries tasks not yet attached to any requirement, defaulting to 待确认+进行中", async () => {
    const tasks = vi.fn().mockResolvedValue({ items: [task], total: 1, limit: 200, offset: 0 });
    render(
      <LinkTasksModal
        apiClient={{ tasks, attachRequirementTasks: vi.fn() } as unknown as ApiClient}
        onCancel={vi.fn()}
        onSaved={vi.fn()}
        projectId="project-a"
        requirementId="req-1"
      />,
    );

    await waitFor(() =>
      expect(tasks).toHaveBeenCalledWith({
        project_id: "project-a",
        requirement_id: "none",
        status: "pending_confirm,confirmed,in_progress",
        q: undefined,
        limit: 200,
      }),
    );
    expect(await screen.findByText("自行接管EDC接入并推动上线")).toBeInTheDocument();
  });

  it("attaches the selected tasks to the requirement on 确定", async () => {
    const tasks = vi.fn().mockResolvedValue({ items: [task], total: 1, limit: 200, offset: 0 });
    const attachRequirementTasks = vi.fn().mockResolvedValue(detail());
    const onSaved = vi.fn();
    render(
      <LinkTasksModal
        apiClient={{ tasks, attachRequirementTasks } as unknown as ApiClient}
        onCancel={vi.fn()}
        onSaved={onSaved}
        projectId="project-a"
        requirementId="req-1"
      />,
    );

    await userEvent.click(await screen.findByRole("checkbox", { name: /自行接管EDC接入并推动上线/ }));
    await userEvent.click(screen.getByRole("button", { name: "确定" }));

    await waitFor(() => expect(attachRequirementTasks).toHaveBeenCalledWith("req-1", ["task-1"]));
    expect(onSaved).toHaveBeenCalled();
  });

  it("D24：批量里有任务已不存在时，就地显示 404 原因，不关弹窗、勾选不丢", async () => {
    const tasks = vi.fn().mockResolvedValue({ items: [task], total: 1, limit: 200, offset: 0 });
    const attachRequirementTasks = vi.fn().mockRejectedValue(new ApiError("任务不存在", 404));
    const onCancel = vi.fn();
    const onSaved = vi.fn();
    render(
      <LinkTasksModal
        apiClient={{ tasks, attachRequirementTasks } as unknown as ApiClient}
        onCancel={onCancel}
        onSaved={onSaved}
        projectId="project-a"
        requirementId="req-1"
      />,
    );

    await userEvent.click(await screen.findByRole("checkbox", { name: /自行接管EDC接入并推动上线/ }));
    await userEvent.click(screen.getByRole("button", { name: "确定" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("任务不存在");
    expect(onCancel).not.toHaveBeenCalled();
    expect(onSaved).not.toHaveBeenCalled();
    expect(screen.getByRole("checkbox", { name: /自行接管EDC接入并推动上线/ })).toBeChecked();
  });
});
