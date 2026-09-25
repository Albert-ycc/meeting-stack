import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { TaskDrawer } from "./TaskDrawer";
import type { ApiClient } from "../api";
import type { TaskDetail, TaskStatus } from "../types";

function makeTask(status: TaskStatus): TaskDetail {
  return {
    id: "t1",
    title: "确认样品发放口径",
    detail: "",
    status,
    origin: "ai",
    assignee: "me",
    meeting_id: "m1",
    project_id: null,
    anchor_ms: null,
    anchor_quote: null,
    status_changed_at: "2026-08-20T02:00:00Z",
    created_at: "2026-08-20T02:00:00Z",
    updated_at: "2026-08-20T02:00:00Z",
    meeting_title: "样品项目周会",
    project_name: null,
    project_color: null,
    stall_days: 0,
    stall_since: null,
    stalled: false,
    events: [],
    deliverables: [],
  };
}

function renderDrawer(status: TaskStatus, overrides: Partial<ApiClient> = {}) {
  const apiClient = {
    task: vi.fn().mockResolvedValue(makeTask(status)),
    confirmTask: vi.fn().mockResolvedValue(makeTask("confirmed")),
    setTaskStatus: vi.fn().mockResolvedValue(makeTask("done")),
    updateTask: vi.fn().mockResolvedValue(makeTask(status)),
    ...overrides,
  } as unknown as ApiClient;
  render(
    <TaskDrawer
      apiClient={apiClient}
      canWrite
      onChanged={vi.fn()}
      onClose={vi.fn()}
      onOpenMeeting={vi.fn()}
      taskId="t1"
    />,
  );
  return apiClient;
}

describe("TaskDrawer 页脚主操作按状态切换", () => {
  it("待确认任务主按钮是「确认」而非「标记完成」", async () => {
    const apiClient = renderDrawer("pending_confirm");

    expect(await screen.findByRole("button", { name: "确认" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "标记完成" })).not.toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "确认" }));
    expect(apiClient.confirmTask).toHaveBeenCalledWith("t1", {});
  });

  it("进行中任务主按钮是「标记完成」", async () => {
    const apiClient = renderDrawer("in_progress");

    expect(await screen.findByRole("button", { name: "标记完成" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "确认" })).not.toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "标记完成" }));
    expect(apiClient.setTaskStatus).toHaveBeenCalledWith("t1", "done");
  });

  it("已取消任务（closed）的主操作与取消按钮被禁用", async () => {
    renderDrawer("cancelled");

    expect(await screen.findByRole("button", { name: "转给 AI 执行" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "标记完成" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "取消任务" })).toBeDisabled();
  });

  it("父级写进行中时禁用所有页脚写按钮", async () => {
    const apiClient = {
      task: vi.fn().mockResolvedValue(makeTask("in_progress")),
    } as unknown as ApiClient;
    render(
      <TaskDrawer
        apiClient={apiClient}
        canWrite
        onChanged={vi.fn()}
        onClose={vi.fn()}
        onOpenMeeting={vi.fn()}
        parentBusy
        taskId="t1"
      />,
    );

    expect(await screen.findByRole("button", { name: "标记完成" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "转给 AI 执行" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "取消任务" })).toBeDisabled();
  });
});

describe("TaskDrawer 所属需求", () => {
  it("挂了需求时显示优先级胶囊与可点链接，点击跳转", async () => {
    const onOpenRequirement = vi.fn();
    const apiClient = {
      task: vi.fn().mockResolvedValue({
        ...makeTask("in_progress"),
        requirement_id: "req-1",
        requirement_title: "北辰仓快递配送",
        requirement_priority: "P0",
        requirement_status: "active",
      }),
    } as unknown as ApiClient;
    render(
      <TaskDrawer
        apiClient={apiClient}
        canWrite
        onChanged={vi.fn()}
        onClose={vi.fn()}
        onOpenMeeting={vi.fn()}
        onOpenRequirement={onOpenRequirement}
        taskId="t1"
      />,
    );

    expect(await screen.findByText("P0")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "北辰仓快递配送" }));
    expect(onOpenRequirement).toHaveBeenCalledWith("req-1");
  });

  it("没挂需求时不显示所属需求行", async () => {
    renderDrawer("in_progress");
    await screen.findByText("确认样品发放口径");
    expect(screen.queryByText("P0")).not.toBeInTheDocument();
  });

  it("没传 onOpenRequirement 时需求名不是可点按钮", async () => {
    const apiClient = {
      task: vi.fn().mockResolvedValue({
        ...makeTask("in_progress"),
        requirement_id: "req-1",
        requirement_title: "北辰仓快递配送",
        requirement_priority: "P0",
        requirement_status: "active",
      }),
    } as unknown as ApiClient;
    render(
      <TaskDrawer
        apiClient={apiClient}
        canWrite
        onChanged={vi.fn()}
        onClose={vi.fn()}
        onOpenMeeting={vi.fn()}
        taskId="t1"
      />,
    );

    expect(await screen.findByText("北辰仓快递配送")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "北辰仓快递配送" })).not.toBeInTheDocument();
  });
});
