import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { ApiClient } from "../api";
import type { RequirementDetail } from "../types";
import { MeetingRequirementPicker } from "./MeetingRequirementPicker";

const projects = [{ id: "project-a", name: "云图科研用药", color: "#2c8d83" }];

function requirementsPayload() {
  return {
    items: [
      {
        id: "req-1", project_id: "project-a", project_name: "云图科研用药", project_color: "#2c8d83",
        title: "北辰仓快递配送", priority: "P0" as const, status: "active" as const,
        created_at: "2026-09-07T00:00:00Z", updated_at: "2026-09-07T00:00:00Z",
        open_task_count: 3, meeting_count: 2, latest_meeting_date: "2026-09-09T00:00:00Z", folder_count: 2,
      },
      {
        id: "req-2", project_id: "project-a", project_name: "云图科研用药", project_color: "#2c8d83",
        title: "库存盘点", priority: "P3" as const, status: "active" as const,
        created_at: "2026-09-07T00:00:00Z", updated_at: "2026-09-07T00:00:00Z",
        open_task_count: 1, meeting_count: 1, latest_meeting_date: "2026-09-08T00:00:00Z", folder_count: 1,
      },
    ],
    total: 2,
    limit: 200,
    offset: 0,
    counts: { active: 2, done: 0, shelved: 0, all: 2 },
  };
}

describe("MeetingRequirementPicker", () => {
  it("shows a placeholder and skips loading options while disabled", () => {
    render(
      <MeetingRequirementPicker
        apiClient={{ requirements: vi.fn() } as unknown as ApiClient}
        disabled
        onChange={vi.fn()}
        projectId=""
        projects={projects}
        selected={[]}
      />,
    );
    expect(screen.getByText("先选择主项目")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "＋ 关联需求" })).not.toBeInTheDocument();
  });

  it("QA BUG-A03：主项目为空但已关联需求时，胶囊照常展示，只是不能再编辑", () => {
    render(
      <MeetingRequirementPicker
        apiClient={{ requirements: vi.fn() } as unknown as ApiClient}
        disabled
        onChange={vi.fn()}
        projectId=""
        projects={projects}
        selected={[{ id: "req-1", title: "北辰仓快递配送", priority: "P0", status: "active", project_id: "project-a" }]}
      />,
    );
    // D9：后端不校验会议主项目和需求所属项目一致，主项目清空不该把已挂的需求从界面里藏起来。
    expect(screen.getByRole("button", { name: /北辰仓快递配送/ })).toBeInTheDocument();
    expect(screen.queryByText("先选择主项目")).not.toBeInTheDocument();
    // 只读：编辑/新增入口不出现，不能借着这个控件改归属。
    expect(screen.queryByRole("button", { name: "＋ 关联需求" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "编辑" })).not.toBeInTheDocument();
  });

  it("filters the dropdown list by name and only commits the selection on 确定", async () => {
    const requirements = vi.fn().mockResolvedValue(requirementsPayload());
    const onChange = vi.fn();
    render(
      <MeetingRequirementPicker
        apiClient={{ requirements } as unknown as ApiClient}
        onChange={onChange}
        projectId="project-a"
        projects={projects}
        selected={[]}
      />,
    );

    await userEvent.click(screen.getByRole("button", { name: "＋ 关联需求" }));
    await waitFor(() => expect(requirements).toHaveBeenCalledWith({ project_id: "project-a", status: "active", limit: 200 }));
    await screen.findByText("库存盘点");

    await userEvent.type(screen.getByLabelText("搜索需求"), "北辰");
    expect(screen.queryByText("库存盘点")).not.toBeInTheDocument();

    await userEvent.click(screen.getByRole("checkbox", { name: /北辰仓快递配送/ }));
    expect(onChange).not.toHaveBeenCalled();

    await userEvent.click(screen.getByRole("button", { name: "确定" }));
    expect(onChange).toHaveBeenCalledWith([
      { id: "req-1", title: "北辰仓快递配送", priority: "P0", status: "active", project_id: "project-a" },
    ]);
  });

  it("creates a requirement inline and folds it into the current selection", async () => {
    const requirements = vi.fn().mockResolvedValue(requirementsPayload());
    const created: RequirementDetail = {
      id: "req-3",
      project_id: "project-a",
      project_name: "云图科研用药",
      project_color: "#2c8d83",
      title: "老患者超期申领拦截",
      priority: "P1",
      status: "active",
      created_at: "2026-09-15T00:00:00Z",
      updated_at: "2026-09-15T00:00:00Z",
      open_task_count: 0,
      meeting_count: 0,
      latest_meeting_date: null,
      folder_count: 0,
      folders: [],
      meetings: [],
      tasks: [],
    };
    const createRequirement = vi.fn().mockResolvedValue(created);
    const onChange = vi.fn();
    render(
      <MeetingRequirementPicker
        apiClient={{ requirements, createRequirement } as unknown as ApiClient}
        onChange={onChange}
        projectId="project-a"
        projects={projects}
        selected={[]}
      />,
    );

    await userEvent.click(screen.getByRole("button", { name: "＋ 关联需求" }));
    await userEvent.click(await screen.findByRole("button", { name: "＋ 新建需求" }));
    expect(await screen.findByRole("dialog", { name: "新建需求" })).toBeInTheDocument();

    await userEvent.type(screen.getByPlaceholderText("例如：北辰仓快递配送"), "老患者超期申领拦截");
    await userEvent.click(screen.getByRole("button", { name: "创建" }));

    await waitFor(() =>
      expect(onChange).toHaveBeenCalledWith([
        { id: "req-3", title: "老患者超期申领拦截", priority: "P1", status: "active", project_id: "project-a" },
      ]),
    );
  });
});
