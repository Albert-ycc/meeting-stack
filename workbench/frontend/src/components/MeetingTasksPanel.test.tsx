import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { MeetingTasksPanel } from "./MeetingTasksPanel";
import type { ApiClient } from "../api";
import type { Task, TaskStatus } from "../types";

function makeTask(id: string, status: TaskStatus, title: string): Task {
  return {
    id,
    title,
    detail: "",
    status,
    origin: "ai",
    assignee: "me",
    meeting_id: "vm-1",
    project_id: null,
    anchor_ms: null,
    anchor_quote: null,
    status_changed_at: "2026-09-01T02:00:00Z",
    created_at: "2026-09-01T02:00:00Z",
    updated_at: "2026-09-01T02:00:00Z",
    meeting_title: "明德周会",
    project_name: null,
    project_color: null,
    stall_days: 0,
    stall_since: null,
    stalled: false,
  };
}

function renderPanel(tasks: Task[], projects = [{ id: "p-1", name: "明德基金会科普同行", color: "#2c8d83" }]) {
  const apiClient = {
    tasks: vi.fn().mockResolvedValue({ items: tasks, total: tasks.length, limit: 100, offset: 0 }),
    confirmTask: vi.fn(),
    rejectTask: vi.fn(),
  } as unknown as ApiClient;
  render(
    <MeetingTasksPanel
      apiClient={apiClient}
      canWrite
      meetingId="vm-1"
      meetingTitle="明德周会"
      onOpenTasks={vi.fn()}
      projects={projects}
    />,
  );
}

describe("会议详情的本场任务", () => {
  it("已过期的草稿归进灰色分组，不会从会议里消失", async () => {
    renderPanel([makeTask("t-1", "pending_confirm", "新草稿"), makeTask("t-2", "expired", "两周前的草稿")]);

    const row = (await screen.findByText("两周前的草稿")).closest("li")!;
    expect(within(row).getByText("已过期")).toBeInTheDocument();
    expect(screen.getByText("已取消 · 已过期")).toBeInTheDocument();
  });

  it("修改任务弹窗能选到项目，不再是空下拉", async () => {
    renderPanel([makeTask("t-1", "pending_confirm", "新草稿")]);

    await userEvent.click(await screen.findByRole("button", { name: "修改" }));
    // 所属项目是自定义下拉：先点开触发按钮，选项才渲染
    await userEvent.click(await screen.findByRole("button", { name: /未归项目/ }));

    expect(await screen.findByRole("option", { name: "明德基金会科普同行" })).toBeInTheDocument();
  });
});
