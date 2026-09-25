import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { ApiClient } from "../api";
import type { RequirementDetail } from "../types";
import { RequirementDetailPage } from "./RequirementDetailPage";

function baseDetail(overrides: Partial<RequirementDetail> = {}): RequirementDetail {
  return {
    id: "req-1",
    project_id: "project-a",
    project_name: "云图科研用药",
    project_color: "#2c8d83",
    title: "北辰仓快递配送",
    priority: "P0",
    status: "active",
    created_at: "2026-09-07T12:00:00Z",
    updated_at: "2026-09-07T12:00:00Z",
    open_task_count: 3,
    meeting_count: 2,
    latest_meeting_date: "2026-09-09T12:00:00Z",
    folder_count: 2,
    meetings: [
      {
        id: "vm-1",
        title: "260908 云图需求梳理与北辰科研仓对接",
        recording_date: "2026-09-08T13:05:00Z",
        duration_ms: 50 * 60_000,
        canonical_dir: "/Volumes/资料盘/会议纪要与录音/260908 云图需求梳理与北辰科研仓对接",
      },
    ],
    folders: [
      {
        id: 1,
        name: "V1.5.7-北辰仓快递配送-260914",
        path: "/Volumes/资料盘/蓝鲸云/云图科研用药/V1.5.7-北辰仓快递配送-260914",
        exists: true,
        file_count: 91,
        file_count_capped: false,
        modified_at: "2026-09-14T00:00:00Z",
        preview_files: [
          { relative_path: "README.md", size_bytes: 2500, modified_at: "2026-09-14T00:00:00Z" },
        ],
      },
    ],
    tasks: [
      {
        id: "task-1",
        title: "与北辰拉会对齐科研仓对接工作量与排期",
        detail: "",
        status: "in_progress",
        origin: "ai",
        assignee: "me",
        meeting_title: "260908 云图需求梳理与北辰科研仓对接",
        stall_days: 7,
        stalled: true,
        status_changed_at: "2026-09-08T00:00:00Z",
        created_at: "2026-09-08T00:00:00Z",
        updated_at: "2026-09-08T00:00:00Z",
      },
    ],
    ...overrides,
  };
}

beforeEach(() => {
  Object.assign(navigator, { clipboard: { writeText: vi.fn().mockResolvedValue(undefined) } });
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("RequirementDetailPage", () => {
  it("renders the header, and the meeting/folder/task cards from the loaded detail", async () => {
    const requirement = vi.fn().mockResolvedValue(baseDetail());
    render(
      <RequirementDetailPage
        apiClient={{ requirement } as unknown as ApiClient}
        canPickFolders
        canWrite
        onBack={vi.fn()}
        onOpenMeeting={vi.fn()}
        onOpenProject={vi.fn()}
        onOpenTask={vi.fn()}
        projects={[{ id: "project-a", name: "云图科研用药", color: "#2c8d83" }]}
        requirementId="req-1"
      />,
    );

    expect(await screen.findByRole("heading", { name: "北辰仓快递配送" })).toBeInTheDocument();
    expect(screen.getByText("创建于 09-07")).toBeInTheDocument();
    // 会议标题在「关联会议」表和任务行「来源会议」列都会出现，两处都得有
    expect(screen.getAllByText("260908 云图需求梳理与北辰科研仓对接")).toHaveLength(2);
    expect(screen.getByText("91 个文件")).toBeInTheDocument();
    expect(screen.getByText("与北辰拉会对齐科研仓对接工作量与排期")).toBeInTheDocument();
    expect(screen.getByText("停滞 7 天")).toBeInTheDocument();
  });

  it("copies the consolidated material list (meeting folders + material folders)", async () => {
    const requirement = vi.fn().mockResolvedValue(baseDetail());
    render(
      <RequirementDetailPage
        apiClient={{ requirement } as unknown as ApiClient}
        canPickFolders
        canWrite
        onBack={vi.fn()}
        onOpenMeeting={vi.fn()}
        onOpenProject={vi.fn()}
        onOpenTask={vi.fn()}
        projects={[]}
        requirementId="req-1"
      />,
    );

    await screen.findByRole("heading", { name: "北辰仓快递配送" });
    await userEvent.click(screen.getByRole("button", { name: "复制材料清单" }));

    expect(navigator.clipboard.writeText).toHaveBeenCalledWith(
      [
        "/Volumes/资料盘/会议纪要与录音/260908 云图需求梳理与北辰科研仓对接",
        "/Volumes/资料盘/蓝鲸云/云图科研用药/V1.5.7-北辰仓快递配送-260914",
      ].join("\n"),
    );
    expect(await screen.findByText("已复制 2 条路径")).toBeInTheDocument();
  });

  it("D23：一条可复制的路径都没有时，「复制材料清单」置灰", async () => {
    const requirement = vi.fn().mockResolvedValue(baseDetail({ meetings: [], folders: [] }));
    render(
      <RequirementDetailPage
        apiClient={{ requirement } as unknown as ApiClient}
        canPickFolders
        canWrite
        onBack={vi.fn()}
        onOpenMeeting={vi.fn()}
        onOpenProject={vi.fn()}
        onOpenTask={vi.fn()}
        projects={[]}
        requirementId="req-1"
      />,
    );

    await screen.findByRole("heading", { name: "北辰仓快递配送" });
    expect(screen.getByRole("button", { name: "复制材料清单" })).toBeDisabled();
  });

  it("D23：关联会议没有 canonical_dir 时也算没有可复制路径", async () => {
    const requirement = vi.fn().mockResolvedValue(
      baseDetail({
        meetings: [
          { id: "vm-1", title: "还没落地的会议", recording_date: null, duration_ms: null, canonical_dir: null },
        ],
        folders: [],
      }),
    );
    render(
      <RequirementDetailPage
        apiClient={{ requirement } as unknown as ApiClient}
        canPickFolders
        canWrite
        onBack={vi.fn()}
        onOpenMeeting={vi.fn()}
        onOpenProject={vi.fn()}
        onOpenTask={vi.fn()}
        projects={[]}
        requirementId="req-1"
      />,
    );

    await screen.findByRole("heading", { name: "北辰仓快递配送" });
    expect(screen.getByRole("button", { name: "复制材料清单" })).toBeDisabled();
  });

  it("expands a folder's full file list on 查看全部, capped at 2000", async () => {
    const requirement = vi.fn().mockResolvedValue(baseDetail());
    const requirementFolderFiles = vi.fn().mockResolvedValue({
      folder_id: 1,
      path: "/Volumes/资料盘/蓝鲸云/云图科研用药/V1.5.7-北辰仓快递配送-260914",
      exists: true,
      total: 91,
      capped: false,
      items: [
        { relative_path: "README.md", size_bytes: 2500, modified_at: "2026-09-14T00:00:00Z" },
        { relative_path: "考据/01-北辰物流开放平台接口字段事实.md", size_bytes: 25_000, modified_at: "2026-09-14T00:00:00Z" },
      ],
    });
    render(
      <RequirementDetailPage
        apiClient={{ requirement, requirementFolderFiles } as unknown as ApiClient}
        canPickFolders
        canWrite
        onBack={vi.fn()}
        onOpenMeeting={vi.fn()}
        onOpenProject={vi.fn()}
        onOpenTask={vi.fn()}
        projects={[]}
        requirementId="req-1"
      />,
    );

    await userEvent.click(await screen.findByRole("button", { name: /查看全部 91 个文件/ }));
    expect(requirementFolderFiles).toHaveBeenCalledWith("req-1", 1);
    expect(await screen.findByText("考据/01-北辰物流开放平台接口字段事实.md")).toBeInTheDocument();
  });

  it("removes a linked meeting and a material folder without asking for confirmation", async () => {
    const requirement = vi.fn().mockResolvedValue(baseDetail());
    const removeRequirementMeeting = vi.fn().mockResolvedValue(baseDetail({ meetings: [] }));
    const removeRequirementFolder = vi.fn().mockResolvedValue(baseDetail({ folders: [] }));
    const confirmSpy = vi.spyOn(window, "confirm");
    render(
      <RequirementDetailPage
        apiClient={{ requirement, removeRequirementMeeting, removeRequirementFolder } as unknown as ApiClient}
        canPickFolders
        canWrite
        onBack={vi.fn()}
        onOpenMeeting={vi.fn()}
        onOpenProject={vi.fn()}
        onOpenTask={vi.fn()}
        projects={[]}
        requirementId="req-1"
      />,
    );

    await screen.findByRole("heading", { name: "北辰仓快递配送" });
    await userEvent.click(screen.getAllByRole("button", { name: "移除" })[0]);
    await waitFor(() => expect(removeRequirementMeeting).toHaveBeenCalledWith("req-1", "vm-1"));
    expect(confirmSpy).not.toHaveBeenCalled();
  });
});
