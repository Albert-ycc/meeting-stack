import { act, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { ApiClient } from "../api";
import type { Job, MeetingSummary, Task } from "../types";
import { OverviewPage } from "./OverviewPage";

const pendingTask: Task = {
  id: "task-1",
  title: "给王老师回邮件确认模板",
  detail: "下周一前发出",
  status: "pending_confirm",
  origin: "ai",
  assignee: "me",
  meeting_id: "vm-1",
  meeting_title: "协会MDT需求评审",
  project_id: "p-1",
  project_name: "MDT",
  project_color: "#2c8d83",
  stall_days: 0,
  stalled: false,
  status_changed_at: new Date().toISOString(),
  created_at: new Date().toISOString(),
  updated_at: new Date().toISOString(),
};

function apiClient(items: Task[] = [], meetings: MeetingSummary[] = []): ApiClient {
  return {
    tasks: vi.fn().mockResolvedValue({ items, total: items.length, limit: 5, offset: 0 }),
    confirmTask: vi.fn().mockResolvedValue({}),
    // 工作台的抖动图表按天聚合，主列表只有一页盖不住 30 天，所以它自己再取一次
    meetings: vi.fn().mockResolvedValue({
      items: meetings,
      total: meetings.length,
      limit: 400,
      offset: 0,
    }),
  } as unknown as ApiClient;
}

function baseProps(overrides: Partial<Parameters<typeof OverviewPage>[0]> = {}) {
  return {
    health: {
      status: "ok" as const,
      services: {},
      counts: { meetings: 1, unreviewed: 0, failed_jobs: 0, scan_errors: 0 },
    },
    jobs: [] as Job[],
    jobsAvailable: true,
    meetings: [] as MeetingSummary[],
    onOpenJobs: vi.fn(),
    onOpenLibrary: vi.fn(),
    onOpenTasks: vi.fn(),
    apiClient: apiClient(),
    ...overrides,
  };
}

describe("OverviewPage mobile safety", () => {
  it("renders task metrics as static information on mobile", async () => {
    const onOpenJobs = vi.fn();
    const onOpenTasks = vi.fn();
    render(
      <OverviewPage
        {...baseProps({
          jobsInteractive: false,
          onOpenJobs,
          onOpenTasks,
          apiClient: apiClient([pendingTask]),
        })}
      />,
    );
    await act(async () => {});

    // 待确认任务卡在移动端是静态信息，不是按钮
    expect(screen.getByText("待确认任务").closest("button")).toBeNull();
    // 待办列表仍展示，但确认按钮不渲染（只读）
    expect(screen.getByText("给王老师回邮件确认模板")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "确认" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "查看全部" })).not.toBeInTheDocument();
    expect(onOpenJobs).not.toHaveBeenCalled();
    expect(onOpenTasks).not.toHaveBeenCalled();
  });

  it("summarises recent meetings by recording date", async () => {
    render(
      <OverviewPage
        {...baseProps({
          health: {
            status: "ok",
            services: { database: "healthy", semantic: "ready" },
            counts: { meetings: 2, unreviewed: 0, failed_jobs: 0, scan_errors: 0 },
          },
          meetings: [
            {
              id: "vm-1",
              title: "协会MDT需求评审",
              recording_date: new Date().toISOString(),
              duration_ms: 3_600_000,
              status: "completed_unreviewed",
              tags: [],
            },
            {
              id: "vm-2",
              title: "vm-2",
              recording_date: new Date().toISOString(),
              duration_ms: 600_000,
              status: "completed_unreviewed",
              tags: [],
            },
          ],
        })}
      />,
    );
    await act(async () => {});

    expect(screen.getByText("本周录音")).toBeInTheDocument();
    expect(screen.getByText("本地服务全部正常")).toBeInTheDocument();
    expect(screen.getByText("协会MDT需求评审")).toBeInTheDocument();
    expect(screen.getAllByText("标题待生成").length).toBeGreaterThan(0);
  });
});
