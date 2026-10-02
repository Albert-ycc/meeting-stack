import { act, render, screen, within } from "@testing-library/react";
import { describe, expect, it, onTestFinished, vi } from "vitest";

import type { ApiClient } from "../api";
import type { Job, MeetingSummary, Task } from "../types";
import { OverviewPage, chartDayLabels, materialTagText } from "./OverviewPage";

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

  it("treats semantic search pausing for a meeting transcription as healthy", async () => {
    render(
      <OverviewPage
        {...baseProps({
          health: {
            status: "ok",
            services: { database: "healthy", semantic: "paused" },
            counts: { meetings: 0, unreviewed: 0, failed_jobs: 0, scan_errors: 0 },
          },
        })}
      />,
    );
    await act(async () => {});
    expect(screen.getByText("本地服务全部正常")).toBeInTheDocument();
  });

  it("本周录音按录音日期数本周的场次和总时长，上周的不算", async () => {
    vi.useFakeTimers({ toFake: ["requestAnimationFrame", "cancelAnimationFrame", "performance"] });
    onTestFinished(() => {
      vi.useRealTimers();
    });
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
            {
              id: "vm-0",
              title: "上上周的会",
              // 8 天前一定早于本周一零点
              recording_date: new Date(Date.now() - 8 * 86_400_000).toISOString(),
              duration_ms: 1_800_000,
              status: "completed_unreviewed",
              tags: [],
            },
          ],
        })}
      />,
    );
    await act(async () => {});

    const week = screen.getByText("本周录音").closest("button")!;
    expect(within(week).getByText("1 小时 10 分钟")).toBeInTheDocument();
    // 数字从 0 滚上来（1.1 秒的动画），把动画时钟拨到头再看，不靠真等
    act(() => vi.advanceTimersByTime(2_000));
    expect(week.querySelector("strong")).toHaveTextContent(/^2$/);
    expect(screen.getByText("本地服务全部正常")).toBeInTheDocument();
    expect(screen.getByText("协会MDT需求评审")).toBeInTheDocument();
    expect(screen.getAllByText("标题待生成").length).toBeGreaterThan(0);
  });
});

describe("OverviewPage 图表日期", () => {
  it("跨过夏令时切换的那几周，悬停日期和星期不差一天", () => {
    // 本机在太平洋时区；2026-03-08 切夏令时，那天只有 23 小时
    vi.stubEnv("TZ", "America/Los_Angeles");
    try {
      const days = chartDayLabels(new Date(2026, 2, 20));
      expect(days.points[0]).toBe("2/19 周四");
      expect(days.points.slice(16, 18)).toEqual(["3/7 周六", "3/8 周日"]);
      expect(days.points[29]).toBe("3/20 周五");
      expect(days.cells[0]).toBe("12/1 周一");
      expect(days.cells[96]).toBe("3/7 周六");
      expect(days.cells[111]).toBe("3/22 周日");
      expect(days.axis).toEqual(["2/19", "2/28", "3/10", "3/20"]);
    } finally {
      vi.unstubAllEnvs();
    }
  });
});

describe("OverviewPage 材料小标签（3e）", () => {
  const withMaterials = (pending: number, paused: "busy" | null, offlinePending: number) => ({
    status: "ok" as const,
    services: { database: "healthy", semantic: "paused" },
    counts: { meetings: 0, unreviewed: 0, failed_jobs: 0, scan_errors: 0, material_pending: pending },
    details: { materials: { pending, paused, offline_pending: offlinePending } },
  });

  it("三种说法：还剩多少、转写会议时先停、剩下的都在没插的盘上", () => {
    expect(materialTagText(withMaterials(210, null, 0))).toBe("材料 还剩 210 个");
    expect(materialTagText(withMaterials(1234, "busy", 0))).toBe("材料 还剩 1,234 个 · 转写会议时先停");
    expect(materialTagText(withMaterials(210, null, 210))).toBe("材料 还剩 210 个 · 资料盘未连接");
    expect(materialTagText(withMaterials(210, null, 12))).toBe("材料 还剩 210 个");
  });

  it("0 个、旧后端没有这个字段时不显示", () => {
    expect(materialTagText(withMaterials(0, null, 0))).toBeNull();
    expect(materialTagText(baseProps().health)).toBeNull();
    expect(materialTagText(null)).toBeNull();
  });

  it("灰色小标签挂在本地服务那行下面，不能点；语义检索让路暂停仍写全部正常", async () => {
    render(<OverviewPage {...baseProps({ health: withMaterials(210, "busy", 0) })} />);
    await act(async () => {});
    const tag = screen.getByText("材料 还剩 210 个 · 转写会议时先停");
    expect(tag.tagName).toBe("P");
    expect(tag.closest("button")).toBeNull();
    expect(screen.getByText("本地服务全部正常")).toBeInTheDocument();
  });

  it("没有材料要读时没有这个标签", async () => {
    render(<OverviewPage {...baseProps()} />);
    await act(async () => {});
    expect(document.querySelector(".material-tag")).toBeNull();
  });
});
