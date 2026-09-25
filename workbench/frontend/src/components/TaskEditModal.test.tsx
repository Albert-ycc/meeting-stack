import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { TaskEditModal } from "./TaskEditModal";
import type { ApiClient } from "../api";
import type { Project, RequirementsPayload, Task } from "../types";

const PROJECTS: Project[] = [
  { id: "proj-a", name: "云图科研用药", color: "#2c8d83" },
  { id: "proj-b", name: "样品项目", color: "#f0783b" },
];

function requirementsPayload(items: RequirementsPayload["items"]): RequirementsPayload {
  return { items, total: items.length, limit: 200, offset: 0, counts: { active: items.length, done: 0, shelved: 0, all: items.length } };
}

const REQ_A1 = { id: "req-a1", project_id: "proj-a", project_name: "云图科研用药", project_color: "#2c8d83", title: "北辰仓快递配送", priority: "P0" as const, status: "active" as const, created_at: "2026-09-07T00:00:00Z", updated_at: "2026-09-09T00:00:00Z", open_task_count: 3, meeting_count: 2, latest_meeting_date: null, folder_count: 2 };
const REQ_A2 = { ...REQ_A1, id: "req-a2", title: "复审流程可配置", priority: "P1" as const };
const REQ_B1 = { id: "req-b1", project_id: "proj-b", project_name: "样品项目", project_color: "#f0783b", title: "白名单改造", priority: "P2" as const, status: "active" as const, created_at: "2026-09-01T00:00:00Z", updated_at: "2026-09-01T00:00:00Z", open_task_count: 1, meeting_count: 1, latest_meeting_date: null, folder_count: 0 };

function makeClient(overrides: Partial<ApiClient> = {}) {
  return {
    requirements: vi.fn().mockImplementation(async (filters: { project_id?: string }) => {
      if (filters.project_id === "proj-a") return requirementsPayload([REQ_A1, REQ_A2]);
      if (filters.project_id === "proj-b") return requirementsPayload([REQ_B1]);
      return requirementsPayload([]);
    }),
    createTask: vi.fn().mockResolvedValue({}),
    updateTask: vi.fn().mockResolvedValue({}),
    confirmTask: vi.fn().mockResolvedValue({}),
    ...overrides,
  } as unknown as ApiClient;
}

function makeTask(overrides: Partial<Task> = {}): Task {
  return {
    id: "t1",
    title: "尽快落实与北辰的对接安排",
    detail: "",
    status: "pending_confirm",
    origin: "ai",
    assignee: "me",
    meeting_id: "m1",
    project_id: "proj-a",
    anchor_ms: null,
    anchor_quote: null,
    status_changed_at: "2026-09-09T00:00:00Z",
    created_at: "2026-09-09T00:00:00Z",
    updated_at: "2026-09-09T00:00:00Z",
    meeting_title: "云图 EDC 接入与北辰安排跟进",
    project_name: "云图科研用药",
    project_color: "#2c8d83",
    stall_days: 0,
    stall_since: null,
    stalled: false,
    ...overrides,
  };
}

describe("TaskEditModal 新建：所属需求", () => {
  it("所属项目未选时所属需求禁用，选完项目才拉出该项目的需求", async () => {
    const requirements = vi.fn().mockImplementation(async (filters: { project_id?: string }) =>
      filters.project_id === "proj-a" ? requirementsPayload([REQ_A1, REQ_A2]) : requirementsPayload([]),
    );
    render(
      <TaskEditModal apiClient={makeClient({ requirements } as Partial<ApiClient>)} canWrite onClose={vi.fn()} onSaved={vi.fn()} projects={PROJECTS} task={null} />,
    );

    expect(screen.getByRole("button", { name: /未归需求/ })).toBeDisabled();

    await userEvent.click(screen.getByRole("button", { name: /未归项目/ }));
    await userEvent.click(screen.getByRole("option", { name: "云图科研用药" }));

    expect(screen.getByRole("button", { name: /未归需求/ })).toBeEnabled();
    await userEvent.click(screen.getByRole("button", { name: /未归需求/ }));
    expect(await screen.findByRole("option", { name: /北辰仓快递配送/ })).toBeInTheDocument();
    expect(screen.getByRole("option", { name: /复审流程可配置/ })).toBeInTheDocument();
  });

  it("选中需求后创建任务，payload 带上 requirement_id", async () => {
    const createTask = vi.fn().mockResolvedValue({});
    render(
      <TaskEditModal apiClient={makeClient({ createTask } as Partial<ApiClient>)} canWrite onClose={vi.fn()} onSaved={vi.fn()} projects={PROJECTS} task={null} />,
    );

    await userEvent.type(screen.getByPlaceholderText("要完成的事，例如：整理本周产品周报"), "新任务");
    await userEvent.click(screen.getByRole("button", { name: /未归项目/ }));
    await userEvent.click(screen.getByRole("option", { name: "云图科研用药" }));
    await userEvent.click(screen.getByRole("button", { name: /未归需求/ }));
    await userEvent.click(await screen.findByRole("option", { name: /北辰仓快递配送/ }));
    await userEvent.click(screen.getByRole("button", { name: "创建任务" }));

    expect(createTask).toHaveBeenCalledWith({
      title: "新任务",
      project_id: "proj-a",
      requirement_id: "req-a1",
      assignee: "ai",
    });
  });

  it("换所属项目后清空已选的所属需求", async () => {
    render(<TaskEditModal apiClient={makeClient()} canWrite onClose={vi.fn()} onSaved={vi.fn()} projects={PROJECTS} task={null} />);

    await userEvent.click(screen.getByRole("button", { name: /未归项目/ }));
    await userEvent.click(screen.getByRole("option", { name: "云图科研用药" }));
    await userEvent.click(screen.getByRole("button", { name: /未归需求/ }));
    await userEvent.click(await screen.findByRole("option", { name: /北辰仓快递配送/ }));
    expect(screen.getByRole("button", { name: /北辰仓快递配送/ })).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: /云图科研用药/ }));
    await userEvent.click(screen.getByRole("option", { name: "样品项目" }));

    expect(screen.getByRole("button", { name: /未归需求/ })).toBeInTheDocument();
  });

  it("defaultProjectId / defaultRequirementId 预选新建任务的所属项目与需求", async () => {
    render(
      <TaskEditModal
        apiClient={makeClient()}
        canWrite
        defaultProjectId="proj-a"
        defaultRequirementId="req-a1"
        onClose={vi.fn()}
        onSaved={vi.fn()}
        projects={PROJECTS}
        task={null}
      />,
    );

    expect(await screen.findByRole("button", { name: /北辰仓快递配送/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /云图科研用药/ })).toBeInTheDocument();
  });
});

describe("TaskEditModal 修改：所属需求", () => {
  it("已挂需求的任务预填优先级与需求名，保存并确认时带上 requirement_id", async () => {
    const confirmTask = vi.fn().mockResolvedValue({});
    render(
      <TaskEditModal
        apiClient={makeClient({ confirmTask } as Partial<ApiClient>)}
        canWrite
        onClose={vi.fn()}
        onSaved={vi.fn()}
        projects={PROJECTS}
        task={makeTask({ requirement_id: "req-a1", requirement_title: "北辰仓快递配送", requirement_priority: "P0", requirement_status: "active" })}
      />,
    );

    expect(await screen.findByText("P0")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /北辰仓快递配送/ })).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "保存并确认" }));
    expect(confirmTask).toHaveBeenCalledWith(
      "t1",
      expect.objectContaining({ requirement_id: "req-a1", project_id: "proj-a" }),
    );
  });

  it("改选未归需求后保存，requirement_id 传 null", async () => {
    const confirmTask = vi.fn().mockResolvedValue({});
    render(
      <TaskEditModal
        apiClient={makeClient({ confirmTask } as Partial<ApiClient>)}
        canWrite
        onClose={vi.fn()}
        onSaved={vi.fn()}
        projects={PROJECTS}
        task={makeTask({ requirement_id: "req-a1", requirement_title: "北辰仓快递配送", requirement_priority: "P0", requirement_status: "active" })}
      />,
    );

    await screen.findByText("P0");
    await userEvent.click(screen.getByRole("button", { name: /北辰仓快递配送/ }));
    await userEvent.click(await screen.findByRole("option", { name: "未归需求" }));
    await userEvent.click(screen.getByRole("button", { name: "保存并确认" }));

    expect(confirmTask).toHaveBeenCalledWith("t1", expect.objectContaining({ requirement_id: null }));
  });
});
