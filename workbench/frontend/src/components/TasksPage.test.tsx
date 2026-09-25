import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { TasksPage } from "./TasksPage";
import type { ApiClient } from "../api";
import type { RequirementsPayload, Task, TaskFilters, TaskStatus } from "../types";

function makeTask(id: string, status: TaskStatus, title: string): Task {
  return {
    id,
    title,
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
  };
}

const TASKS: Task[] = [
  makeTask("t-pending", "pending_confirm", "确认样品发放口径"),
  makeTask("t-confirmed", "confirmed", "补齐 HCP 编码校验"),
  makeTask("t-progress", "in_progress", "对接青禾下单"),
  makeTask("t-done", "done", "交付操作手册"),
  makeTask("t-cancelled", "cancelled", "平台级模板页"),
  makeTask("t-expired", "expired", "整理两周前的草稿"),
];

const EMPTY_REQUIREMENTS: RequirementsPayload = {
  items: [],
  total: 0,
  limit: 200,
  offset: 0,
  counts: { active: 0, done: 0, shelved: 0, all: 0 },
};

function makeClient(overrides: Partial<ApiClient> = {}) {
  return {
    tasks: vi.fn().mockResolvedValue({ items: TASKS, total: TASKS.length, limit: 10, offset: 0 }),
    task: vi.fn().mockResolvedValue({ ...TASKS[3], events: [], deliverables: [] }),
    requirements: vi.fn().mockResolvedValue(EMPTY_REQUIREMENTS),
    confirmTask: vi.fn().mockResolvedValue({}),
    rejectTask: vi.fn().mockResolvedValue({}),
    setTaskStatus: vi.fn().mockResolvedValue({}),
    batchConfirmTasks: vi.fn().mockResolvedValue({ confirmed: [], failed: [] }),
    batchRejectTasks: vi.fn().mockResolvedValue({ rejected: [], failed: [] }),
    undoTaskReview: vi.fn().mockResolvedValue({ reverted: [], failed: [] }),
    ...overrides,
  } as unknown as ApiClient;
}

/** 模拟服务端：按 status（可逗号分隔）筛选、按 limit/offset 分页、counts 固定返回。 */
function serverTasks(items: Task[], counts: Partial<Record<TaskStatus, number>> = {}) {
  return vi.fn().mockImplementation(async (filters: TaskFilters = {}) => {
    const statuses = filters.status ? filters.status.split(",") : null;
    const matched = statuses ? items.filter((task) => statuses.includes(task.status)) : items;
    const limit = filters.limit ?? matched.length;
    const offset = filters.offset ?? 0;
    return { items: matched.slice(offset, offset + limit), total: matched.length, limit, offset, counts };
  });
}

function renderPage(apiClient: ApiClient, canWrite = true) {
  return render(
    <TasksPage
      apiClient={apiClient}
      canWrite={canWrite}
      onOpenMeeting={vi.fn()}
      onOpenProject={vi.fn()}
      onOpenRequirement={vi.fn()}
      projects={[]}
    />,
  );
}

/** 行定位：任务标题所在的 <tr>，操作按钮都挂在同一行内 */
async function rowOf(title: string): Promise<HTMLElement> {
  const titleNode = await screen.findByText(title);
  const row = titleNode.closest("tr");
  if (!row) throw new Error(`未找到 ${title} 所在的任务行`);
  return row as HTMLElement;
}

describe("TasksPage 行内操作", () => {
  it("待确认任务可以就地确认", async () => {
    const apiClient = makeClient();
    renderPage(apiClient);

    const row = await rowOf("确认样品发放口径");
    await userEvent.click(within(row).getByRole("button", { name: "确认" }));

    expect(apiClient.confirmTask).toHaveBeenCalledWith("t-pending", {});
  });

  it("待确认任务的确认/修改/驳回平铺显示，没有「更多操作」菜单", async () => {
    const apiClient = makeClient();
    renderPage(apiClient);

    const row = await rowOf("确认样品发放口径");
    expect(within(row).getByRole("button", { name: "确认" })).toBeInTheDocument();
    expect(within(row).getByRole("button", { name: "修改" })).toBeInTheDocument();
    expect(within(row).getByRole("button", { name: "驳回" })).toBeInTheDocument();
    expect(within(row).queryByRole("button", { name: "更多操作" })).not.toBeInTheDocument();

    await userEvent.click(within(row).getByRole("button", { name: "驳回" }));
    expect(apiClient.rejectTask).toHaveBeenCalledWith("t-pending");
  });

  it("已确认任务可以开始处理", async () => {
    const apiClient = makeClient();
    renderPage(apiClient);

    const row = await rowOf("补齐 HCP 编码校验");
    await userEvent.click(within(row).getByRole("button", { name: "开始处理" }));

    expect(apiClient.setTaskStatus).toHaveBeenCalledWith("t-confirmed", "in_progress");
  });

  it("进行中任务可以标记完成", async () => {
    const apiClient = makeClient();
    renderPage(apiClient);

    const row = await rowOf("对接青禾下单");
    await userEvent.click(within(row).getByRole("button", { name: "标记完成" }));

    expect(apiClient.setTaskStatus).toHaveBeenCalledWith("t-progress", "done");
  });

  it("进行中任务可以从更多菜单回退到已确认", async () => {
    const apiClient = makeClient();
    renderPage(apiClient);

    const row = await rowOf("对接青禾下单");
    await userEvent.click(within(row).getByRole("button", { name: "更多操作" }));
    await userEvent.click(screen.getByRole("menuitem", { name: "回退" }));

    expect(apiClient.setTaskStatus).toHaveBeenCalledWith("t-progress", "confirmed");
  });

  it("进行中任务可以从更多菜单取消", async () => {
    const apiClient = makeClient();
    renderPage(apiClient);

    const row = await rowOf("对接青禾下单");
    await userEvent.click(within(row).getByRole("button", { name: "更多操作" }));
    await userEvent.click(screen.getByRole("menuitem", { name: "取消任务" }));

    expect(apiClient.setTaskStatus).toHaveBeenCalledWith("t-progress", "cancelled");
  });

  it("已取消任务可以恢复成已确认", async () => {
    const apiClient = makeClient();
    renderPage(apiClient);

    const row = await rowOf("平台级模板页");
    await userEvent.click(within(row).getByRole("button", { name: "恢复" }));

    expect(apiClient.setTaskStatus).toHaveBeenCalledWith("t-cancelled", "confirmed");
  });

  it("已过期草稿可以恢复到待确认", async () => {
    const apiClient = makeClient();
    renderPage(apiClient);

    const row = await rowOf("整理两周前的草稿");
    await userEvent.click(within(row).getByRole("button", { name: "恢复" }));

    expect(apiClient.setTaskStatus).toHaveBeenCalledWith("t-expired", "pending_confirm");
  });

  it("已过期草稿可以从更多菜单直接确认", async () => {
    const apiClient = makeClient();
    renderPage(apiClient);

    const row = await rowOf("整理两周前的草稿");
    await userEvent.click(within(row).getByRole("button", { name: "更多操作" }));
    await userEvent.click(screen.getByRole("menuitem", { name: "直接确认" }));

    expect(apiClient.confirmTask).toHaveBeenCalledWith("t-expired", {});
  });

  it("已完成任务不出状态按钮：后端 done 是终态", async () => {
    const apiClient = makeClient();
    renderPage(apiClient);

    const row = await rowOf("交付操作手册");
    expect(within(row).queryByRole("button")).not.toBeInTheDocument();
  });

  it("只读模式下不渲染任何行内操作", async () => {
    const apiClient = makeClient();
    renderPage(apiClient, false);

    const row = await rowOf("确认样品发放口径");
    expect(within(row).queryByRole("button", { name: "确认" })).not.toBeInTheDocument();
    expect(within(row).queryByRole("button", { name: "更多操作" })).not.toBeInTheDocument();
    // 只读模式下也不出勾选列（待确认页签的全选/单选都要求 canWrite）
    expect(within(row).queryByRole("checkbox")).not.toBeInTheDocument();
  });

  it("操作进行中不会连发第二个写请求", async () => {
    let resolveConfirm!: (value: unknown) => void;
    const confirmTask = vi.fn().mockReturnValue(new Promise((resolve) => { resolveConfirm = resolve; }));
    const apiClient = makeClient({ confirmTask } as Partial<ApiClient>);
    renderPage(apiClient);

    const row = await rowOf("确认样品发放口径");
    const button = within(row).getByRole("button", { name: "确认" });
    await userEvent.click(button);
    await userEvent.click(button);

    expect(confirmTask).toHaveBeenCalledTimes(1);
    resolveConfirm({});
  });

  it("行内按钮聚焦时按回车不劫持：不应误开抽屉", async () => {
    const apiClient = makeClient();
    renderPage(apiClient);

    const row = await rowOf("补齐 HCP 编码校验");
    const button = within(row).getByRole("button", { name: "开始处理" });
    fireEvent.keyDown(button, { key: "Enter" });

    expect(screen.queryByRole("dialog", { name: "任务详情" })).not.toBeInTheDocument();
  });

  it("行本身聚焦时按回车仍打开抽屉", async () => {
    const apiClient = makeClient();
    renderPage(apiClient);

    const row = await rowOf("补齐 HCP 编码校验");
    fireEvent.keyDown(row, { key: "Enter" });

    expect(screen.getByRole("dialog", { name: "任务详情" })).toBeInTheDocument();
  });
});

describe("TasksPage 所属项目 / 所属需求列", () => {
  it("有所属项目时显示色点+名称，点击跳转项目详情", async () => {
    const withProject = { ...makeTask("t-p1", "in_progress", "对接北辰仓"), project_id: "proj-a", project_name: "云图科研用药", project_color: "#2c8d83" };
    const onOpenProject = vi.fn();
    const apiClient = makeClient({ tasks: serverTasks([withProject]) } as Partial<ApiClient>);
    render(<TasksPage apiClient={apiClient} canWrite onOpenMeeting={vi.fn()} onOpenProject={onOpenProject} onOpenRequirement={vi.fn()} projects={[]} />);

    const row = await rowOf("对接北辰仓");
    await userEvent.click(within(row).getByRole("button", { name: "云图科研用药" }));
    expect(onOpenProject).toHaveBeenCalledWith("proj-a");
  });

  it("没有所属项目/所属需求时显示—", async () => {
    const apiClient = makeClient();
    renderPage(apiClient);

    const row = await rowOf("确认样品发放口径");
    const cells = within(row).getAllByRole("cell");
    expect(cells.map((cell) => cell.textContent)).toContain("—");
  });

  it("挂了需求时显示需求名与优先级胶囊，点击跳转需求详情", async () => {
    const withRequirement = {
      ...makeTask("t-r1", "in_progress", "按北辰接口清单重做原型"),
      requirement_id: "req-1",
      requirement_title: "北辰仓快递配送",
      requirement_priority: "P0" as const,
      requirement_status: "active" as const,
    };
    const onOpenRequirement = vi.fn();
    const apiClient = makeClient({ tasks: serverTasks([withRequirement]) } as Partial<ApiClient>);
    render(<TasksPage apiClient={apiClient} canWrite onOpenMeeting={vi.fn()} onOpenProject={vi.fn()} onOpenRequirement={onOpenRequirement} projects={[]} />);

    const row = await rowOf("按北辰接口清单重做原型");
    expect(within(row).getByText("P0")).toBeInTheDocument();
    await userEvent.click(within(row).getByRole("button", { name: "北辰仓快递配送" }));
    expect(onOpenRequirement).toHaveBeenCalledWith("req-1");
  });
});

describe("TasksPage 服务端计数、分页、批量与撤销", () => {
  const PENDING = [
    makeTask("p1", "pending_confirm", "草稿甲"),
    makeTask("p2", "pending_confirm", "草稿乙"),
  ];

  it("页签数字取服务端 counts，不受本页条数影响", async () => {
    const apiClient = makeClient({
      tasks: serverTasks(TASKS, {
        pending_confirm: 383,
        confirmed: 139,
        in_progress: 0,
        done: 73,
        cancelled: 152,
        expired: 205,
      }),
    } as Partial<ApiClient>);
    renderPage(apiClient);

    expect(await screen.findByRole("tab", { name: /已完成/ })).toHaveTextContent("73");
    expect(screen.getByRole("tab", { name: /全部/ })).toHaveTextContent("952");
    expect(screen.getByRole("tab", { name: /已过期/ })).toHaveTextContent("205");
    expect(screen.getByRole("tab", { name: /进行中/ })).toHaveTextContent("139");
  });

  it("切页签按状态向服务端取数，进行中合并已确认", async () => {
    const tasks = serverTasks(TASKS);
    const apiClient = makeClient({ tasks } as Partial<ApiClient>);
    renderPage(apiClient);
    await rowOf("确认样品发放口径");

    await userEvent.click(screen.getByRole("tab", { name: /进行中/ }));

    expect(tasks).toHaveBeenLastCalledWith(expect.objectContaining({ status: "confirmed,in_progress" }));
    expect(await screen.findByText("补齐 HCP 编码校验")).toBeInTheDocument();
    expect(screen.queryByText("交付操作手册")).not.toBeInTheDocument();
  });

  it("待确认页签才出勾选列，勾选框只做选择不直接确认", async () => {
    const apiClient = makeClient({ tasks: serverTasks(PENDING) } as Partial<ApiClient>);
    renderPage(apiClient);
    await userEvent.click(await screen.findByRole("tab", { name: /待确认/ }));

    await userEvent.click(await screen.findByRole("checkbox", { name: "选择任务：草稿甲" }));

    expect(apiClient.confirmTask).not.toHaveBeenCalled();
    expect(screen.getByRole("toolbar", { name: "批量操作" })).toHaveTextContent("已选 1 项");
  });

  it("表头「全选本页」勾选后可批量确认", async () => {
    const batchConfirmTasks = vi.fn().mockResolvedValue({ confirmed: ["p1", "p2"], failed: [] });
    const apiClient = makeClient({ tasks: serverTasks(PENDING), batchConfirmTasks } as Partial<ApiClient>);
    renderPage(apiClient);
    await userEvent.click(await screen.findByRole("tab", { name: /待确认/ }));

    await userEvent.click(await screen.findByRole("checkbox", { name: "全选本页" }));
    await userEvent.click(screen.getByRole("button", { name: "确认所选" }));

    expect(batchConfirmTasks).toHaveBeenCalledWith(["p1", "p2"]);
    expect(await screen.findByRole("button", { name: "撤销" })).toBeInTheDocument();
  });

  it("勾选多条批量驳回后可以撤销", async () => {
    const batchRejectTasks = vi.fn().mockResolvedValue({ rejected: ["p1", "p2"], failed: [] });
    const undoTaskReview = vi.fn().mockResolvedValue({ reverted: ["p1", "p2"], failed: [] });
    const apiClient = makeClient({
      tasks: serverTasks(PENDING),
      batchRejectTasks,
      undoTaskReview,
    } as Partial<ApiClient>);
    renderPage(apiClient);
    await userEvent.click(await screen.findByRole("tab", { name: /待确认/ }));

    await userEvent.click(await screen.findByRole("checkbox", { name: "选择任务：草稿甲" }));
    await userEvent.click(screen.getByRole("checkbox", { name: "选择任务：草稿乙" }));
    await userEvent.click(screen.getByRole("button", { name: "驳回所选" }));

    expect(batchRejectTasks).toHaveBeenCalledWith(["p1", "p2"]);
    await userEvent.click(await screen.findByRole("button", { name: "撤销" }));
    expect(undoTaskReview).toHaveBeenCalledWith(["p1", "p2"]);
    expect(await screen.findByText("已撤销 2 项，恢复为待确认")).toBeInTheDocument();
  });

  it("每页 10 条，服务端分页，点页码条翻页", async () => {
    const many = Array.from({ length: 23 }, (_, index) =>
      makeTask(`c${index}`, "confirmed", `已确认任务 ${index}`),
    );
    const tasks = serverTasks(many);
    const apiClient = makeClient({ tasks } as Partial<ApiClient>);
    renderPage(apiClient);

    expect(await screen.findByText("已确认任务 0")).toBeInTheDocument();
    expect(tasks).toHaveBeenLastCalledWith(expect.objectContaining({ limit: 10, offset: 0 }));
    expect(screen.getByText("共 23 条")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "2" }));
    expect(tasks).toHaveBeenLastCalledWith(expect.objectContaining({ limit: 10, offset: 10 }));
    expect(await screen.findByText("已确认任务 10")).toBeInTheDocument();
  });
});

describe("TasksPage 查询区", () => {
  const PROJECTS = [
    { id: "proj-a", name: "云图科研用药", color: "#2c8d83" },
    { id: "proj-b", name: "样品项目", color: "#f0783b" },
  ];

  function renderWithProjects(apiClient: ApiClient) {
    return render(
      <TasksPage
        apiClient={apiClient}
        canWrite
        onOpenMeeting={vi.fn()}
        onOpenProject={vi.fn()}
        onOpenRequirement={vi.fn()}
        projects={PROJECTS}
      />,
    );
  }

  it("所属项目下拉数字跟随当前页签", async () => {
    const tasks = vi.fn().mockResolvedValue({
      items: TASKS,
      total: TASKS.length,
      limit: 10,
      offset: 0,
      counts: {},
      project_counts: {
        "proj-a": { pending_confirm: 40, confirmed: 17, done: 15 },
        "proj-b": { confirmed: 34 },
        none: { pending_confirm: 79, confirmed: 32 },
      },
    });
    renderWithProjects(makeClient({ tasks } as Partial<ApiClient>));

    const select = await screen.findByLabelText("所属项目");
    await waitFor(() => expect(within(select).getByRole("option", { name: "云图科研用药（72）" })).toBeInTheDocument());
    expect(within(select).getByRole("option", { name: "未归属（111）" })).toBeInTheDocument();
    expect(within(select).getByRole("option", { name: "全部项目（217）" })).toBeInTheDocument();

    await userEvent.click(screen.getByRole("tab", { name: /进行中/ }));

    await waitFor(() => expect(within(select).getByRole("option", { name: "云图科研用药（17）" })).toBeInTheDocument());
    expect(within(select).getByRole("option", { name: "样品项目（34）" })).toBeInTheDocument();
    expect(within(select).getByRole("option", { name: "未归属（32）" })).toBeInTheDocument();
  });

  it("点查询按所属项目筛选，未归属传 none；重置清空全部条件", async () => {
    const tasks = serverTasks(TASKS);
    renderWithProjects(makeClient({ tasks } as Partial<ApiClient>));
    await rowOf("确认样品发放口径");

    await userEvent.selectOptions(screen.getByLabelText("所属项目"), "proj-b");
    await userEvent.click(screen.getByRole("button", { name: "查询" }));
    expect(tasks).toHaveBeenLastCalledWith(expect.objectContaining({ project_id: "proj-b" }));

    await userEvent.selectOptions(screen.getByLabelText("所属项目"), "none");
    await userEvent.click(screen.getByRole("button", { name: "查询" }));
    expect(tasks).toHaveBeenLastCalledWith(expect.objectContaining({ project_id: "none" }));

    await userEvent.click(screen.getByRole("button", { name: "重置" }));
    expect(tasks.mock.lastCall?.[0]).not.toHaveProperty("project_id");
    expect(screen.getByLabelText("所属项目")).toHaveValue("");
  });

  it("按任务名称查询", async () => {
    const tasks = serverTasks(TASKS);
    renderWithProjects(makeClient({ tasks } as Partial<ApiClient>));
    await rowOf("确认样品发放口径");

    await userEvent.type(screen.getByPlaceholderText("输入任务名称"), "样品");
    await userEvent.click(screen.getByRole("button", { name: "查询" }));

    expect(tasks).toHaveBeenLastCalledWith(expect.objectContaining({ q: "样品" }));
  });

  it("按执行方与来源会议日期区间查询", async () => {
    const tasks = serverTasks(TASKS);
    renderWithProjects(makeClient({ tasks } as Partial<ApiClient>));
    await rowOf("确认样品发放口径");

    await userEvent.selectOptions(screen.getByLabelText("执行方"), "me");
    await userEvent.type(screen.getByLabelText("开始日期"), "2026-09-01");
    await userEvent.type(screen.getByLabelText("结束日期"), "2026-09-15");
    await userEvent.click(screen.getByRole("button", { name: "查询" }));

    expect(tasks).toHaveBeenLastCalledWith(
      expect.objectContaining({ assignee: "me", meeting_date_from: "2026-09-01", meeting_date_to: "2026-09-15" }),
    );
  });

  it("所属需求下拉跟随所属项目收窄，选了项目才列该项目的进行中需求，查询时带上 requirement_id", async () => {
    const requirements = vi.fn().mockImplementation(async (filters: { project_id?: string }) =>
      filters.project_id === "proj-a"
        ? {
            items: [
              { id: "req-1", project_id: "proj-a", project_name: "云图科研用药", project_color: "#2c8d83", title: "北辰仓快递配送", priority: "P0", status: "active", created_at: "", updated_at: "", open_task_count: 3, meeting_count: 2, latest_meeting_date: null, folder_count: 2 },
            ],
            total: 1,
            limit: 200,
            offset: 0,
            counts: { active: 1, done: 0, shelved: 0, all: 1 },
          }
        : EMPTY_REQUIREMENTS,
    );
    const tasks = serverTasks(TASKS);
    renderWithProjects(makeClient({ requirements, tasks } as Partial<ApiClient>));
    await rowOf("确认样品发放口径");

    await userEvent.selectOptions(screen.getByLabelText("所属项目"), "proj-a");
    expect(await screen.findByRole("option", { name: /北辰仓快递配送/ })).toBeInTheDocument();

    await userEvent.selectOptions(screen.getByLabelText("所属需求"), "req-1");
    await userEvent.click(screen.getByRole("button", { name: "查询" }));

    expect(tasks).toHaveBeenLastCalledWith(
      expect.objectContaining({ project_id: "proj-a", requirement_id: "req-1" }),
    );
  });
});
