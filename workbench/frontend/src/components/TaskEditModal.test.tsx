import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { TaskEditModal } from "./TaskEditModal";
import { ApiError } from "../api";
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
    expect(confirmTask).toHaveBeenCalledWith("t1", expect.objectContaining({ requirement_id: "req-a1" }));
    // 没动过项目下拉就不带 project_id：显式 null 会被后端当成「清空项目」
    expect(confirmTask.mock.calls[0][1]).not.toHaveProperty("project_id");
  });

  it("动过项目下拉才提交 project_id，选「未归项目」时显式传 null", async () => {
    const updateTask = vi.fn().mockResolvedValue({});
    render(
      <TaskEditModal
        apiClient={makeClient({ updateTask } as Partial<ApiClient>)}
        canWrite
        onClose={vi.fn()}
        onSaved={vi.fn()}
        projects={PROJECTS}
        task={makeTask({ status: "confirmed" })}
      />,
    );

    await userEvent.click(screen.getByRole("button", { name: /云图科研用药/ }));
    await userEvent.click(screen.getByRole("option", { name: "未归项目" }));
    await userEvent.click(screen.getByRole("button", { name: "保存" }));

    expect(updateTask).toHaveBeenCalledWith("t1", expect.objectContaining({ project_id: null }));
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

describe("TaskEditModal 原地新建项目：近似重名", () => {
  const suggestion = { project_id: "proj-a", name: "云图科研用药", also_names: ["云图"], matched: "云图科研用药", match: "similar" };

  it("撞上近似重名时问「已有『X』，用它？」，点「用它」就选中已有项目", async () => {
    const createProject = vi.fn().mockRejectedValue(new ApiError("已有相近的项目", 409, { suggestion }));
    const createTask = vi.fn().mockResolvedValue({});
    render(
      <TaskEditModal
        apiClient={makeClient({ createProject, createTask } as Partial<ApiClient>)}
        canWrite
        onClose={vi.fn()}
        onSaved={vi.fn()}
        projects={PROJECTS}
        task={null}
      />,
    );

    await userEvent.type(screen.getByPlaceholderText("要完成的事，例如：整理本周产品周报"), "新任务");
    await userEvent.click(screen.getByRole("button", { name: /未归项目/ }));
    await userEvent.click(screen.getByRole("button", { name: "＋ 新建项目" }));
    await userEvent.type(screen.getByRole("textbox", { name: "新项目名称" }), "云图科研");
    await userEvent.click(screen.getByRole("button", { name: "创建" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("已有「云图科研用药」（又称 云图），是不是它？");
    await userEvent.click(screen.getByRole("button", { name: "用它" }));
    await userEvent.click(screen.getByRole("button", { name: "创建任务" }));

    expect(createTask).toHaveBeenCalledWith(expect.objectContaining({ project_id: "proj-a" }));
  });

  it("点「仍然新建」带 force 建新项目", async () => {
    const createProject = vi.fn().mockRejectedValue(new ApiError("已有相近的项目", 409, { suggestion }));
    const createProjectWith = vi.fn().mockResolvedValue({ id: "proj-new", name: "云图科研", color: "#667085" });
    render(
      <TaskEditModal
        apiClient={makeClient({ createProject, createProjectWith } as Partial<ApiClient>)}
        canWrite
        onClose={vi.fn()}
        onSaved={vi.fn()}
        projects={PROJECTS}
        task={null}
      />,
    );

    await userEvent.click(screen.getByRole("button", { name: /未归项目/ }));
    await userEvent.click(screen.getByRole("button", { name: "＋ 新建项目" }));
    await userEvent.type(screen.getByRole("textbox", { name: "新项目名称" }), "云图科研");
    await userEvent.click(screen.getByRole("button", { name: "创建" }));
    await userEvent.click(await screen.findByRole("button", { name: "仍然新建" }));

    expect(createProjectWith).toHaveBeenCalledWith({ name: "云图科研", color: expect.stringMatching(/^#/), force: true });
  });
});

describe("TaskEditModal 从需求详情新建（fixedRequirement，R05-6，S12-c）", () => {
  const FIXED = { id: "req-a1", title: "北辰仓快递配送", priority: "P0" as const };

  function renderFixed(api: Partial<ApiClient> = {}, props: Partial<Parameters<typeof TaskEditModal>[0]> = {}) {
    const onSaved = vi.fn();
    const onClose = vi.fn();
    render(
      <TaskEditModal
        apiClient={makeClient(api)}
        canWrite
        defaultProjectId="proj-a"
        fixedRequirement={FIXED}
        onClose={onClose}
        onSaved={onSaved}
        projects={PROJECTS}
        task={null}
        {...props}
      />,
    );
    return { onSaved, onClose };
  }

  it("副标题写「挂在「需求名」下」；「挂到需求」是只读的一行：需求名、P 标签、锁，旁注说明为什么不能改", () => {
    renderFixed();

    const dialog = screen.getByRole("dialog", { name: "新建任务" });
    expect(dialog).toHaveTextContent("挂在「北辰仓快递配送」下");
    const locked = within(dialog).getByRole("group", { name: "挂到需求" });
    expect(locked).toHaveTextContent("北辰仓快递配送");
    expect(within(locked).getByText("P0")).toHaveClass("priority-badge--p0");
    expect(within(locked).getByRole("img", { name: "不能修改" })).toBeInTheDocument();
    expect(dialog).toHaveTextContent("从需求详情新建，固定挂在这条需求上");
    // 是一行字，不是下拉：里面没有能点开的东西
    expect(within(locked).queryByRole("button")).not.toBeInTheDocument();
  });

  it("不能改需求，也不能改项目：弹窗里没有所属项目、所属需求两个下拉，也不去拉项目下的需求列表", () => {
    const requirements = vi.fn();
    renderFixed({ requirements } as Partial<ApiClient>);

    expect(screen.queryByText("所属项目")).not.toBeInTheDocument();
    expect(screen.queryByText("所属需求")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /未归项目|云图科研用药/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /未归需求/ })).not.toBeInTheDocument();
    expect(requirements).not.toHaveBeenCalled();
  });

  it("填任务名、选负责人，创建时任务固定挂在这条需求上，项目取需求所在的项目；负责人默认「我」", async () => {
    const createTask = vi.fn().mockResolvedValue({});
    const { onSaved, onClose } = renderFixed({ createTask } as Partial<ApiClient>);

    expect(screen.getByRole("button", { name: "我" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("button", { name: "AI" })).toHaveAttribute("aria-pressed", "false");
    // 没填任务名不能创建
    expect(screen.getByRole("button", { name: "创建" })).toBeDisabled();

    await userEvent.type(screen.getByPlaceholderText("要做的一件事"), "与北辰拉会对齐科研仓对接工作量与排期");
    await userEvent.click(screen.getByRole("button", { name: "创建" }));

    expect(createTask).toHaveBeenCalledWith({
      title: "与北辰拉会对齐科研仓对接工作量与排期",
      project_id: "proj-a",
      requirement_id: "req-a1",
      assignee: "me",
    });
    expect(onSaved).toHaveBeenCalledTimes(1);
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("负责人可以改成 AI", async () => {
    const createTask = vi.fn().mockResolvedValue({});
    renderFixed({ createTask } as Partial<ApiClient>);

    await userEvent.click(screen.getByRole("button", { name: "AI" }));
    expect(screen.getByRole("button", { name: "AI" })).toHaveAttribute("aria-pressed", "true");
    await userEvent.type(screen.getByPlaceholderText("要做的一件事"), "整理本周产品周报");
    await userEvent.click(screen.getByRole("button", { name: "创建" }));

    expect(createTask).toHaveBeenCalledWith(expect.objectContaining({ assignee: "ai", requirement_id: "req-a1" }));
  });

  it("创建失败：原因写在弹窗里，弹窗不关，需求还是固定的那一条", async () => {
    const createTask = vi.fn().mockRejectedValue(new Error("需求已经被删了"));
    const { onSaved, onClose } = renderFixed({ createTask } as Partial<ApiClient>);

    await userEvent.type(screen.getByPlaceholderText("要做的一件事"), "整理本周产品周报");
    await userEvent.click(screen.getByRole("button", { name: "创建" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("需求已经被删了");
    expect(onSaved).not.toHaveBeenCalled();
    expect(onClose).not.toHaveBeenCalled();
    expect(screen.getByRole("group", { name: "挂到需求" })).toHaveTextContent("北辰仓快递配送");
  });

  it("旧调用方没变（待办、项目页）：不传 fixedRequirement 时还是原来的样子——有所属项目、所属需求下拉，没有副标题和锁定行，负责人默认 AI", async () => {
    const createTask = vi.fn().mockResolvedValue({});
    render(
      <TaskEditModal
        apiClient={makeClient({ createTask } as Partial<ApiClient>)}
        canWrite
        defaultProjectId="proj-a"
        defaultRequirementId="req-a1"
        onClose={vi.fn()}
        onSaved={vi.fn()}
        projects={PROJECTS}
        task={null}
      />,
    );

    const dialog = screen.getByRole("dialog", { name: "新建任务" });
    expect(dialog).not.toHaveTextContent("挂在「");
    expect(within(dialog).queryByRole("group", { name: "挂到需求" })).not.toBeInTheDocument();
    expect(within(dialog).getByText("所属项目")).toBeInTheDocument();
    expect(within(dialog).getByText("所属需求")).toBeInTheDocument();
    expect(within(dialog).getByText("任务描述")).toBeInTheDocument();
    expect(within(dialog).getByText("执行方")).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "交给 AI" })).toHaveAttribute("aria-pressed", "true");
    expect(within(dialog).getByRole("button", { name: "我来做" })).toHaveAttribute("aria-pressed", "false");
    expect(within(dialog).getByRole("button", { name: "创建任务" })).toBeInTheDocument();
    // 项目和需求还能点开改
    expect(await within(dialog).findByRole("button", { name: /北辰仓快递配送/ })).toBeEnabled();
    expect(within(dialog).getByRole("button", { name: /云图科研用药/ })).toBeEnabled();

    await userEvent.type(within(dialog).getByPlaceholderText("要完成的事，例如：整理本周产品周报"), "新任务");
    await userEvent.click(within(dialog).getByRole("button", { name: "创建任务" }));
    expect(createTask).toHaveBeenCalledWith({
      title: "新任务",
      project_id: "proj-a",
      requirement_id: "req-a1",
      assignee: "ai",
    });
  });

  it("旧调用方没变：修改已有任务时传了 fixedRequirement 也不生效（只对新建生效），需求下拉还在", async () => {
    render(
      <TaskEditModal
        apiClient={makeClient()}
        canWrite
        fixedRequirement={FIXED}
        onClose={vi.fn()}
        onSaved={vi.fn()}
        projects={PROJECTS}
        task={makeTask({ status: "confirmed", requirement_id: "req-a1", requirement_title: "北辰仓快递配送", requirement_priority: "P0", requirement_status: "active" })}
      />,
    );

    const dialog = screen.getByRole("dialog", { name: "修改任务" });
    expect(dialog).not.toHaveTextContent("挂在「");
    expect(within(dialog).queryByRole("group", { name: "挂到需求" })).not.toBeInTheDocument();
    expect(within(dialog).getByText("所属需求")).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "交给 AI" })).toBeInTheDocument();
  });
});
