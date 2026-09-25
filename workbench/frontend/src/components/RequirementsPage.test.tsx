import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { ApiClient } from "../api";
import type { RequirementsPayload, RequirementSummary } from "../types";
import { RequirementsPage } from "./RequirementsPage";

const summary: RequirementSummary = {
  id: "req-1",
  project_id: "project-a",
  project_name: "云图科研用药",
  project_color: "#2c8d83",
  title: "北辰仓快递配送",
  priority: "P0",
  status: "active",
  created_at: "2026-09-07T00:00:00Z",
  updated_at: "2026-09-07T00:00:00Z",
  open_task_count: 3,
  meeting_count: 2,
  latest_meeting_date: "2026-09-09T00:00:00Z",
  folder_count: 2,
};

function payload(overrides: Partial<RequirementsPayload> = {}): RequirementsPayload {
  return {
    items: [summary],
    total: 1,
    limit: 10,
    offset: 0,
    counts: { active: 1, done: 0, shelved: 0, all: 1 },
    ...overrides,
  };
}

describe("RequirementsPage", () => {
  it("loads the active tab by default and lists the requirement's columns", async () => {
    const requirements = vi.fn().mockResolvedValue(payload());
    render(
      <RequirementsPage
        apiClient={{ requirements } as unknown as ApiClient}
        canPickFolders
        canWrite
        onOpenProject={vi.fn()}
        onOpenRequirement={vi.fn()}
        projects={[]}
      />,
    );

    await waitFor(() =>
      expect(requirements).toHaveBeenCalledWith({
        project_id: undefined,
        priority: undefined,
        q: undefined,
        status: "active",
        limit: 10,
        offset: 0,
      }),
    );
    expect(await screen.findByText("北辰仓快递配送")).toBeInTheDocument();
    expect(screen.getByText("云图科研用药")).toBeInTheDocument();
  });

  it("applies query filters only after clicking 查询, and clears them on 重置", async () => {
    const requirements = vi.fn().mockResolvedValue(payload());
    render(
      <RequirementsPage
        apiClient={{ requirements } as unknown as ApiClient}
        canPickFolders
        canWrite
        onOpenProject={vi.fn()}
        onOpenRequirement={vi.fn()}
        projects={[{ id: "project-a", name: "云图科研用药", color: "#2c8d83" }]}
      />,
    );
    await waitFor(() => expect(requirements).toHaveBeenCalledTimes(1));

    await userEvent.type(screen.getByPlaceholderText("输入需求名称"), "北辰");
    expect(requirements).toHaveBeenCalledTimes(1);

    await userEvent.click(screen.getByRole("button", { name: "查询" }));
    await waitFor(() =>
      expect(requirements).toHaveBeenLastCalledWith(
        expect.objectContaining({ q: "北辰" }),
      ),
    );

    await userEvent.click(screen.getByRole("button", { name: "重置" }));
    await waitFor(() =>
      expect(requirements).toHaveBeenLastCalledWith(
        expect.objectContaining({ q: undefined }),
      ),
    );
  });

  it("opens the requirement detail via 查看 and the create modal via 新建需求", async () => {
    const requirements = vi.fn().mockResolvedValue(payload());
    const onOpenRequirement = vi.fn();
    render(
      <RequirementsPage
        apiClient={{ requirements } as unknown as ApiClient}
        canPickFolders
        canWrite
        onOpenProject={vi.fn()}
        onOpenRequirement={onOpenRequirement}
        projects={[]}
      />,
    );

    await userEvent.click(await screen.findByRole("button", { name: "查看" }));
    expect(onOpenRequirement).toHaveBeenCalledWith("req-1");

    await userEvent.click(screen.getByRole("button", { name: "＋ 新建需求" }));
    expect(screen.getByRole("dialog", { name: "新建需求" })).toBeInTheDocument();
  });

  it("shows the empty state with a create shortcut when a tab has no requirements", async () => {
    const requirements = vi.fn().mockResolvedValue(payload({ items: [], total: 0 }));
    render(
      <RequirementsPage
        apiClient={{ requirements } as unknown as ApiClient}
        canPickFolders
        canWrite
        onOpenProject={vi.fn()}
        onOpenRequirement={vi.fn()}
        projects={[]}
      />,
    );

    expect(await screen.findByText("还没有需求")).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: "＋ 新建需求" }).length).toBeGreaterThan(0);
  });
});
