import { createEvent, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { ApiError, type ApiClient } from "../../../api";
import type {
  ProjectBoard,
  ProjectRecordingRow,
  ProjectWorkPayload,
  ProjectWorkRequirement,
  Task,
} from "../../../types";
import { ProjectDetailPage } from "../../ProjectDetailPage";
import { YIMI, requirementItem } from "../../pool/poolFixtures";

// 数据照口径书 P02「医米科研用药」：进行中两条（京东科研仓对接任务全完成、赠药横跳拦截三条未完成）、
// 已完成「新患者注册：五个问题前置」、已搁置「EDC 系统选型」、没挂需求的三条、待认领候选「京东仓签收凭证」

const JD_MEETING = "260916 医米京东科研仓系统对接";
const HOP_MEETING = "医米赠药横跳拦截规则";
const EDC_MEETING = "EDC 系统选型与产研对接决策";

function task(id: string, title: string, overrides: Partial<Task> = {}): Task {
  return {
    id,
    title,
    detail: "",
    status: "confirmed",
    origin: "ai",
    assignee: "me",
    meeting_id: "m-hop",
    meeting_title: HOP_MEETING,
    meeting_recording_date: "2026-09-10T18:22:00-07:00",
    anchor_ms: 88_000,
    project_id: YIMI,
    project_name: "医米科研用药",
    requirement_id: null,
    status_changed_at: "2026-09-10T00:00:00Z",
    created_at: "2026-09-10T00:00:00Z",
    updated_at: "2026-09-10T00:00:00Z",
    stall_days: 0,
    stalled: false,
    due_date: null,
    ...overrides,
  };
}

const JD_TASKS = [
  task("t-jd-1", "梳理京东开放平台需对接的接口清单", { status: "done", requirement_id: "requirement-jd", meeting_title: JD_MEETING, anchor_ms: null }),
  task("t-jd-2", "与合作方协商落实采购单号告知物流司机", { status: "done", requirement_id: "requirement-jd", meeting_title: JD_MEETING, anchor_ms: null }),
  task("t-jd-3", "确认入库单从医米系统推送京东科研仓", { status: "done", requirement_id: "requirement-jd", meeting_title: JD_MEETING, anchor_ms: null }),
];
const HOP_TASKS = [
  task("t-hop-1", "把跨政策横跳卡控作为需求立项并修复", { requirement_id: "requirement-hop" }),
  task("t-hop-2", "排查现网是否已有同一药品不能重复申领能力，若无则9月完成", { requirement_id: "requirement-hop", status: "in_progress" }),
  task("t-hop-3", "就艾瑞康、艾瑞妮项目是否与济佰世同步给崔成回复", { requirement_id: "requirement-hop" }),
];
const UNLINKED = [
  task("t-u-1", "安排与华谊的会", { meeting_title: EDC_MEETING }),
  task("t-u-2", "确认产研能否派一人对接 EDC", { meeting_title: EDC_MEETING }),
  task("t-u-3", "与萌总同步 EDC 选型议题", { meeting_title: EDC_MEETING }),
];

function requirement(
  id: string,
  title: string,
  tasks: Task[],
  priority: ProjectWorkRequirement["priority"] = "P0",
): ProjectWorkPayload["requirements"][number] {
  return {
    id,
    title,
    status: "active",
    priority,
    task_count: tasks.length,
    open_task_count: tasks.filter((item) => item.status !== "done").length,
    tasks,
  };
}

function makeWork(overrides: Partial<ProjectWorkPayload> = {}): ProjectWorkPayload {
  return {
    project_id: YIMI,
    requirements: [
      requirement("requirement-jd", "京东科研仓对接", JD_TASKS),
      requirement("requirement-hop", "赠药横跳拦截", HOP_TASKS),
    ],
    closed_requirements: [
      { id: "requirement-reg", title: "新患者注册：五个问题前置", status: "done", priority: "P2", task_count: 2, open_task_count: 0, all_done: true },
      { id: "requirement-edc", title: "EDC 系统选型", status: "shelved", priority: "P1", task_count: 0, open_task_count: 0, all_done: false },
    ],
    unlinked_tasks: UNLINKED,
    pending_candidates: [{ id: "candidate-receipt", title: "京东仓签收凭证" }],
    ...overrides,
  };
}

const board = {
  id: YIMI,
  name: "医米科研用药",
  color: "#2c8d83",
  meeting_count: 36,
  requirement_counts: { active: 2, done: 1, shelved: 1, all: 4 },
  open_task_count: 6,
  material_roots: [{ id: 1, project_id: YIMI, path: "/Volumes/外置中枢/医朵云/医米科研用药", exists: true, created_at: "2026-09-01T00:00:00Z" }],
  meetings: [],
} as unknown as ProjectBoard;

const OPTION_JD = {
  kind: "requirement" as const,
  id: "requirement-jd",
  title: "京东科研仓对接",
  priority: "P0" as const,
  project_id: YIMI,
  project_name: "医米科研用药",
  meeting_id: null,
};

function recording(id: string, title: string, at: string, overrides: Partial<ProjectRecordingRow> = {}): ProjectRecordingRow {
  return { id, title, recording_date: at, duration_ms: 60_000, canonical_dir: `/归档/${title}`, requirements: [], task_count: 0, ...overrides };
}

function setup(options: { work?: ProjectWorkPayload; api?: Partial<ApiClient>; props?: Partial<Parameters<typeof ProjectDetailPage>[0]>; tab?: string | null } = {}) {
  // work 是「服务器上的状态」：写接口改它，重取就拿到新的
  const state = { work: options.work ?? makeWork() };
  const api = {
    projectBoard: vi.fn().mockResolvedValue(board),
    projectMaterialSubfolders: vi.fn().mockResolvedValue({ roots: [] }),
    projectWork: vi.fn(async () => structuredClone(state.work)),
    requirementPool: vi.fn().mockResolvedValue({
      items: [
        requirementItem({ id: "requirement-jd", title: "京东科研仓对接", open_task_count: 0 }),
        requirementItem({ id: "requirement-hop", title: "赠药横跳拦截", open_task_count: 3, source: null, follow_up_count: 0 }),
      ],
      total: 2,
    }),
    projectRecordings: vi.fn().mockResolvedValue([]),
    setTaskStatus: vi.fn().mockResolvedValue({}),
    undoTaskComplete: vi.fn().mockResolvedValue({ reverted: [], failed: [] }),
    updateTask: vi.fn().mockResolvedValue({}),
    createTask: vi.fn().mockResolvedValue({}),
    taskRequirementOptions: vi.fn().mockResolvedValue({
      task_id: "x",
      can_link_candidates: false,
      recommended: [],
      default: null,
      options: [OPTION_JD, { ...OPTION_JD, id: "requirement-hop", title: "赠药横跳拦截" }],
      current: null,
    }),
    ...options.api,
  } as unknown as ApiClient;
  const props = {
    onBack: vi.fn(),
    onOpenGlossary: vi.fn(),
    onOpenMeeting: vi.fn(),
    onOpenRequirement: vi.fn(),
    onOpenTask: vi.fn(),
    ...options.props,
  };
  render(
    <ProjectDetailPage
      apiClient={api}
      canPickFolders
      canWrite
      projectId={YIMI}
      projects={[]}
      {...props}
    />,
  );
  return { api, props, state };
}

const pair = (id: string) => document.querySelector<HTMLElement>(`[data-requirement-id="${id}"]`)!;

describe("项目详情的四个标签页", () => {
  it("默认停在「需求与任务」，切到录音、材料，没传关系图内容时没有关系图标签", async () => {
    const { api } = setup({ api: { projectRecordings: vi.fn().mockResolvedValue([recording("m1", EDC_MEETING, "2026-09-28T18:37:26-07:00")]) } });
    expect(await screen.findByRole("tab", { name: "需求与任务" })).toHaveAttribute("aria-selected", "true");
    expect(screen.queryByRole("tab", { name: "关系图" })).toBeNull();
    await screen.findByRole("region", { name: "进行中的需求" });
    expect(api.projectRecordings).not.toHaveBeenCalled();

    await userEvent.click(screen.getByRole("tab", { name: /录音/ }));
    expect(await screen.findByText(EDC_MEETING)).toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "进行中的需求" })).toBeNull();

    await userEvent.click(screen.getByRole("tab", { name: "材料" }));
    expect(await screen.findByRole("heading", { name: "材料根目录" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "材料" })).toHaveAttribute("aria-selected", "true");
  });

  it("关系图标签渲染外面传进来的原有画布，切进、切出都告诉外面", async () => {
    const onViewModeChange = vi.fn();
    setup({ props: { graphTab: <div>项目关系图画布</div>, viewMode: "list", onViewModeChange } });
    await userEvent.click(await screen.findByRole("tab", { name: "关系图" }));
    expect(screen.getByText("项目关系图画布")).toBeInTheDocument();
    expect(onViewModeChange).toHaveBeenLastCalledWith("graph");

    await userEvent.click(screen.getByRole("tab", { name: "材料" }));
    expect(screen.queryByText("项目关系图画布")).toBeNull();
    expect(onViewModeChange).toHaveBeenLastCalledWith("list");
    // 材料 → 录音这种不碰关系图的切换不打扰外面
    onViewModeChange.mockClear();
    await userEvent.click(screen.getByRole("tab", { name: /录音/ }));
    expect(onViewModeChange).not.toHaveBeenCalled();
  });

  it("地址栏记着的是关系图时直接停在关系图标签", async () => {
    setup({ props: { graphTab: <div>项目关系图画布</div>, viewMode: "graph" } });
    expect(await screen.findByText("项目关系图画布")).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "关系图" })).toHaveAttribute("aria-selected", "true");
  });

  it("接口读取失败（旧后端 404）：显示读取失败和重试，不白屏", async () => {
    const projectWork = vi.fn().mockRejectedValue(new Error("404"));
    const { api } = setup({ api: { projectWork } });
    expect(await screen.findByText("需求与任务读取失败")).toBeInTheDocument();
    projectWork.mockImplementation(async () => makeWork());
    await userEvent.click(screen.getByRole("button", { name: "重试" }));
    expect(await screen.findByRole("region", { name: "进行中的需求" })).toBeInTheDocument();
    expect(api.projectWork).toHaveBeenCalledTimes(2);
  });
});

describe("需求与任务：待认领横幅", () => {
  it("提示候选条数和名称，「去认领」带着项目 id 回调", async () => {
    const onClaimCandidates = vi.fn();
    setup({ props: { onClaimCandidates } });
    const banner = await screen.findByRole("note");
    expect(banner).toHaveTextContent("这个项目有 1 条待认领的需求候选：京东仓签收凭证");
    await userEvent.click(within(banner).getByRole("button", { name: /去认领/ }));
    expect(onClaimCandidates).toHaveBeenCalledWith(YIMI);
  });

  it("没有候选就没有横幅", async () => {
    setup({ work: makeWork({ pending_candidates: [] }) });
    await screen.findByRole("region", { name: "进行中的需求" });
    expect(screen.queryByRole("note")).toBeNull();
  });
});

describe("需求与任务：海报和任务面板", () => {
  it("每条进行中需求一张海报配一块面板，顺序照接口（P0→P3），海报取自需求池", async () => {
    const { api } = setup({
      work: makeWork({
        requirements: [
          requirement("requirement-jd", "京东科研仓对接", JD_TASKS),
          requirement("requirement-hop", "赠药横跳拦截", HOP_TASKS),
          requirement("requirement-p3", "一条 P3 的需求", [], "P3"),
        ],
      }),
      api: {
        requirementPool: vi.fn().mockResolvedValue({
          items: [
            requirementItem({ id: "requirement-p3", title: "一条 P3 的需求", priority: "P3" }),
            requirementItem({ id: "requirement-hop", title: "赠药横跳拦截" }),
            requirementItem({ id: "requirement-jd", title: "京东科研仓对接" }),
          ],
          total: 3,
        }),
      },
    });
    await screen.findByRole("region", { name: "进行中的需求" });
    expect(api.requirementPool).toHaveBeenCalledWith({ status: "active", project_id: YIMI });
    const ids = [...document.querySelectorAll("[data-requirement-id]")].map((el) => el.getAttribute("data-requirement-id"));
    expect(ids).toEqual(["requirement-jd", "requirement-hop", "requirement-p3"]);
    const first = pair("requirement-jd");
    expect(within(first).getByRole("article", { name: "需求：京东科研仓对接" })).toBeInTheDocument();
    expect(within(first).getByRole("region", { name: "「京东科研仓对接」的任务" })).toBeInTheDocument();
    expect(within(pair("requirement-p3")).getByText("这条需求下还没有任务")).toBeInTheDocument();
  });

  it("面板标题：有未完成写「任务 N · 未完成 M」，全完成写「已完成 N」", async () => {
    setup();
    const jd = await screen.findByRole("region", { name: "「京东科研仓对接」的任务" });
    expect(within(jd).getByRole("heading")).toHaveTextContent("任务 3· 已完成 3");
    const hop = screen.getByRole("region", { name: "「赠药横跳拦截」的任务" });
    expect(within(hop).getByRole("heading")).toHaveTextContent("任务 3· 未完成 3");
  });

  it("任务超过 6 行折成「还有 N 项」，点开就地展开，能再收起", async () => {
    const many = Array.from({ length: 8 }, (_, index) => task(`t-m-${index}`, `第 ${index + 1} 项任务`, { requirement_id: "requirement-hop" }));
    setup({ work: makeWork({ requirements: [requirement("requirement-hop", "赠药横跳拦截", many)] }) });
    const panel = await screen.findByRole("region", { name: "「赠药横跳拦截」的任务" });
    expect(within(panel).getAllByRole("listitem")).toHaveLength(6);
    expect(within(panel).queryByText("第 7 项任务")).toBeNull();
    await userEvent.click(within(panel).getByRole("button", { name: "还有 2 项" }));
    expect(within(panel).getAllByRole("listitem")).toHaveLength(8);
    await userEvent.click(within(panel).getByRole("button", { name: "收起" }));
    expect(within(panel).getAllByRole("listitem")).toHaveLength(6);
  });

  it("勾选完成：提示带撤销，面板里该任务变成已完成、移到末尾，计数同步", async () => {
    const { api, state } = setup();
    const panel = await screen.findByRole("region", { name: "「赠药横跳拦截」的任务" });
    api.setTaskStatus = vi.fn(async (id: string) => {
      // 服务端把它标成完成、排到面板末尾
      const target = state.work.requirements[1];
      const done = { ...target.tasks.find((item) => item.id === id)!, status: "done" as const };
      target.tasks = [...target.tasks.filter((item) => item.id !== id), done];
      target.open_task_count -= 1;
      return {} as never;
    });
    await userEvent.click(within(panel).getByRole("checkbox", { name: "完成「把跨政策横跳卡控作为需求立项并修复」" }));
    expect(api.setTaskStatus).toHaveBeenCalledWith("t-hop-1", "done");
    expect(await screen.findByText("已完成「把跨政策横跳卡控作为需求立项并修复」")).toBeInTheDocument();

    await waitFor(() => expect(within(panel).getByRole("heading")).toHaveTextContent("任务 3· 未完成 2"));
    const rows = within(panel).getAllByRole("listitem");
    expect(rows[rows.length - 1]).toHaveTextContent("把跨政策横跳卡控作为需求立项并修复");
    expect(within(rows[rows.length - 1]).getByText("已完成")).toBeInTheDocument();
    expect(api.projectWork).toHaveBeenCalledTimes(2);
    expect(api.requirementPool).toHaveBeenCalledTimes(2);
  });

  it("撤销完成：调 undoTaskComplete，任务回到未完成；超过 10 分钟时给出原因", async () => {
    const { api } = setup();
    const panel = await screen.findByRole("region", { name: "「赠药横跳拦截」的任务" });
    await userEvent.click(within(panel).getAllByRole("button", { name: /^完成「/ })[0]);
    await userEvent.click(await screen.findByRole("button", { name: "撤销" }));
    expect(api.undoTaskComplete).toHaveBeenCalledWith(["t-hop-1"]);
    expect(await screen.findByText("已撤销，「把跨政策横跳卡控作为需求立项并修复」回到未完成")).toBeInTheDocument();

    api.undoTaskComplete = vi.fn().mockResolvedValue({ reverted: [], failed: [{ task_id: "t-hop-1", error: "已经过了 10 分钟，没法撤销了" }] });
    await userEvent.click(within(panel).getAllByRole("button", { name: /^完成「/ })[0]);
    await userEvent.click(await screen.findByRole("button", { name: "撤销" }));
    expect(await screen.findByText("已经过了 10 分钟，没法撤销了")).toBeInTheDocument();
  });

  it("写操作失败：红色提示，面板不变", async () => {
    const { api } = setup({ api: { setTaskStatus: vi.fn().mockRejectedValue(new Error("任务状态已变化")) } });
    const panel = await screen.findByRole("region", { name: "「赠药横跳拦截」的任务" });
    await userEvent.click(within(panel).getAllByRole("button", { name: /^完成「/ })[0]);
    expect(await screen.findByRole("alert")).toHaveTextContent("任务状态已变化");
    expect(api.projectWork).toHaveBeenCalledTimes(1);
  });

  it("更多菜单按状态给项：已确认有「开始处理」，进行中有「回退」，都有修改、改挂需求、取消任务", async () => {
    const { api } = setup();
    const panel = await screen.findByRole("region", { name: "「赠药横跳拦截」的任务" });
    const labels = async (name: string) => {
      await userEvent.click(within(panel).getByRole("button", { name }));
      const items = within(screen.getByRole("menu")).getAllByRole("menuitem").map((item) => item.textContent);
      return items;
    };
    expect(await labels("更多操作：把跨政策横跳卡控作为需求立项并修复")).toEqual(["开始处理", "修改", "改挂需求", "取消任务"]);
    await userEvent.keyboard("{Escape}");
    expect(await labels("更多操作：排查现网是否已有同一药品不能重复申领能力，若无则9月完成")).toEqual(["回退", "修改", "改挂需求", "取消任务"]);
    await userEvent.click(screen.getByRole("menuitem", { name: "回退" }));
    expect(api.setTaskStatus).toHaveBeenCalledWith("t-hop-2", "confirmed");

    await userEvent.click(within(panel).getByRole("button", { name: "更多操作：把跨政策横跳卡控作为需求立项并修复" }));
    await userEvent.click(screen.getByRole("menuitem", { name: "开始处理" }));
    expect(api.setTaskStatus).toHaveBeenCalledWith("t-hop-1", "in_progress");

    await userEvent.click(within(panel).getByRole("button", { name: "更多操作：就艾瑞康、艾瑞妮项目是否与济佰世同步给崔成回复" }));
    await userEvent.click(screen.getByRole("menuitem", { name: "取消任务" }));
    expect(api.setTaskStatus).toHaveBeenCalledWith("t-hop-3", "cancelled");

    // 已完成的任务不能再取消
    const jd = screen.getByRole("region", { name: "「京东科研仓对接」的任务" });
    await userEvent.click(within(jd).getByRole("button", { name: "更多操作：梳理京东开放平台需对接的接口清单" }));
    expect(within(screen.getByRole("menu")).getAllByRole("menuitem").map((item) => item.textContent)).toEqual(["修改", "改挂需求"]);
  });

  it("菜单里的「修改」打开修改任务弹窗", async () => {
    setup();
    const panel = await screen.findByRole("region", { name: "「赠药横跳拦截」的任务" });
    await userEvent.click(within(panel).getByRole("button", { name: "更多操作：把跨政策横跳卡控作为需求立项并修复" }));
    await userEvent.click(screen.getByRole("menuitem", { name: "修改" }));
    expect(await screen.findByRole("dialog", { name: "修改任务" })).toBeInTheDocument();
  });

  it("菜单里的「改挂需求」弹出选择器，选了另一条需求就改挂，提示带撤销", async () => {
    const { api } = setup();
    const panel = await screen.findByRole("region", { name: "「赠药横跳拦截」的任务" });
    await userEvent.click(within(panel).getByRole("button", { name: "更多操作：把跨政策横跳卡控作为需求立项并修复" }));
    await userEvent.click(screen.getByRole("menuitem", { name: "改挂需求" }));
    const picker = await screen.findByRole("dialog", { name: "挂到需求" });
    await userEvent.click(await within(picker).findByRole("option", { name: /京东科研仓对接/ }));
    expect(api.updateTask).toHaveBeenCalledWith("t-hop-1", { requirement_id: "requirement-jd" });
    expect(await screen.findByText("已挂到「京东科研仓对接」")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "撤销" }));
    expect(api.updateTask).toHaveBeenLastCalledWith("t-hop-1", { requirement_id: "requirement-hop" });
  });

  it("面板里新建任务：弹窗固定挂在这条需求下、归本项目", async () => {
    const { api } = setup();
    const panel = await screen.findByRole("region", { name: "「赠药横跳拦截」的任务" });
    await userEvent.click(within(panel).getByRole("button", { name: "＋ 新建任务" }));
    const dialog = await screen.findByRole("dialog", { name: "新建任务" });
    expect(within(dialog).getByText("挂在「赠药横跳拦截」下")).toBeInTheDocument();
    await userEvent.type(within(dialog).getByPlaceholderText("要做的一件事"), "同步上线时间");
    await userEvent.click(within(dialog).getByRole("button", { name: "创建" }));
    await waitFor(() => expect(api.createTask).toHaveBeenCalled());
    expect(vi.mocked(api.createTask).mock.calls[0][0]).toMatchObject({
      title: "同步上线时间",
      requirement_id: "requirement-hop",
      project_id: YIMI,
    });
    expect(await screen.findByText("已新建任务")).toBeInTheDocument();
    expect(api.projectWork).toHaveBeenCalledTimes(2);
  });

  it("点海报进需求详情", async () => {
    const onOpenRequirement = vi.fn();
    setup({ props: { onOpenRequirement } });
    await userEvent.click(await screen.findByRole("article", { name: "需求：赠药横跳拦截" }));
    expect(onOpenRequirement).toHaveBeenCalledWith("requirement-hop");
  });
});

describe("需求与任务：已完成、已搁置折叠区", () => {
  it("每条显示需求名、状态、等级、任务数和是否全部完成，点进需求详情，能收起", async () => {
    const onOpenRequirement = vi.fn();
    setup({ props: { onOpenRequirement } });
    const bar = await screen.findByRole("button", { name: /已完成 1 · 已搁置 1/ });
    const done = screen.getByRole("article", { name: "已完成需求：新患者注册：五个问题前置" });
    expect(done).toHaveTextContent("P2");
    expect(done).toHaveTextContent("任务 2 · 全部完成");
    const shelved = screen.getByRole("article", { name: "已搁置需求：EDC 系统选型" });
    expect(shelved).toHaveTextContent("P1");
    expect(shelved).toHaveTextContent("任务 0");
    expect(shelved).not.toHaveTextContent("全部完成");

    await userEvent.click(within(done).getByRole("button", { name: "查看" }));
    expect(onOpenRequirement).toHaveBeenCalledWith("requirement-reg");
    await userEvent.click(shelved);
    expect(onOpenRequirement).toHaveBeenCalledWith("requirement-edc");

    await userEvent.click(bar);
    expect(screen.queryByRole("article", { name: /已完成需求/ })).toBeNull();
  });
});

describe("需求与任务：没挂需求的任务", () => {
  it("列出本项目没挂需求的任务，带数量", async () => {
    setup();
    const section = await screen.findByRole("region", { name: "没挂需求的任务" });
    expect(within(section).getByRole("heading")).toHaveTextContent("没挂需求的任务 3");
    expect(within(section).getAllByRole("listitem")).toHaveLength(3);
  });

  it("行内「挂到需求」从本项目进行中需求里选，挂上后两边计数同步，提示带撤销", async () => {
    const { api, state } = setup();
    const section = await screen.findByRole("region", { name: "没挂需求的任务" });
    api.updateTask = vi.fn(async (id: string) => {
      const moved = { ...state.work.unlinked_tasks.find((item) => item.id === id)!, requirement_id: "requirement-hop" };
      state.work.unlinked_tasks = state.work.unlinked_tasks.filter((item) => item.id !== id);
      state.work.requirements[1].tasks = [...state.work.requirements[1].tasks, moved];
      return {} as never;
    });
    await userEvent.click(within(section).getByRole("button", { name: "挂到需求：安排与华谊的会" }));
    const picker = await screen.findByRole("dialog", { name: "挂到需求" });
    await userEvent.click(await within(picker).findByRole("option", { name: /赠药横跳拦截/ }));

    expect(api.updateTask).toHaveBeenCalledWith("t-u-1", { requirement_id: "requirement-hop" });
    expect(await screen.findByText("已挂到「赠药横跳拦截」")).toBeInTheDocument();
    await waitFor(() => expect(within(section).getByRole("heading")).toHaveTextContent("没挂需求的任务 2"));
    const hop = screen.getByRole("region", { name: "「赠药横跳拦截」的任务" });
    expect(within(hop).getByRole("heading")).toHaveTextContent("任务 4· 未完成 4");
    expect(within(hop).getByText("安排与华谊的会")).toBeInTheDocument();
  });

  it("选「不挂需求」：什么都不改的话仍可选，撤销写回原样", async () => {
    const { api } = setup();
    const section = await screen.findByRole("region", { name: "没挂需求的任务" });
    await userEvent.click(within(section).getByRole("button", { name: "挂到需求：安排与华谊的会" }));
    const picker = await screen.findByRole("dialog", { name: "挂到需求" });
    await userEvent.click(within(picker).getByRole("button", { name: "不挂需求" }));
    expect(api.updateTask).toHaveBeenCalledWith("t-u-1", { requirement_id: null, candidate_id: null });
    expect(await screen.findByText("已设为不挂需求")).toBeInTheDocument();
  });

  it("拖到某条进行中需求上挂上：目标带「放到这里挂上」、其他需求变淡", async () => {
    const { api } = setup();
    const section = await screen.findByRole("region", { name: "没挂需求的任务" });
    const row = within(section).getByText("确认产研能否派一人对接 EDC").closest("li")!;
    expect(row).toHaveAttribute("draggable", "true");
    const dataTransfer = { setData: vi.fn(), effectAllowed: "", dropEffect: "" };
    fireEvent.dragStart(row, { dataTransfer });

    const hop = pair("requirement-hop");
    const over = createEvent.dragOver(hop, { dataTransfer });
    fireEvent(hop, over);
    expect(over.defaultPrevented).toBe(true);
    expect(within(hop).getByText("放到这里挂上")).toBeInTheDocument();
    expect(hop).toHaveClass("is-target");
    expect(pair("requirement-jd")).toHaveClass("is-dim");
    expect(within(pair("requirement-jd")).queryByText("放到这里挂上")).toBeNull();

    fireEvent.drop(hop, { dataTransfer });
    await waitFor(() => expect(api.updateTask).toHaveBeenCalledWith("t-u-2", { requirement_id: "requirement-hop" }));
    expect(await screen.findByText("已挂到「赠药横跳拦截」")).toBeInTheDocument();
    expect(screen.queryByText("放到这里挂上")).toBeNull();
  });

  it("拖动中途放弃（拖出去松手）：提示和变淡都撤掉，不写任何东西", async () => {
    const { api } = setup();
    const section = await screen.findByRole("region", { name: "没挂需求的任务" });
    const row = within(section).getByText("安排与华谊的会").closest("li")!;
    const dataTransfer = { setData: vi.fn(), effectAllowed: "", dropEffect: "" };
    fireEvent.dragStart(row, { dataTransfer });
    fireEvent.dragOver(pair("requirement-hop"), { dataTransfer });
    fireEvent.dragEnd(row, { dataTransfer });
    expect(screen.queryByText("放到这里挂上")).toBeNull();
    expect(pair("requirement-jd")).not.toHaveClass("is-dim");
    expect(api.updateTask).not.toHaveBeenCalled();
  });

  it("本项目没有进行中需求：不显示「挂到需求」，行也不能拖", async () => {
    setup({ work: makeWork({ requirements: [] }) });
    const section = await screen.findByRole("region", { name: "没挂需求的任务" });
    expect(within(section).queryByRole("button", { name: /^挂到需求/ })).toBeNull();
    expect(within(section).getByText("安排与华谊的会").closest("li")).toHaveAttribute("draggable", "false");
    expect(screen.getByText("本项目没有进行中的需求")).toBeInTheDocument();
    expect(within(section).queryByText(/可以拖到上面某条需求/)).toBeNull();
  });

  it("挂在待认领候选上的任务仍在这里，标出候选名；候选不在本项目的待认领名单里就不标", async () => {
    const onCandidate = task("t-c-1", "确认签收凭证从哪里拿", { candidate_id: "candidate-receipt", candidate_title: "京东仓签收凭证" });
    const moved = task("t-c-2", "补一份签收样例", { candidate_id: "candidate-elsewhere", candidate_title: "已搬去别的项目的候选" });
    setup({ work: makeWork({ unlinked_tasks: [onCandidate, moved] }) });
    const section = await screen.findByRole("region", { name: "没挂需求的任务" });
    expect(within(section).getByText("候选：京东仓签收凭证")).toBeInTheDocument();
    expect(within(section).getByText("补一份签收样例")).toBeInTheDocument();
    expect(within(section).queryByText(/已搬去别的项目的候选/)).toBeNull();
  });

  it("只读（canWrite 为 false）：没有勾选、完成、挂到需求和新建任务，也不能拖", async () => {
    setup({ props: { canWrite: false } });
    const section = await screen.findByRole("region", { name: "没挂需求的任务" });
    expect(within(section).queryByRole("button", { name: /^挂到需求/ })).toBeNull();
    expect(within(section).getByText("安排与华谊的会").closest("li")).toHaveAttribute("draggable", "false");
    expect(screen.queryByRole("button", { name: "＋ 新建任务" })).toBeNull();
  });
});

describe("录音标签页", () => {
  const rows = [
    recording("m-edc", EDC_MEETING, "2026-09-28T18:37:26-07:00", { duration_ms: 113_475, task_count: 3 }),
    recording("m-jd", JD_MEETING, "2026-09-16T19:01:50-07:00", {
      duration_ms: 3_033_387,
      task_count: 3,
      requirements: [{ id: "requirement-jd", title: "京东科研仓对接", priority: "P0", status: "active", project_id: YIMI }],
    }),
    recording("m-free", "医米免费结束时间字典规范", "2026-09-10T20:00:00-07:00", { duration_ms: 13 * 60_000, task_count: 11 }),
    recording("m-hop", HOP_MEETING, "2026-09-10T18:22:00-07:00", { duration_ms: 3 * 60_000, task_count: 1 }),
  ];

  async function openRecordings(props: Partial<Parameters<typeof ProjectDetailPage>[0]> = {}) {
    const view = setup({ api: { projectRecordings: vi.fn().mockResolvedValue(rows) }, props });
    await userEvent.click(await screen.findByRole("tab", { name: /录音/ }));
    await screen.findByText(EDC_MEETING);
    return view;
  }

  it("按录音日期倒序、按日分组，每组写场数和总时长；每行有时间、时长、任务数", async () => {
    await openRecordings();
    const days = screen.getAllByRole("region").filter((el) => el.classList.contains("archive-day"));
    expect(days.map((el) => el.getAttribute("aria-label"))).toEqual(["9月29日", "9月17日", "9月11日"]);
    // 同一天两场会归一组
    const last = days[2];
    expect(within(last).getAllByRole("listitem")).toHaveLength(2);
    expect(last).toHaveTextContent("2场 · 16 分钟");
    expect(within(days[0]).getByText("任务 3")).toBeInTheDocument();
    expect(within(days[0]).getByText("2 分钟")).toBeInTheDocument();
    expect(within(last).getByText("任务 11")).toBeInTheDocument();
    expect(within(last).getByText("13 分钟")).toBeInTheDocument();
  });

  it("点行进入会议详情；点需求名进入需求详情，不连带打开会议", async () => {
    const onOpenMeeting = vi.fn();
    const onOpenRequirement = vi.fn();
    await openRecordings({ onOpenMeeting, onOpenRequirement });
    await userEvent.click(screen.getByText(HOP_MEETING));
    expect(onOpenMeeting).toHaveBeenCalledWith("m-hop");
    onOpenMeeting.mockClear();
    await userEvent.click(screen.getByRole("button", { name: "京东科研仓对接" }));
    expect(onOpenRequirement).toHaveBeenCalledWith("requirement-jd");
    expect(onOpenMeeting).not.toHaveBeenCalled();
  });

  it("没有录音时给空态；接口失败给读取失败", async () => {
    setup({ api: { projectRecordings: vi.fn().mockResolvedValue([]) } });
    await userEvent.click(await screen.findByRole("tab", { name: /录音/ }));
    expect(await screen.findByText("这个项目还没有录音")).toBeInTheDocument();
  });

  it("旧后端没有这个接口：读取失败而不是白屏", async () => {
    setup({ api: { projectRecordings: vi.fn().mockRejectedValue(new Error("404")) } });
    await userEvent.click(await screen.findByRole("tab", { name: /录音/ }));
    expect(await screen.findByText("录音读取失败")).toBeInTheDocument();
  });

  it("超过 8 天的更早的收在「更早 N 场」里，点开才全列", async () => {
    const many = Array.from({ length: 10 }, (_, index) =>
      recording(`m-${index}`, `第 ${index + 1} 场会`, `2026-09-${String(28 - index).padStart(2, "0")}T12:00:00-07:00`),
    );
    setup({ api: { projectRecordings: vi.fn().mockResolvedValue(many) } });
    await userEvent.click(await screen.findByRole("tab", { name: /录音/ }));
    await screen.findByText("第 1 场会");
    expect(screen.queryByText("第 10 场会")).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: /更早 2 场/ }));
    expect(screen.getByText("第 10 场会")).toBeInTheDocument();
  });
});

describe("项目详情的收尾细节", () => {
  it("待认领候选多时横幅只写前两个名字加「等 N 条」", async () => {
    setup({
      work: makeWork({
        pending_candidates: [
          { id: "c1", title: "京东仓签收凭证" },
          { id: "c2", title: "物流轨迹对账" },
          { id: "c3", title: "月结单导出" },
        ],
      }),
    });
    const banner = await screen.findByRole("note");
    expect(banner).toHaveTextContent("这个项目有 3 条待认领的需求候选：京东仓签收凭证、物流轨迹对账 等");
    expect(banner).not.toHaveTextContent("月结单导出");
    // 被省略的名字放在 title 里
    expect(within(banner).getByTitle("京东仓签收凭证、物流轨迹对账、月结单导出")).toBeInTheDocument();
  });

  it("完成、挂需求、新建任务后都通知外层刷新项目列表", async () => {
    const onProjectsChanged = vi.fn();
    setup({ props: { onProjectsChanged } });
    const panel = await screen.findByRole("region", { name: "「赠药横跳拦截」的任务" });
    await userEvent.click(within(panel).getAllByRole("button", { name: /^完成「/ })[0]);
    await waitFor(() => expect(onProjectsChanged).toHaveBeenCalledTimes(1));

    const section = screen.getByRole("region", { name: "没挂需求的任务" });
    await userEvent.click(within(section).getByRole("button", { name: "挂到需求：安排与华谊的会" }));
    const picker = await screen.findByRole("dialog", { name: "挂到需求" });
    await userEvent.click(await within(picker).findByRole("option", { name: /京东科研仓对接/ }));
    await waitFor(() => expect(onProjectsChanged).toHaveBeenCalledTimes(2));

    await userEvent.click(within(panel).getByRole("button", { name: "＋ 新建任务" }));
    const dialog = await screen.findByRole("dialog", { name: "新建任务" });
    await userEvent.type(within(dialog).getByPlaceholderText("要做的一件事"), "同步上线时间");
    await userEvent.click(within(dialog).getByRole("button", { name: "创建" }));
    await waitFor(() => expect(onProjectsChanged).toHaveBeenCalledTimes(3));
  });

  it("面板里新建任务：说明不提需求详情；折着的面板自动展开让新任务看得见", async () => {
    const many = Array.from({ length: 8 }, (_, index) => task(`t-m-${index}`, `第 ${index + 1} 项任务`, { requirement_id: "requirement-hop" }));
    const { state } = setup({ work: makeWork({ requirements: [requirement("requirement-hop", "赠药横跳拦截", many)] }) });
    const panel = await screen.findByRole("region", { name: "「赠药横跳拦截」的任务" });
    expect(within(panel).getAllByRole("listitem")).toHaveLength(6);
    await userEvent.click(within(panel).getByRole("button", { name: "＋ 新建任务" }));
    const dialog = await screen.findByRole("dialog", { name: "新建任务" });
    expect(dialog).toHaveTextContent("固定挂在这条需求上");
    expect(dialog).not.toHaveTextContent("从需求详情");
    // 服务端收下新任务（排在未完成的最后）
    state.work.requirements[0].tasks = [...many, task("t-new", "同步上线时间", { requirement_id: "requirement-hop" })];
    await userEvent.type(within(dialog).getByPlaceholderText("要做的一件事"), "同步上线时间");
    await userEvent.click(within(dialog).getByRole("button", { name: "创建" }));
    expect(await within(panel).findByText("同步上线时间")).toBeInTheDocument();
    expect(within(panel).getByRole("button", { name: "收起" })).toBeInTheDocument();
  });

  it("写成功但重新读取失败：撤销入口还在，另用一条横幅说没能刷新，不拿提示顶替", async () => {
    const { api } = setup();
    const panel = await screen.findByRole("region", { name: "「赠药横跳拦截」的任务" });
    vi.mocked(api.projectWork).mockRejectedValue(new Error("boom"));
    await userEvent.click(within(panel).getAllByRole("button", { name: /^完成「/ })[0]);
    expect(await screen.findByText("已完成「把跨政策横跳卡控作为需求立项并修复」")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "撤销" })).toBeInTheDocument();
    expect(await screen.findByText(/没能重新读取/)).toBeInTheDocument();
    // 旧数据还在
    expect(screen.getByRole("region", { name: "「赠药横跳拦截」的任务" })).toBeInTheDocument();
  });

  it("项目不存在（404）：只出一个「项目不存在」，带返回项目列表，不再画关系图", async () => {
    const onBack = vi.fn();
    setup({
      api: { projectBoard: vi.fn().mockRejectedValue(new ApiError("项目不存在", 404)) },
      props: { onBack, graphTab: <div>项目关系图画布</div>, viewMode: "graph" },
    });
    expect(await screen.findByText(/项目不存在/)).toBeInTheDocument();
    expect(screen.queryByText("项目详情读取失败")).toBeNull();
    expect(screen.queryByText("项目关系图画布")).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "返回项目列表" }));
    expect(onBack).toHaveBeenCalled();
  });

  it("读取失败（非 404）仍是「读取失败＋重试」", async () => {
    setup({ api: { projectBoard: vi.fn().mockRejectedValue(new Error("boom")) } });
    expect(await screen.findByText("项目详情读取失败")).toBeInTheDocument();
  });

  it("进详情后焦点落到页面标题", async () => {
    setup();
    const title = await screen.findByRole("heading", { name: "医米科研用药", level: 1 });
    await waitFor(() => expect(title).toHaveFocus());
  });

  it("完成后重取还没回来就点［撤销］：撤销不被吞，等手头这次做完再撤，库状态恢复", async () => {
    const { api, state } = setup();
    const panel = await screen.findByRole("region", { name: "「赠药横跳拦截」的任务" });
    // 第二次重取（完成之后那次）卡住，模拟接口慢
    let release: () => void = () => undefined;
    const base = vi.mocked(api.projectWork).getMockImplementation()!;
    const gate = new Promise<void>((resolve) => (release = resolve));
    vi.mocked(api.projectWork).mockImplementationOnce(async () => {
      await gate;
      return base(YIMI);
    });
    let done = false;
    vi.mocked(api.setTaskStatus).mockImplementation(async () => {
      done = true;
      return {} as never;
    });
    vi.mocked(api.undoTaskComplete).mockImplementation(async () => {
      done = false;
      return { reverted: ["t-hop-1"], failed: [] };
    });

    await userEvent.click(within(panel).getAllByRole("button", { name: /^完成「/ })[0]);
    // 提示已弹出，重取还卡着
    await userEvent.click(await screen.findByRole("button", { name: "撤销" }));
    expect(api.undoTaskComplete).not.toHaveBeenCalled();
    release();
    await waitFor(() => expect(api.undoTaskComplete).toHaveBeenCalledWith(["t-hop-1"]));
    expect(done).toBe(false);
    expect(await screen.findByText("已撤销，「把跨政策横跳卡控作为需求立项并修复」回到未完成")).toBeInTheDocument();
    expect(state.work.requirements[1].tasks[0].status).toBe("confirmed");
  });
});
