import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { ApiError, type ApiClient } from "../api";
import type { MeetingSummary, RequirementDetail } from "../types";
import { LinkMeetingsModal } from "./LinkMeetingsModal";

const meetingA: MeetingSummary = {
  id: "vm-1",
  title: "260908 云图需求梳理与北辰科研仓对接",
  status: "published",
  recording_date: "2026-09-08T06:05:00Z",
  duration_ms: 50 * 60_000,
  tags: [],
};

const meetingB: MeetingSummary = {
  id: "vm-2",
  title: "云图 EDC 接入与北辰安排跟进",
  status: "published",
  recording_date: "2026-09-09T05:10:00Z",
  duration_ms: 2 * 60_000,
  tags: [],
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
    open_task_count: 0,
    meeting_count: 2,
    latest_meeting_date: null,
    folder_count: 0,
    folders: [],
    meetings: [],
    tasks: [],
  };
}

describe("LinkMeetingsModal", () => {
  it("lists the project's meetings, pre-checks the linked ones and replaces the set on 确定", async () => {
    const meetings = vi.fn().mockResolvedValue({ items: [meetingA, meetingB], limit: 400, offset: 0, total: 2 });
    const setRequirementMeetings = vi.fn().mockResolvedValue(detail());
    const onSaved = vi.fn();
    render(
      <LinkMeetingsModal
        apiClient={{ meetings, setRequirementMeetings } as unknown as ApiClient}
        onCancel={vi.fn()}
        onSaved={onSaved}
        projectId="project-a"
        requirementId="req-1"
        selectedIds={["vm-1"]}
      />,
    );

    await waitFor(() => expect(meetings).toHaveBeenCalledWith({ project_id: "project-a", limit: 400 }));
    expect(await screen.findByRole("checkbox", { name: /260908 云图需求梳理与北辰科研仓对接/ })).toBeChecked();
    expect(screen.getByText("已选 1 场")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("checkbox", { name: /云图 EDC 接入与北辰安排跟进/ }));
    expect(screen.getByText("已选 2 场")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "确定" }));
    await waitFor(() => expect(setRequirementMeetings).toHaveBeenCalledWith("req-1", ["vm-1", "vm-2"]));
    expect(onSaved).toHaveBeenCalled();
  });

  it("filters the list by title", async () => {
    const meetings = vi.fn().mockResolvedValue({ items: [meetingA, meetingB], limit: 400, offset: 0, total: 2 });
    render(
      <LinkMeetingsModal
        apiClient={{ meetings, setRequirementMeetings: vi.fn() } as unknown as ApiClient}
        onCancel={vi.fn()}
        onSaved={vi.fn()}
        projectId="project-a"
        requirementId="req-1"
        selectedIds={[]}
      />,
    );

    await screen.findByText("260908 云图需求梳理与北辰科研仓对接");
    await userEvent.type(screen.getByLabelText("搜索会议"), "EDC");

    expect(screen.queryByText("260908 云图需求梳理与北辰科研仓对接")).not.toBeInTheDocument();
    expect(screen.getByText("云图 EDC 接入与北辰安排跟进")).toBeInTheDocument();
  });

  it("D24：批量里有会议已不存在时，就地显示 404 原因，不关弹窗、勾选不丢", async () => {
    const meetings = vi.fn().mockResolvedValue({ items: [meetingA, meetingB], limit: 400, offset: 0, total: 2 });
    const setRequirementMeetings = vi
      .fn()
      .mockRejectedValue(new ApiError("需求不存在", 404));
    const onCancel = vi.fn();
    const onSaved = vi.fn();
    render(
      <LinkMeetingsModal
        apiClient={{ meetings, setRequirementMeetings } as unknown as ApiClient}
        onCancel={onCancel}
        onSaved={onSaved}
        projectId="project-a"
        requirementId="req-1"
        selectedIds={["vm-1"]}
      />,
    );

    await screen.findByText("260908 云图需求梳理与北辰科研仓对接");
    await userEvent.click(screen.getByRole("checkbox", { name: /云图 EDC 接入与北辰安排跟进/ }));
    await userEvent.click(screen.getByRole("button", { name: "确定" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("需求不存在");
    expect(onCancel).not.toHaveBeenCalled();
    expect(onSaved).not.toHaveBeenCalled();
    expect(screen.getByRole("checkbox", { name: /260908 云图需求梳理与北辰科研仓对接/ })).toBeChecked();
    expect(screen.getByRole("checkbox", { name: /云图 EDC 接入与北辰安排跟进/ })).toBeChecked();
  });
});
