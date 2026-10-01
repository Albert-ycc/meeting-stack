import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { TasksPage } from "./TasksPage";
import type { ReviewCardsPanelProps } from "./todo/ReviewCardsPanel";
import { ApiError, type ApiClient } from "../api";
import type {
  LinkOption,
  RequirementOptionsPayload,
  RequirementsPayload,
  Task,
  TaskFilters,
  TaskStatus,
  TodoFilters,
  TodoGroupKey,
  TodoPayload,
} from "../types";

// 审核卡由另一个组件负责：这里只看页面传给它的 props
const panel = vi.hoisted(() => ({ props: null as unknown }));
vi.mock("./todo/ReviewCardsPanel", () => ({
  ReviewCardsPanel: (props: unknown) => {
    panel.props = props;
    return <div data-testid="review-cards" />;
  },
}));
const panelProps = () => panel.props as ReviewCardsPanelProps;

const TODAY = "2026-09-30"; // 周三，本周日是 10-04

function makeTask(id: string, status: TaskStatus, title: string, overrides: Partial<Task> = {}): Task {
  return {
    id,
    title,
    detail: "",
    status,
    origin: "ai",
    assignee: "me",
    meeting_id: "m-edc",
    project_id: null,
    anchor_ms: null,
    anchor_quote: null,
    status_changed_at: "2026-09-28T02:00:00Z",
    created_at: "2026-09-28T02:00:00Z",
    updated_at: "2026-09-28T02:00:00Z",
    meeting_title: "EDC 系统选型与产研对接决策",
    meeting_recording_date: "2026-09-28",
    project_name: null,
    project_color: null,
    stall_days: 0,
    stall_since: null,
    stalled: false,
    due_date: null,
    ...overrides,
  };
}

const HEKA = { project_id: "p-heika", project_name: "黑卡小程序", project_color: "#101010" };
const YIMI = { project_id: "p-yimi", project_name: "医米科研用药", project_color: "#fb7b30" };
const CARD_MEETING = {
  meeting_id: "m-card",
  meeting_title: "发卡变更与一直拍流程改造沟通",
  meeting_recording_date: "2026-09-22",
};

function seedTasks(): Task[] {
  return [
    makeTask("t-late-1", "confirmed", "明天晚上先上后台并撤下码，统一验证扫码", {
      ...HEKA,
      ...CARD_MEETING,
      due_date: "2026-09-24",
      anchor_ms: 696_000,
    }),
    makeTask("t-late-2", "in_progress", "「人身健康」明天开发完、28号再测", {
      ...HEKA,
      ...CARD_MEETING,
      due_date: "2026-09-28",
      anchor_ms: 380_000,
      assignee: "ai",
    }),
    makeTask("t-week", "confirmed", "给出定好的积分规则", { ...HEKA, due_date: "2026-10-02", meeting_id: "m-card" }),
    makeTask("t-sync", "confirmed", "与萌总同步 EDC 选型议题", { ...YIMI, anchor_ms: 120_000, stall_days: 3.6, stalled: true }),
    makeTask("t-staff", "confirmed", "确认产研能否派一人对接 EDC", { ...YIMI, stall_days: 2, stalled: false }),
    makeTask("t-huayi", "confirmed", "安排与华谊的会", { ...YIMI }),
    makeTask("t-pending", "pending_confirm", "确认发卡名单口径", { ...HEKA }),
    makeTask("t-done", "done", "交付操作手册", { ...YIMI }),
    makeTask("t-cancelled", "cancelled", "平台级模板页", { ...YIMI }),
    makeTask("t-expired", "expired", "整理两周前的草稿", { ...YIMI }),
  ];
}

const PROJECTS = [
  { id: "p-yimi", name: "医米科研用药", color: "#fb7b30", seat: 1, latest_meeting_date: "2026-09-28" },
  { id: "p-heika", name: "黑卡小程序", color: "#101010", seat: 2, latest_meeting_date: "2026-09-22" },
  { id: "p-cvm", name: "CVM 云讲堂", color: "#2c8d83", seat: null, latest_meeting_date: "2026-09-29" },
];

const EMPTY_REQUIREMENTS: RequirementsPayload = {
  items: [],
  total: 0,
  limit: 200,
  offset: 0,
  counts: { active: 0, done: 0, shelved: 0, all: 0 },
};

function groupOf(due: string | null | undefined): TodoGroupKey {
  if (!due) return "undated";
  if (due < TODAY) return "overdue";
  if (due === TODAY) return "today";
  return due <= "2026-10-04" ? "week" : "later";
}

const GROUP_LABEL: Record<TodoGroupKey, string> = {
  overdue: "逾期",
  today: "今天",
  week: "本周",
  later: "之后",
  undated: "未定截止",
};

/** 模拟 /api/todo：只收已确认、进行中，按截止分五组；按 project_id（逗号多选、none）筛；计数不受状态筛选影响。 */
function serverTodo(items: Task[], today = TODAY) {
  return vi.fn().mockImplementation(async (filters: TodoFilters = {}): Promise<TodoPayload> => {
    const keys = filters.project_id ? filters.project_id.split(",") : [];
    const matched = items.filter((task) => keys.length === 0 || keys.includes(task.project_id ?? "none"));
    const open = matched.filter((task) => task.status === "confirmed" || task.status === "in_progress");
    const counts: TodoPayload["counts"] = {};
    for (const task of matched) counts[task.status] = (counts[task.status] ?? 0) + 1;
    const projectCounts: TodoPayload["project_counts"] = {};
    for (const task of items) {
      const bucket = (projectCounts[task.project_id ?? "none"] ??= {});
      bucket[task.status] = (bucket[task.status] ?? 0) + 1;
    }
    return {
      today,
      week_end: "2026-10-04",
      total: open.length,
      groups: (Object.keys(GROUP_LABEL) as TodoGroupKey[]).map((key) => {
        const inGroup = open.filter((task) => groupOf(task.due_date) === key);
        return { key, label: GROUP_LABEL[key], count: inGroup.length, items: inGroup };
      }),
      counts,
      project_counts: projectCounts,
      projects: PROJECTS,
    };
  });
}

/** 模拟 /api/tasks：按 status（可逗号分隔）筛选、按 limit/offset 分页。 */
function serverTasks(items: Task[]) {
  return vi.fn().mockImplementation(async (filters: TaskFilters = {}) => {
    const statuses = filters.status ? filters.status.split(",") : null;
    const matched = statuses ? items.filter((task) => statuses.includes(task.status)) : items;
    const limit = filters.limit ?? matched.length;
    const offset = filters.offset ?? 0;
    return { items: matched.slice(offset, offset + limit), total: matched.length, limit, offset };
  });
}

const REQ_EDC: LinkOption = {
  kind: "requirement",
  id: "req-edc",
  title: "EDC 对接",
  priority: "P1",
  project_id: "p-yimi",
  project_name: "医米科研用药",
  meeting_id: null,
};
const CAND_SCAN: LinkOption = {
  kind: "candidate",
  id: "cand-scan",
  title: "扫码入组强提醒",
  priority: null,
  project_id: "p-yimi",
  project_name: "医米科研用药",
  meeting_id: "m-edc",
};

function linkOptions(): RequirementOptionsPayload {
  return {
    task_id: "x",
    can_link_candidates: true,
    recommended: [],
    default: null,
    options: [REQ_EDC, CAND_SCAN],
    current: null,
  };
}

function makeClient(items: Task[] = seedTasks(), overrides: Partial<ApiClient> = {}) {
  const setTaskStatus = vi.fn().mockImplementation(async (id: string, status: TaskStatus) => {
    const task = items.find((item) => item.id === id);
    if (task) task.status = status;
    return {};
  });
  return {
    todo: serverTodo(items),
    tasks: serverTasks(items),
    task: vi.fn().mockResolvedValue({ ...items[0], events: [], deliverables: [] }),
    requirements: vi.fn().mockResolvedValue(EMPTY_REQUIREMENTS),
    taskRequirementOptions: vi.fn().mockResolvedValue(linkOptions()),
    confirmTask: vi.fn().mockResolvedValue({}),
    rejectTask: vi.fn().mockResolvedValue({}),
    updateTask: vi.fn().mockResolvedValue({}),
    undoTaskReview: vi.fn().mockResolvedValue({ reverted: [], failed: [] }),
    undoTaskComplete: vi.fn().mockImplementation(async (ids: string[]) => {
      for (const id of ids) {
        const task = items.find((item) => item.id === id);
        if (task) task.status = "confirmed";
      }
      return { reverted: ids, failed: [] };
    }),
    saveProjectSeats: vi.fn().mockResolvedValue({ seats: [] }),
    setTaskStatus,
    ...overrides,
  } as unknown as ApiClient;
}

function renderPage(apiClient: ApiClient, canWrite = true, extra: Partial<Parameters<typeof TasksPage>[0]> = {}) {
  return render(
    <TasksPage
      apiClient={apiClient}
      canWrite={canWrite}
      onClaimCandidate={vi.fn()}
      onOpenMeeting={vi.fn()}
      onOpenProject={vi.fn()}
      onOpenRequirement={vi.fn()}
      projects={[]}
      {...extra}
    />,
  );
}

/** 「未完成」页签里的一行：任务名所在的 <li> */
async function todoRow(title: string): Promise<HTMLElement> {
  const node = await screen.findByText(title);
  const row = node.closest("li");
  if (!row) throw new Error(`未找到 ${title} 所在的待办行`);
  return row as HTMLElement;
}

const region = (name: string) => screen.getByRole("region", { name });

describe("待办：五组与每条的内容", () => {
  it("按逾期、今天、本周、之后、未定截止分组，空组照常显示并写明为空", async () => {
    renderPage(makeClient());
    await screen.findByText("明天晚上先上后台并撤下码，统一验证扫码");

    const names = screen.getAllByRole("heading", { level: 2 }).map((heading) => heading.textContent);
    expect(names).toEqual(["逾期2", "今天0", "本周1", "之后0", "未定截止3"]);

    expect(within(region("今天")).getByText("今天没有到期的任务")).toBeInTheDocument();
    expect(within(region("之后")).getByText("之后没有到期的任务")).toBeInTheDocument();
    expect(within(region("逾期")).getByText("「人身健康」明天开发完、28号再测")).toBeInTheDocument();
    expect(within(region("本周")).getByText("给出定好的积分规则")).toBeInTheDocument();
  });

  it("本周为空时写「本周没有到期的任务」", async () => {
    const items = seedTasks().filter((task) => task.id !== "t-week");
    renderPage(makeClient(items));
    await screen.findByText("安排与华谊的会");

    expect(within(region("本周")).getByText("本周没有到期的任务")).toBeInTheDocument();
  });

  it("逾期的标红并写逾期天数，按接口返回的今天算，不看浏览器时钟", async () => {
    const items = seedTasks();
    renderPage(makeClient(items, { todo: serverTodo(items, "2026-10-10") } as Partial<ApiClient>));

    const row = await todoRow("明天晚上先上后台并撤下码，统一验证扫码");
    expect(row).toHaveTextContent("09-24 · 逾期 16 天");
    expect(within(row).getByText(/逾期 16 天/)).toHaveClass("is-late");
  });

  it("每条显示执行方、截止、项目、需求、来源会议与日期、原话时间锚", async () => {
    renderPage(makeClient());

    const row = await todoRow("明天晚上先上后台并撤下码，统一验证扫码");
    expect(row).toHaveTextContent("我");
    expect(row).toHaveTextContent("09-24 · 逾期 6 天");
    expect(row).toHaveTextContent("黑卡小程序");
    expect(row).toHaveTextContent("未挂需求");
    expect(row).toHaveTextContent("发卡变更与一直拍流程改造沟通 · 09-22");
    expect(within(row).getByRole("button", { name: /原话 00:11:36/ })).toBeInTheDocument();

    const second = await todoRow("「人身健康」明天开发完、28号再测");
    expect(second).toHaveTextContent("AI");
    expect(second).toHaveTextContent("逾期 2 天");
    expect(second).toHaveTextContent("进行中");
  });

  it("点原话时间锚从那一秒播放；没有锚点的只显示会议与日期", async () => {
    const onOpenMeeting = vi.fn();
    renderPage(makeClient(), true, { onOpenMeeting });

    const row = await todoRow("「人身健康」明天开发完、28号再测");
    await userEvent.click(within(row).getByRole("button", { name: /00:06:20/ }));
    expect(onOpenMeeting).toHaveBeenCalledWith("m-card", 380_000);

    const noAnchor = await todoRow("安排与华谊的会");
    expect(noAnchor).toHaveTextContent("EDC 系统选型与产研对接决策 · 09-28");
    expect(within(noAnchor).queryByRole("button", { name: /▶/ })).not.toBeInTheDocument();
  });

  it("没有截止写「截止未定」，没有项目写「未归项目」；挂候选的写「候选：」并和已挂需求区分", async () => {
    const items = seedTasks();
    items.push(
      makeTask("t-cand", "confirmed", "核对扫码入组提醒的触发时机", {
        candidate_id: "cand-scan",
        candidate_title: "扫码入组强提醒",
      }),
      makeTask("t-req", "confirmed", "把 EDC 选型结论写进周报", {
        requirement_id: "req-edc",
        requirement_title: "EDC 对接",
        requirement_priority: "P1",
      }),
    );
    renderPage(makeClient(items));

    const bare = await todoRow("核对扫码入组提醒的触发时机");
    expect(bare).toHaveTextContent("截止未定");
    expect(bare).toHaveTextContent("未归项目");
    expect(within(bare).getByText("候选：扫码入组强提醒")).toHaveClass("todo-row__candidate");
    // 挂着候选也算挂了，按钮写「改挂」
    expect(within(bare).getByRole("button", { name: /^改挂/ })).toBeInTheDocument();

    const attached = await todoRow("把 EDC 选型结论写进周报");
    expect(within(attached).getByRole("button", { name: "EDC 对接" })).toBeInTheDocument();
    expect(within(attached).getByRole("button", { name: /^改挂/ })).toBeInTheDocument();
  });

  it("停滞满 3 天的行内标出天数，不满 3 天不标，也不影响分组", async () => {
    renderPage(makeClient());

    const stalled = await todoRow("与萌总同步 EDC 选型议题");
    expect(stalled).toHaveTextContent("停滞 3 天");
    expect(within(region("未定截止")).getByText("与萌总同步 EDC 选型议题")).toBeInTheDocument();
    expect(await todoRow("确认产研能否派一人对接 EDC")).not.toHaveTextContent("停滞");
  });
});

describe("待办：未定截止折叠", () => {
  function manyUndated() {
    const items = seedTasks().filter((task) => groupOf(task.due_date) !== "undated");
    for (let index = 1; index <= 12; index += 1) {
      items.push(makeTask(`u${index}`, "confirmed", `未定截止任务 ${index}`, { ...YIMI }));
    }
    return items;
  }

  it("默认显示前 8 条，其余折成「还有 N 条」，点开就地展开，展开后可收起", async () => {
    renderPage(makeClient(manyUndated()));
    await screen.findByText("未定截止任务 1");

    const undated = region("未定截止");
    expect(within(undated).getAllByRole("listitem")).toHaveLength(8);
    expect(within(undated).queryByText("未定截止任务 9")).not.toBeInTheDocument();

    await userEvent.click(within(undated).getByRole("button", { name: "还有 4 条" }));
    expect(within(undated).getAllByRole("listitem")).toHaveLength(12);
    expect(within(undated).getByText("未定截止任务 12")).toBeInTheDocument();

    await userEvent.click(within(undated).getByRole("button", { name: "收起" }));
    expect(within(undated).getAllByRole("listitem")).toHaveLength(8);
    expect(within(undated).getByRole("button", { name: "还有 4 条" })).toBeInTheDocument();
  });

  it("不超过 8 条时不出折叠按钮", async () => {
    renderPage(makeClient());
    await screen.findByText("安排与华谊的会");

    expect(screen.queryByRole("button", { name: /还有/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "收起" })).not.toBeInTheDocument();
  });
});

describe("待办：完成与撤销", () => {
  it("点［完成］一步完成：任务离开分组，组计数和页头总数同步减 1", async () => {
    const apiClient = makeClient();
    renderPage(apiClient);
    const row = await todoRow("安排与华谊的会");
    expect(screen.getByText("件 未完成").previousElementSibling).toHaveTextContent("6");

    await userEvent.click(within(row).getByRole("button", { name: /^完成「/ }));

    expect(apiClient.setTaskStatus).toHaveBeenCalledWith("t-huayi", "done");
    await waitFor(() => expect(screen.queryByText("安排与华谊的会", { selector: ".todo-row__title" })).not.toBeInTheDocument());
    expect(screen.getAllByRole("heading", { level: 2 }).map((heading) => heading.textContent)).toContain("未定截止2");
    expect(screen.getByText("件 未完成").previousElementSibling).toHaveTextContent("5");
    expect(screen.getByRole("tab", { name: /未完成/ })).toHaveTextContent("5");
    expect(screen.getByRole("status")).toHaveTextContent("已完成「安排与华谊的会」");
  });

  it("左侧勾选框也是完成", async () => {
    const apiClient = makeClient();
    renderPage(apiClient);
    await todoRow("给出定好的积分规则");

    await userEvent.click(screen.getByRole("checkbox", { name: "完成「给出定好的积分规则」" }));

    expect(apiClient.setTaskStatus).toHaveBeenCalledWith("t-week", "done");
  });

  it("提示条上的［撤销］调用 undoTaskComplete，任务回到分组", async () => {
    const apiClient = makeClient();
    renderPage(apiClient);
    const row = await todoRow("安排与华谊的会");
    await userEvent.click(within(row).getByRole("button", { name: /^完成「/ }));

    await userEvent.click(await screen.findByRole("button", { name: "撤销" }));

    expect(apiClient.undoTaskComplete).toHaveBeenCalledWith(["t-huayi"]);
    expect(await screen.findByText("安排与华谊的会", { selector: ".todo-row__title" })).toBeInTheDocument();
    expect(screen.getByText("件 未完成").previousElementSibling).toHaveTextContent("6");
  });

  it("超过 10 分钟撤不了时提示原因，任务仍是已完成", async () => {
    const undoTaskComplete = vi
      .fn()
      .mockResolvedValue({ reverted: [], failed: [{ task_id: "t-huayi", error: "完成已超过 10 分钟，不能撤销" }] });
    renderPage(makeClient(seedTasks(), { undoTaskComplete } as Partial<ApiClient>));
    await userEvent.click(within(await todoRow("安排与华谊的会")).getByRole("button", { name: /^完成「/ }));

    await userEvent.click(await screen.findByRole("button", { name: "撤销" }));

    expect(await screen.findByText("完成已超过 10 分钟，不能撤销")).toBeInTheDocument();
  });

  it("提示条显示约 10 秒后收起", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      renderPage(makeClient());
      await userEvent.click(within(await todoRow("安排与华谊的会")).getByRole("button", { name: /^完成「/ }));
      expect(await screen.findByRole("status")).toBeInTheDocument();

      await act(async () => {
        await vi.advanceTimersByTimeAsync(9_000);
      });
      expect(screen.getByRole("status")).toBeInTheDocument();
      await act(async () => {
        await vi.advanceTimersByTimeAsync(1_500);
      });
      expect(screen.queryByRole("status")).not.toBeInTheDocument();
    } finally {
      vi.useRealTimers();
    }
  });

  it("操作进行中不会连发第二个写请求", async () => {
    let resolveDone!: (value: unknown) => void;
    const setTaskStatus = vi.fn().mockReturnValue(new Promise((resolve) => { resolveDone = resolve; }));
    renderPage(makeClient(seedTasks(), { setTaskStatus } as Partial<ApiClient>));
    const button = within(await todoRow("安排与华谊的会")).getByRole("button", { name: /^完成「/ });

    await userEvent.click(button);
    await userEvent.click(button);

    expect(setTaskStatus).toHaveBeenCalledTimes(1);
    resolveDone({});
  });
});

describe("待办：挂到需求", () => {
  async function openPicker(title: string) {
    const row = await todoRow(title);
    await userEvent.click(within(row).getByRole("button", { name: /^(挂到需求|改挂)：/ }));
    return screen.findByRole("dialog", { name: /^挂到需求/ });
  }

  it("选需求：只写 requirement_id，提示「已挂到「需求名」」，撤销时原来都没挂就两个都写 null", async () => {
    const apiClient = makeClient();
    renderPage(apiClient);

    const picker = await openPicker("安排与华谊的会");
    await userEvent.click(await within(picker).findByRole("option", { name: /EDC 对接/ }));

    expect(apiClient.updateTask).toHaveBeenLastCalledWith("t-huayi", { requirement_id: "req-edc" });
    expect(await screen.findByText("已挂到「EDC 对接」")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "撤销" }));
    expect(apiClient.updateTask).toHaveBeenLastCalledWith("t-huayi", { requirement_id: null, candidate_id: null });
  });

  it("选候选：只写 candidate_id，撤销时原来挂着需求就写回原需求", async () => {
    const items = seedTasks();
    Object.assign(items.find((task) => task.id === "t-sync")!, { requirement_id: "req-old", requirement_title: "旧需求" });
    const apiClient = makeClient(items);
    renderPage(apiClient);

    const picker = await openPicker("与萌总同步 EDC 选型议题");
    await userEvent.click(await within(picker).findByRole("option", { name: /扫码入组强提醒/ }));

    expect(apiClient.updateTask).toHaveBeenLastCalledWith("t-sync", { candidate_id: "cand-scan" });
    expect(await screen.findByText("已挂到「扫码入组强提醒」")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "撤销" }));
    expect(apiClient.updateTask).toHaveBeenLastCalledWith("t-sync", { requirement_id: "req-old" });
  });

  it("选「不挂需求」：两个都写 null，撤销时原来挂着候选就写回原候选", async () => {
    const items = seedTasks();
    Object.assign(items.find((task) => task.id === "t-staff")!, { candidate_id: "cand-scan", candidate_title: "扫码入组强提醒" });
    const apiClient = makeClient(items);
    renderPage(apiClient);

    const picker = await openPicker("确认产研能否派一人对接 EDC");
    await userEvent.click(within(picker).getByRole("button", { name: "不挂需求" }));

    expect(apiClient.updateTask).toHaveBeenLastCalledWith("t-staff", { requirement_id: null, candidate_id: null });

    await userEvent.click(await screen.findByRole("button", { name: "撤销" }));
    expect(apiClient.updateTask).toHaveBeenLastCalledWith("t-staff", { candidate_id: "cand-scan" });
  });

  it("整页同一时刻只开一个选择器", async () => {
    renderPage(makeClient());
    await openPicker("安排与华谊的会");

    const other = await todoRow("确认产研能否派一人对接 EDC");
    await userEvent.click(within(other).getByRole("button", { name: /^挂到需求/ }));

    expect(screen.getAllByRole("dialog", { name: /^挂到需求/ })).toHaveLength(1);
  });
});

describe("待办：⋯ 更多菜单", () => {
  it("已确认的有「开始处理」没有「回退」，点了置为进行中", async () => {
    const apiClient = makeClient();
    renderPage(apiClient);

    await userEvent.click(within(await todoRow("安排与华谊的会")).getByRole("button", { name: /^更多操作/ }));
    expect(screen.queryByRole("menuitem", { name: "回退" })).not.toBeInTheDocument();
    expect(screen.getByRole("menuitem", { name: "修改" })).toBeInTheDocument();
    await userEvent.click(screen.getByRole("menuitem", { name: "开始处理" }));

    expect(apiClient.setTaskStatus).toHaveBeenCalledWith("t-huayi", "in_progress");
  });

  it("进行中的有「回退」没有「开始处理」，点了回到已确认", async () => {
    const apiClient = makeClient();
    renderPage(apiClient);

    await userEvent.click(within(await todoRow("「人身健康」明天开发完、28号再测")).getByRole("button", { name: /^更多操作/ }));
    expect(screen.queryByRole("menuitem", { name: "开始处理" })).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("menuitem", { name: "回退" }));

    expect(apiClient.setTaskStatus).toHaveBeenCalledWith("t-late-2", "confirmed");
  });

  it("取消任务是红字菜单项，点了置为已取消", async () => {
    const apiClient = makeClient();
    renderPage(apiClient);

    await userEvent.click(within(await todoRow("安排与华谊的会")).getByRole("button", { name: /^更多操作/ }));
    const cancel = screen.getByRole("menuitem", { name: "取消任务" });
    expect(cancel).toHaveClass("is-danger");
    await userEvent.click(cancel);

    expect(apiClient.setTaskStatus).toHaveBeenCalledWith("t-huayi", "cancelled");
  });

  it("「修改」打开修改弹窗", async () => {
    renderPage(makeClient());

    await userEvent.click(within(await todoRow("安排与华谊的会")).getByRole("button", { name: /^更多操作/ }));
    await userEvent.click(screen.getByRole("menuitem", { name: "修改" }));

    expect(await screen.findByRole("dialog", { name: "修改任务" })).toBeInTheDocument();
  });

  it("只读模式下不出任何行内操作", async () => {
    renderPage(makeClient(), false);

    const row = await todoRow("安排与华谊的会");
    expect(within(row).queryByRole("button", { name: /^完成「/ })).not.toBeInTheDocument();
    expect(within(row).queryByRole("button", { name: /^更多操作/ })).not.toBeInTheDocument();
    expect(within(row).queryByRole("button", { name: /^挂到需求/ })).not.toBeInTheDocument();
    expect(within(row).getByRole("checkbox")).toBeDisabled();
  });
});

describe("待办：页头、页签与侧栏之外的壳", () => {
  it("页头写待办，副标题和大数字「件 未完成」", async () => {
    renderPage(makeClient());
    await screen.findByText("安排与华谊的会");

    expect(screen.getByRole("heading", { level: 1, name: "待办" })).toBeInTheDocument();
    expect(screen.getByText("会上答应的事和需求拆出来的步骤，都在这里按截止排")).toBeInTheDocument();
    expect(screen.getByText("件 未完成").previousElementSibling).toHaveTextContent("6");
  });

  it("页签是待确认、未完成、已完成、已取消、已过期，默认在未完成；没有「全部」「进行中」", async () => {
    renderPage(makeClient());
    await screen.findByText("安排与华谊的会");

    const tabs = screen.getAllByRole("tab");
    expect(tabs.map((tab) => tab.textContent)).toEqual(["待确认1", "未完成6", "已完成1", "已取消1", "已过期1"]);
    expect(screen.getByRole("tab", { name: /未完成/ })).toHaveAttribute("aria-selected", "true");
    // 有待确认时数字旁有小红点
    expect(screen.getByRole("tab", { name: /待确认/ })).toHaveClass("has-dot");
    expect(screen.getByRole("tab", { name: /已完成/ })).not.toHaveClass("has-dot");
  });

  it("本机存过的旧页签值 all、in_progress 迁到未完成", async () => {
    window.sessionStorage.setItem("meeting-workbench:view:tasks.activeTab", JSON.stringify("in_progress"));
    renderPage(makeClient());

    expect(await screen.findByRole("tab", { name: /未完成/ })).toHaveAttribute("aria-selected", "true");
    expect(await screen.findByText("安排与华谊的会")).toBeInTheDocument();
  });
});

describe("待办：我的方向条", () => {
  const seatChip = (name: RegExp) =>
    within(screen.getByRole("group", { name: "已排座次的项目" })).getByRole("button", { name });

  it("条上每个项目的数字跟随当前页签；旁边有「未归项目」块", async () => {
    const items = seedTasks();
    items.push(makeTask("t-loose", "confirmed", "整理发卡名单"));
    renderPage(makeClient(items));
    await screen.findByText("整理发卡名单");

    expect(seatChip(/医米科研用药/)).toHaveTextContent("3"); // 未完成：医米 3 条
    expect(seatChip(/黑卡小程序/)).toHaveTextContent("3");
    expect(screen.getByRole("button", { name: /^未归项目/ })).toHaveTextContent("1");

    await userEvent.click(screen.getByRole("tab", { name: /已完成/ }));
    await waitFor(() => expect(seatChip(/医米科研用药/)).toHaveTextContent("1")); // 已完成：医米 1 条
    expect(seatChip(/黑卡小程序/)).toHaveTextContent("0");
  });

  it("「未归项目」虚线胶囊在方向条内，和芯片、提示同在一个白底条里", async () => {
    renderPage(makeClient());
    await screen.findByText("安排与华谊的会");

    const none = screen.getByRole("button", { name: /^未归项目/ });
    const bar = none.closest(".todo-direction")!;
    expect(bar).toContainElement(screen.getByText("我的方向"));
    expect(bar).toContainElement(screen.getByText("拖动排序，点选筛选"));
  });

  it("项目多选后请求带逗号拼起来的 project_id，「未归项目」用 none", async () => {
    const apiClient = makeClient();
    renderPage(apiClient);
    await screen.findByText("安排与华谊的会");

    await userEvent.click(seatChip(/医米科研用药/));
    await waitFor(() => expect(apiClient.todo).toHaveBeenLastCalledWith(expect.objectContaining({ project_id: "p-yimi" })));
    // 只剩医米的三条
    expect(screen.queryByText("给出定好的积分规则")).not.toBeInTheDocument();

    await userEvent.click(seatChip(/黑卡小程序/));
    await waitFor(() =>
      expect(apiClient.todo).toHaveBeenLastCalledWith(expect.objectContaining({ project_id: "p-yimi,p-heika" })),
    );

    await userEvent.click(screen.getByRole("button", { name: /^未归项目/ }));
    await waitFor(() =>
      expect(apiClient.todo).toHaveBeenLastCalledWith(expect.objectContaining({ project_id: "p-yimi,p-heika,none" })),
    );
    expect(screen.getByRole("button", { name: /^未归项目/ })).toHaveAttribute("aria-pressed", "true");
  });

  it("分页清单的请求也带 project_id", async () => {
    const apiClient = makeClient();
    renderPage(apiClient);
    await screen.findByText("安排与华谊的会");
    await userEvent.click(seatChip(/医米科研用药/));
    await userEvent.click(screen.getByRole("tab", { name: /已完成/ }));

    await waitFor(() =>
      expect(apiClient.tasks).toHaveBeenLastCalledWith(
        expect.objectContaining({ status: "done", project_id: "p-yimi" }),
      ),
    );
  });

  it("拖动排序和需求池走同一个座次接口：把「未排座次」的项目排入座次", async () => {
    const apiClient = makeClient();
    renderPage(apiClient);
    await screen.findByText("安排与华谊的会");

    await userEvent.click(screen.getByRole("button", { name: /未排座次/ }));
    await userEvent.click(screen.getByRole("button", { name: "排入座次" }));

    expect(apiClient.saveProjectSeats).toHaveBeenCalledWith(["p-yimi", "p-heika", "p-cvm"]);
    expect(await screen.findByText("座次已保存")).toBeInTheDocument();
  });
});

describe("审查补丁", () => {
  it("审核卡打开的修改弹窗只保存不确认：按钮写「保存」，走 updateTask，卡上选的需求不被清掉，保存后有提示", async () => {
    const items = seedTasks();
    const apiClient = makeClient(items);
    renderPage(apiClient);
    await userEvent.click(await screen.findByRole("tab", { name: /待确认/ }));
    await screen.findByTestId("review-cards");

    act(() => panelProps().onEditTask(items.find((task) => task.id === "t-pending")!));
    await userEvent.click(await screen.findByRole("button", { name: "保存" }));

    expect(apiClient.confirmTask).not.toHaveBeenCalled();
    expect(apiClient.updateTask).toHaveBeenCalledTimes(1);
    expect(vi.mocked(apiClient.updateTask).mock.calls[0][1]).not.toHaveProperty("requirement_id");
    expect(await screen.findByRole("status")).toHaveTextContent("已保存「确认发卡名单口径」");
  });

  it("已完成页签：完成不满 10 分钟的行有［撤销完成］，过了窗口不显示", async () => {
    const items = seedTasks();
    const fresh = makeTask("t-fresh", "done", "给出定好的积分规则", { status_changed_at: new Date(Date.now() - 2 * 60_000).toISOString() });
    const stale = makeTask("t-stale", "done", "安排与华谊的会", { status_changed_at: new Date(Date.now() - 11 * 60_000).toISOString() });
    items.push(fresh, stale);
    const apiClient = makeClient(items);
    renderPage(apiClient);
    await userEvent.click(await screen.findByRole("tab", { name: /已完成/ }));

    const staleRow = (await screen.findByText("安排与华谊的会")).closest("tr")!;
    expect(within(staleRow).queryByRole("button", { name: /撤销完成/ })).not.toBeInTheDocument();
    const freshRow = (await screen.findByText("给出定好的积分规则")).closest("tr")!;
    await userEvent.click(within(freshRow).getByRole("button", { name: /^撤销完成/ }));

    expect(apiClient.undoTaskComplete).toHaveBeenCalledWith(["t-fresh"]);
  });

  it("结果提示用共用的底部深色条（不占文档流），带撤销；失败用错误样式", async () => {
    const apiClient = makeClient();
    renderPage(apiClient);
    await userEvent.click(within(await todoRow("安排与华谊的会")).getByRole("button", { name: /^完成「/ }));

    const toast = await screen.findByRole("status");
    expect(toast).toHaveClass("app-toast");
    expect(within(toast).getByRole("button", { name: "撤销" })).toBeInTheDocument();

    (apiClient.setTaskStatus as ReturnType<typeof vi.fn>).mockRejectedValueOnce(
      new Error("任务已经是「已完成」，不能改成「已完成」，刷新后再看"),
    );
    await userEvent.click(within(await todoRow("确认产研能否派一人对接 EDC")).getByRole("button", { name: /^完成「/ }));
    expect(await screen.findByRole("alert")).toHaveClass("app-toast--error");
  });

  it("挂到需求选择器和 ⋯ 菜单互斥：开一个就收起另一个", async () => {
    renderPage(makeClient());
    const row = await todoRow("安排与华谊的会");

    await userEvent.click(within(row).getByRole("button", { name: /^挂到需求/ }));
    expect(await screen.findByRole("dialog", { name: "挂到需求" })).toBeInTheDocument();
    await userEvent.click(within(row).getByRole("button", { name: /^更多操作/ }));
    expect(screen.queryByRole("dialog", { name: "挂到需求" })).not.toBeInTheDocument();
    expect(screen.getByRole("menu")).toBeInTheDocument();

    await userEvent.click(within(row).getByRole("button", { name: /^挂到需求/ }));
    expect(screen.queryByRole("menu")).not.toBeInTheDocument();
    expect(await screen.findByRole("dialog", { name: "挂到需求" })).toBeInTheDocument();
  });

  it("本机存着不合格的日期时，查询不应用并就地提示", async () => {
    window.sessionStorage.setItem("meeting-workbench:view:tasks.dateFromDraft", JSON.stringify("2026-9-3x"));
    const apiClient = makeClient();
    renderPage(apiClient);
    await screen.findByText("安排与华谊的会");
    (apiClient.todo as ReturnType<typeof vi.fn>).mockClear();

    await userEvent.click(screen.getByRole("button", { name: "查询" }));

    expect(screen.getByRole("alert")).toHaveTextContent("年-月-日");
    expect(apiClient.todo).not.toHaveBeenCalled();
  });

  it("日期输入框限定 2000～2099 年", async () => {
    renderPage(makeClient());
    await screen.findByText("安排与华谊的会");

    for (const name of ["开始日期", "结束日期"]) {
      expect(screen.getByLabelText(name)).toHaveAttribute("min", "2000-01-01");
      expect(screen.getByLabelText(name)).toHaveAttribute("max", "2099-12-31");
    }
  });

  it("读取失败时显示后端给的原因，并带［重置筛选］", async () => {
    const items = seedTasks();
    const ok = serverTodo(items);
    const todo = vi
      .fn()
      .mockImplementation(async (filters: TodoFilters) => {
        if (filters.q) throw new ApiError("日期格式不对：meeting_date_from", 400, {});
        return ok(filters);
      });
    renderPage(makeClient(items, { todo } as Partial<ApiClient>));
    await screen.findByText("安排与华谊的会");
    await userEvent.type(screen.getByPlaceholderText("输入任务名称"), "坏条件");
    await userEvent.click(screen.getByRole("button", { name: "查询" }));

    expect(await screen.findByText("任务读取失败：日期格式不对：meeting_date_from")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "重置筛选" }));

    expect(await screen.findByText("安排与华谊的会")).toBeInTheDocument();
    expect(screen.queryByText(/任务读取失败/)).not.toBeInTheDocument();
  });

  it("未定截止展开后，切页签再回来仍是展开的", async () => {
    const items = seedTasks().filter((task) => groupOf(task.due_date) !== "undated");
    for (let index = 1; index <= 10; index += 1) items.push(makeTask(`u${index}`, "confirmed", `未定截止任务 ${index}`, { ...YIMI }));
    renderPage(makeClient(items));
    await screen.findByText("未定截止任务 1");
    await userEvent.click(screen.getByRole("button", { name: "还有 2 条" }));

    await userEvent.click(screen.getByRole("tab", { name: /已完成/ }));
    await userEvent.click(screen.getByRole("tab", { name: /未完成/ }));

    expect(await screen.findByText("未定截止任务 10")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "收起" })).toBeInTheDocument();
  });

  it("切页签时清掉上一个页签的提示", async () => {
    const setTaskStatus = vi.fn().mockRejectedValue(new Error("任务状态已变，请刷新"));
    renderPage(makeClient(seedTasks(), { setTaskStatus } as Partial<ApiClient>));
    await userEvent.click(within(await todoRow("安排与华谊的会")).getByRole("button", { name: /^完成「/ }));
    expect(await screen.findByRole("alert")).toHaveTextContent("任务状态已变，请刷新");

    await userEvent.click(screen.getByRole("tab", { name: /已完成/ }));

    expect(screen.queryByText("任务状态已变，请刷新")).not.toBeInTheDocument();
  });

  it("失败提示 10 秒后也自动收起", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      const setTaskStatus = vi.fn().mockRejectedValue(new Error("任务状态已变，请刷新"));
      renderPage(makeClient(seedTasks(), { setTaskStatus } as Partial<ApiClient>));
      await userEvent.click(within(await todoRow("安排与华谊的会")).getByRole("button", { name: /^完成「/ }));
      expect(await screen.findByRole("alert")).toBeInTheDocument();

      await act(async () => {
        await vi.advanceTimersByTimeAsync(9_000);
      });
      expect(screen.getByRole("alert")).toBeInTheDocument();
      await act(async () => {
        await vi.advanceTimersByTimeAsync(1_500);
      });
      expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    } finally {
      vi.useRealTimers();
    }
  });

  it("来源列：会议名、日期分开渲染，title 带上「会议名 · 日期」", async () => {
    renderPage(makeClient());

    const row = await todoRow("明天晚上先上后台并撤下码，统一验证扫码");
    expect(within(row).getByTitle("发卡变更与一直拍流程改造沟通 · 09-22")).toBeInTheDocument();
    expect(row.querySelector(".todo-row__meeting-date")).toHaveTextContent("· 09-22");
  });
});

describe("待确认页签", () => {
  async function openPending(apiClient: ApiClient, extra: Partial<Parameters<typeof TasksPage>[0]> = {}) {
    renderPage(apiClient, true, extra);
    await userEvent.click(await screen.findByRole("tab", { name: /待确认/ }));
    await screen.findByTestId("review-cards");
  }

  it("渲染审核卡面板，页签下方一行提示，面板的筛选只传会议这一层的", async () => {
    await openPending(makeClient());

    expect(screen.getByText("待确认放 7 天没处理会自动过期")).toBeInTheDocument();
    expect(panelProps().canWrite).toBe(true);
    expect(panelProps().filters).toEqual({});

    await userEvent.click(
      within(screen.getByRole("group", { name: "已排座次的项目" })).getByRole("button", { name: /医米科研用药/ }),
    );
    await userEvent.type(screen.getByLabelText("开始日期"), "2026-09-01");
    await userEvent.type(screen.getByLabelText("结束日期"), "2026-09-30");
    await userEvent.type(screen.getByPlaceholderText("输入任务名称"), "EDC");
    await userEvent.click(screen.getByRole("button", { name: "查询" }));

    await waitFor(() =>
      expect(panelProps().filters).toEqual({
        project_id: "p-yimi",
        meeting_date_from: "2026-09-01",
        meeting_date_to: "2026-09-30",
      }),
    );
  });

  it("原话时间锚、认领候选直接交给页面传进来的回调", async () => {
    const onOpenMeeting = vi.fn();
    const onClaimCandidate = vi.fn();
    await openPending(makeClient(), { onOpenMeeting, onClaimCandidate });

    panelProps().onOpenMeeting("m-card", 696_000);
    panelProps().onClaimCandidate("cand-scan");

    expect(onOpenMeeting).toHaveBeenCalledWith("m-card", 696_000);
    expect(onClaimCandidate).toHaveBeenCalledWith("cand-scan");
  });

  it("面板里点「修改」打开修改弹窗", async () => {
    const items = seedTasks();
    await openPending(makeClient(items));

    act(() => panelProps().onEditTask(items.find((task) => task.id === "t-pending")!));

    expect(await screen.findByRole("dialog", { name: "修改任务" })).toBeInTheDocument();
  });

  it("面板的结果提示显示在页面的提示条里，带撤销时点［撤销］调到面板给的函数", async () => {
    await openPending(makeClient());
    const undo = vi.fn().mockResolvedValue(undefined);

    act(() => panelProps().onNotify("已确认 3 项", undo));

    expect(screen.getByRole("status")).toHaveTextContent("已确认 3 项");
    await userEvent.click(screen.getByRole("button", { name: "撤销" }));
    expect(undo).toHaveBeenCalledTimes(1);
  });

  it("面板里有写操作后刷新页签计数", async () => {
    const apiClient = makeClient();
    await openPending(apiClient);
    const before = (apiClient.todo as ReturnType<typeof vi.fn>).mock.calls.length;

    act(() => panelProps().onChanged());

    await waitFor(() => expect((apiClient.todo as ReturnType<typeof vi.fn>).mock.calls.length).toBeGreaterThan(before));
  });
});

describe("已完成、已取消、已过期三个页签", () => {
  async function openTab(name: RegExp, apiClient = makeClient()) {
    renderPage(apiClient);
    await userEvent.click(await screen.findByRole("tab", { name }));
    return apiClient;
  }

  async function rowOf(title: string): Promise<HTMLElement> {
    const row = (await screen.findByText(title)).closest("tr");
    if (!row) throw new Error(`未找到 ${title} 所在的任务行`);
    return row as HTMLElement;
  }

  it("切页签按该状态向服务端取清单", async () => {
    const apiClient = await openTab(/已完成/);

    expect(await rowOf("交付操作手册")).toBeInTheDocument();
    expect(apiClient.tasks).toHaveBeenLastCalledWith(expect.objectContaining({ status: "done", limit: 10, offset: 0 }));
    expect(screen.queryByText("平台级模板页")).not.toBeInTheDocument();
  });

  it("已完成任务不出状态按钮", async () => {
    await openTab(/已完成/);

    const row = await rowOf("交付操作手册");
    expect(within(row).queryByRole("button", { name: /^恢复/ })).not.toBeInTheDocument();
    expect(within(row).queryByRole("button", { name: /^更多操作/ })).not.toBeInTheDocument();
  });

  it("已取消任务可以恢复成已确认", async () => {
    const apiClient = await openTab(/已取消/);

    await userEvent.click(within(await rowOf("平台级模板页")).getByRole("button", { name: /^恢复/ }));

    expect(apiClient.setTaskStatus).toHaveBeenCalledWith("t-cancelled", "confirmed");
  });

  it("已过期草稿：页头注明 7 天自动归到这里；可以恢复到待确认", async () => {
    const apiClient = await openTab(/已过期/);

    expect(screen.getByText("待确认放 7 天没处理自动归到这里")).toBeInTheDocument();
    await userEvent.click(within(await rowOf("整理两周前的草稿")).getByRole("button", { name: /^恢复/ }));

    expect(apiClient.setTaskStatus).toHaveBeenCalledWith("t-expired", "pending_confirm");
  });

  it("已过期草稿可以从更多菜单直接确认或驳回，确认后可撤销", async () => {
    const undoTaskReview = vi.fn().mockResolvedValue({ reverted: ["t-expired"], failed: [] });
    const apiClient = await openTab(/已过期/, makeClient(seedTasks(), { undoTaskReview } as Partial<ApiClient>));

    await userEvent.click(within(await rowOf("整理两周前的草稿")).getByRole("button", { name: /^更多操作/ }));
    await userEvent.click(screen.getByRole("menuitem", { name: "直接确认" }));
    expect(apiClient.confirmTask).toHaveBeenCalledWith("t-expired", {});

    await userEvent.click(await screen.findByRole("button", { name: "撤销" }));
    expect(undoTaskReview).toHaveBeenCalledWith(["t-expired"]);
    expect(await screen.findByText("已撤销 1 项，恢复为待确认")).toBeInTheDocument();

    await userEvent.click(within(await rowOf("整理两周前的草稿")).getByRole("button", { name: /^更多操作/ }));
    await userEvent.click(screen.getByRole("menuitem", { name: "驳回" }));
    expect(apiClient.rejectTask).toHaveBeenCalledWith("t-expired");
  });

  it("行本身聚焦时按回车打开任务抽屉，行内按钮聚焦时回车不劫持", async () => {
    await openTab(/已取消/);
    const row = await rowOf("平台级模板页");

    fireEvent.keyDown(within(row).getByRole("button", { name: /^恢复/ }), { key: "Enter" });
    expect(screen.queryByRole("dialog", { name: "任务详情" })).not.toBeInTheDocument();

    fireEvent.keyDown(row, { key: "Enter" });
    expect(screen.getByRole("dialog", { name: "任务详情" })).toBeInTheDocument();
  });

  it("有项目、需求时显示名称并可跳转，没有时显示—", async () => {
    const items = seedTasks();
    Object.assign(items.find((task) => task.id === "t-done")!, {
      requirement_id: "req-edc",
      requirement_title: "EDC 对接",
      requirement_priority: "P0",
    });
    const onOpenProject = vi.fn();
    const onOpenRequirement = vi.fn();
    renderPage(makeClient(items), true, { onOpenProject, onOpenRequirement });
    await userEvent.click(await screen.findByRole("tab", { name: /已完成/ }));

    const row = await rowOf("交付操作手册");
    expect(within(row).getByText("P0")).toBeInTheDocument();
    await userEvent.click(within(row).getByRole("button", { name: "医米科研用药" }));
    expect(onOpenProject).toHaveBeenCalledWith("p-yimi");
    await userEvent.click(within(row).getByRole("button", { name: "EDC 对接" }));
    expect(onOpenRequirement).toHaveBeenCalledWith("req-edc");
  });

  it("每页 10 条，服务端分页，点页码条翻页", async () => {
    const items = Array.from({ length: 23 }, (_, index) => makeTask(`d${index}`, "done", `已完成任务 ${index}`));
    const apiClient = makeClient(items);
    renderPage(apiClient);
    await userEvent.click(await screen.findByRole("tab", { name: /已完成/ }));

    expect(await screen.findByText("已完成任务 0")).toBeInTheDocument();
    expect(screen.getByText("共 23 条")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "2" }));
    expect(apiClient.tasks).toHaveBeenLastCalledWith(expect.objectContaining({ limit: 10, offset: 10 }));
    expect(await screen.findByText("已完成任务 10")).toBeInTheDocument();
  });
});

describe("查询区", () => {
  it("没有所属项目下拉：项目筛选在「我的方向」条上", async () => {
    renderPage(makeClient());
    await screen.findByText("安排与华谊的会");

    expect(screen.queryByLabelText("所属项目")).not.toBeInTheDocument();
  });

  it("按任务名称、执行方和来源会议日期查询，重置清空全部条件", async () => {
    const apiClient = makeClient();
    renderPage(apiClient);
    await screen.findByText("安排与华谊的会");

    await userEvent.type(screen.getByPlaceholderText("输入任务名称"), "EDC");
    await userEvent.selectOptions(screen.getByLabelText("执行方"), "me");
    await userEvent.type(screen.getByLabelText("开始日期"), "2026-09-01");
    await userEvent.type(screen.getByLabelText("结束日期"), "2026-09-15");
    await userEvent.click(screen.getByRole("button", { name: "查询" }));

    await waitFor(() =>
      expect(apiClient.todo).toHaveBeenLastCalledWith({
        q: "EDC",
        assignee: "me",
        meeting_date_from: "2026-09-01",
        meeting_date_to: "2026-09-15",
      }),
    );

    await userEvent.click(screen.getByRole("button", { name: "重置" }));
    await waitFor(() => expect((apiClient.todo as ReturnType<typeof vi.fn>).mock.lastCall?.[0]).toEqual({}));
    expect(screen.getByPlaceholderText("输入任务名称")).toHaveValue("");
  });

  it("所属需求下拉：只选一个项目时收窄到该项目的需求，查询时带 requirement_id；换项目清掉所选需求", async () => {
    const requirements = vi.fn().mockImplementation(async (filters: { project_id?: string }) =>
      filters.project_id === "p-yimi"
        ? {
            ...EMPTY_REQUIREMENTS,
            items: [
              { id: "req-edc", project_id: "p-yimi", project_name: "医米科研用药", project_color: "#fb7b30", title: "EDC 对接", priority: "P0", status: "active", created_at: "", updated_at: "", open_task_count: 3, meeting_count: 2, latest_meeting_date: null, folder_count: 2 },
            ],
            total: 1,
          }
        : EMPTY_REQUIREMENTS,
    );
    const apiClient = makeClient(seedTasks(), { requirements } as Partial<ApiClient>);
    renderPage(apiClient);
    await screen.findByText("安排与华谊的会");

    await userEvent.click(
      within(screen.getByRole("group", { name: "已排座次的项目" })).getByRole("button", { name: /医米科研用药/ }),
    );
    expect(await screen.findByRole("option", { name: /EDC 对接/ })).toBeInTheDocument();
    await userEvent.selectOptions(screen.getByLabelText("所属需求"), "req-edc");
    await userEvent.click(screen.getByRole("button", { name: "查询" }));
    await waitFor(() =>
      expect(apiClient.todo).toHaveBeenLastCalledWith(expect.objectContaining({ project_id: "p-yimi", requirement_id: "req-edc" })),
    );

    await userEvent.click(
      within(screen.getByRole("group", { name: "已排座次的项目" })).getByRole("button", { name: /黑卡小程序/ }),
    );
    await waitFor(() =>
      expect((apiClient.todo as ReturnType<typeof vi.fn>).mock.lastCall?.[0]).not.toHaveProperty("requirement_id"),
    );
    expect(screen.getByLabelText("所属需求")).toHaveValue("");
  });
});

describe("离开再回来保留检索条件", () => {
  it("卸载后重新挂载，页签、项目选择和已应用的查询都还在", async () => {
    const apiClient = makeClient();
    const first = renderPage(apiClient);
    await screen.findByText("安排与华谊的会");

    await userEvent.click(
      within(screen.getByRole("group", { name: "已排座次的项目" })).getByRole("button", { name: /医米科研用药/ }),
    );
    await userEvent.type(screen.getByPlaceholderText("输入任务名称"), "EDC");
    await userEvent.click(screen.getByRole("button", { name: "查询" }));
    await waitFor(() => expect(apiClient.todo).toHaveBeenLastCalledWith(expect.objectContaining({ q: "EDC" })));
    const lastFilters = (apiClient.todo as ReturnType<typeof vi.fn>).mock.lastCall?.[0];

    first.unmount();
    (apiClient.todo as ReturnType<typeof vi.fn>).mockClear();
    renderPage(apiClient);

    expect(screen.getByPlaceholderText("输入任务名称")).toHaveValue("EDC");
    expect(screen.getByRole("tab", { name: /未完成/ })).toHaveAttribute("aria-selected", "true");
    await waitFor(() => expect(apiClient.todo).toHaveBeenCalledWith(lastFilters));
    expect(lastFilters).toEqual({ project_id: "p-yimi", q: "EDC" });
  });
});
