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

describe("TaskDrawer 文件交付物（3g）", () => {
  const base = { task_id: "t1", title: "", note: "", created_at: "2026-09-27T02:00:00Z" };

  function renderWithDeliverables(deliverables: TaskDetail["deliverables"], onOpenPreview?: (fileId: number) => void) {
    const apiClient = {
      task: vi.fn().mockResolvedValue({ ...makeTask("in_progress"), deliverables }),
    } as unknown as ApiClient;
    render(
      <TaskDrawer
        apiClient={apiClient}
        canWrite
        onChanged={vi.fn()}
        onClose={vi.fn()}
        onOpenMeeting={vi.fn()}
        onOpenPreview={onOpenPreview}
        taskId="t1"
      />,
    );
  }

  it("有 file_id 时是按钮，点了打开预览抽屉；找不到时标「找不到这个文件了」", async () => {
    const onOpenPreview = vi.fn();
    renderWithDeliverables(
      [
        { ...base, id: 1, kind: "file", url: "/Volumes/资料盘/云图AI/交付/定稿.pdf", file_id: 42, name: "定稿.pdf", gone: false },
        { ...base, id: 2, kind: "file", url: "/Volumes/资料盘/云图AI/旧稿.docx", file_id: 43, name: "旧稿.docx", gone: true },
        { ...base, id: 3, kind: "link", url: "https://example.com/doc", title: "在线文档" },
      ],
      onOpenPreview,
    );

    await userEvent.click(await screen.findByRole("button", { name: "定稿.pdf" }));
    expect(onOpenPreview).toHaveBeenCalledWith(42);
    expect(screen.getByText("找不到这个文件了")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "在线文档" })).toHaveAttribute("href", "https://example.com/doc");
    expect(screen.queryByRole("link", { name: /定稿/ })).not.toBeInTheDocument();
  });

  it("没有 file_id（旧数据）时显示路径和［复制路径］，不做成打不开的链接", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText } });
    const path = "/Volumes/资料盘/云图AI/交付/定稿.pdf";
    renderWithDeliverables([{ ...base, id: 1, kind: "file", url: path, file_id: null }], vi.fn());

    expect(await screen.findByText(path)).toBeInTheDocument();
    expect(screen.queryByRole("link")).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "复制路径" }));
    expect(writeText).toHaveBeenCalledWith(path);
    expect(await screen.findByText("路径已复制")).toBeInTheDocument();
  });
});
