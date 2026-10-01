import { createEvent, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ProjectsPage } from "./ProjectsPage";
import type { ApiClient } from "../api";
import type { Project } from "../types";

function makeClient(overrides: Partial<ApiClient> = {}) {
  return {
    createProjectWith: vi.fn().mockResolvedValue({ id: "new", name: "互联网医院", color: "#3f51b5" }),
    saveProjectSeats: vi.fn().mockResolvedValue({ seats: [] }),
    updateProject: vi.fn().mockResolvedValue({ id: "p1", name: "云图科研用药", color: "#2c8d83" }),
    browseMaterials: vi.fn().mockResolvedValue({
      base: "/Volumes/资料盘",
      path: "/Volumes/资料盘",
      parent: null,
      breadcrumbs: [{ name: "资料盘", path: "/Volumes/资料盘" }],
      dirs: [],
    }),
    ...overrides,
  } as unknown as ApiClient;
}

// 真实数据：医米座次 1、恒瑞座次 2，其余未排；节奏数组是后端算好的近 12 周场数，最后一格是本周
const WEEKS_YIMI = [0, 0, 1, 1, 1, 2, 1, 3, 4, 2, 3, 1];
const YIMI: Project = {
  id: "p-yimi",
  name: "医米科研用药",
  color: "#2c8d83",
  seat: 1,
  meeting_count: 36,
  requirement_counts: { active: 2, done: 0, shelved: 0, all: 2 },
  open_task_count: 6,
  latest_meeting_date: "2026-09-28T20:00:00+08:00",
  latest_meeting_title: "EDC 系统选型与产研对接决策",
  active_requirement_titles: ["京东科研仓对接", "赠药横跳拦截"],
  active_requirement_count: 2,
  pending_candidate_count: 1,
  weekly_meetings: WEEKS_YIMI,
  recording_ms: 47_760_000,
  weeks_since_last_meeting: 0,
  material_roots: [
    { id: 1, project_id: "p-yimi", path: "/Volumes/资料盘/医朵云/医米科研用药", exists: true, created_at: "2026-09-01T00:00:00Z" },
  ],
};
const HENGRUI: Project = {
  id: "p-hengrui",
  name: "恒瑞健康",
  color: "#549c8a",
  seat: 2,
  meeting_count: 10,
  open_task_count: 0,
  latest_meeting_date: "2026-09-21T10:00:00+08:00",
  latest_meeting_title: "黑卡分享注销与积分限制口径",
  active_requirement_titles: [],
  active_requirement_count: 0,
  pending_candidate_count: 0,
  weekly_meetings: [0, 0, 0, 0, 0, 0, 0, 2, 0, 3, 1, 0],
  recording_ms: 15_300_000,
  weeks_since_last_meeting: 1,
  material_roots: [],
};
const CVM: Project = {
  id: "p-cvm",
  name: "CVM 云讲堂",
  color: "#5090ff",
  seat: null,
  meeting_count: 6,
  open_task_count: 0,
  latest_meeting_date: "2026-09-29T19:26:37-07:00",
  latest_meeting_title: "260929 云课堂直播运营问题对齐",
  weekly_meetings: [0, 0, 0, 0, 0, 0, 4, 1, 0, 0, 0, 1],
  recording_ms: 5_873_365,
  weeks_since_last_meeting: 0,
  material_roots: [],
};
const BLACKCARD: Project = {
  id: "p-blackcard",
  name: "黑卡小程序",
  color: "#8c6bd9",
  seat: null,
  meeting_count: 4,
  open_task_count: 5,
  latest_meeting_date: "2026-09-22T12:00:00+08:00",
  latest_meeting_title: "发卡变更与一直拍流程改造沟通",
  weekly_meetings: [0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 3, 0],
  recording_ms: 6_600_000,
  weeks_since_last_meeting: 1,
  material_roots: [],
};
const CACA: Project = {
  id: "p-caca",
  name: "CACA",
  color: "#7a1fa2",
  seat: null,
  meeting_count: 16,
  open_task_count: 0,
  latest_meeting_date: "2026-09-03T23:58:37-07:00",
  latest_meeting_title: "AI 剪辑工具（一支拍）接入 CACA 与统一执行流程讨论（260904）",
  weekly_meetings: [2, 2, 3, 0, 1, 2, 0, 1, 0, 0, 0, 0],
  recording_ms: 35_093_349,
  weeks_since_last_meeting: 4,
  material_roots: [],
};
const MDT: Project = {
  id: "p-mdt",
  name: "MDT",
  color: "#a08a3c",
  seat: null,
  meeting_count: 19,
  open_task_count: 0,
  latest_meeting_date: "2026-08-13T23:23:00-07:00",
  latest_meeting_title: "MDT2.0病例汇报模板讨论",
  weekly_meetings: [7, 3, 2, 3, 2, 0, 0, 0, 0, 0, 0, 0],
  recording_ms: 55_909_305,
  weeks_since_last_meeting: 7,
  material_roots: [],
};

// 故意打乱：页面要自己按座次、按最近会议排
const PROJECTS: Project[] = [MDT, CACA, HENGRUI, BLACKCARD, CVM, YIMI];

function renderPage(overrides: Partial<Parameters<typeof ProjectsPage>[0]> = {}) {
  return render(
    <ProjectsPage
      apiClient={makeClient()}
      canEdit
      meetings={[]}
      onCreateTag={vi.fn().mockResolvedValue(undefined)}
      onOpenProject={vi.fn()}
      onProjectsChanged={vi.fn()}
      projects={PROJECTS}
      tags={[]}
      {...overrides}
    />,
  );
}

function panels() {
  return screen.queryAllByRole("article");
}

function rows() {
  const list = screen.queryByRole("list", { name: "未排座次的项目列表" });
  return list ? (Array.from(list.children) as HTMLElement[]) : [];
}

function rowNames() {
  return rows().map((row) => within(row).getAllByRole("button")[0].textContent);
}

function dataTransfer() {
  const store: Record<string, string> = {};
  return {
    setData: (key: string, value: string) => {
      store[key] = value;
    },
    getData: (key: string) => store[key],
    effectAllowed: "",
    dropEffect: "",
  };
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("ProjectsPage 两段列表", () => {
  it("页头：标题、副标题、项目数", () => {
    renderPage();
    expect(screen.getByRole("heading", { name: "项目" })).toBeInTheDocument();
    expect(screen.getByText("按「我的方向」排座次，每个项目里的需求、任务和录音都从这里进。")).toBeInTheDocument();
    const count = document.querySelector(".projects-count") as HTMLElement;
    expect(within(count).getByText("6")).toBeInTheDocument();
    expect(count).toHaveTextContent("个项目在跟进");
  });

  it("已排座次的用大面板，按座次排，座次 1 在最前", () => {
    renderPage();
    const items = panels();
    expect(items).toHaveLength(2);
    expect(within(items[0]).getByRole("heading", { name: "医米科研用药" })).toBeInTheDocument();
    expect(within(items[0]).getByText("1", { selector: ".project-panel__seat" })).toBeInTheDocument();
    expect(within(items[1]).getByRole("heading", { name: "恒瑞健康" })).toBeInTheDocument();
    expect(within(items[1]).getByText("2", { selector: ".project-panel__seat" })).toBeInTheDocument();
  });

  it("没排座次的用列表行，按最近一场会由近到远", () => {
    renderPage();
    expect(rowNames()).toEqual(["CVM 云讲堂", "黑卡小程序", "CACA", "MDT"]);
  });

  it("面板：进行中需求（最多 2 条）、待认领、待办、会议、录音、最近一场会", () => {
    renderPage({
      projects: [
        { ...YIMI, active_requirement_titles: ["京东科研仓对接", "赠药横跳拦截", "第三条不该显示"], active_requirement_count: 3 },
      ],
    });
    const panel = panels()[0];
    expect(within(panel).getByText("京东科研仓对接")).toBeInTheDocument();
    expect(within(panel).getByText("赠药横跳拦截")).toBeInTheDocument();
    expect(within(panel).queryByText("第三条不该显示")).not.toBeInTheDocument();
    expect(within(panel).getByText("进行中需求 3")).toBeInTheDocument();
    expect(within(panel).getByText("待认领 1")).toBeInTheDocument();
    expect(within(panel).getByText("待办 6")).toBeInTheDocument();
    expect(within(panel).getByText("会议 36")).toBeInTheDocument();
    expect(within(panel).getByText("录音 13 小时 16 分钟")).toBeInTheDocument();
    expect(within(panel).getByText("EDC 系统选型与产研对接决策")).toBeInTheDocument();
    expect(within(panel).getByText("09-28")).toBeInTheDocument();
  });

  it("没有进行中需求、没有待认领时：写明没有，不画待认领小块", () => {
    renderPage();
    const hengrui = panels()[1];
    expect(within(hengrui).getByText("没有进行中的需求")).toBeInTheDocument();
    expect(within(hengrui).queryByText(/待认领/)).not.toBeInTheDocument();
  });

  it("列表行：数字、最近一场会（标题、日期、录音时长）", () => {
    renderPage();
    const row = rows()[1]; // 黑卡小程序
    expect(within(row).getByText("发卡变更与一直拍流程改造沟通")).toBeInTheDocument();
    expect(row).toHaveTextContent("09-22 最近一场 · 录音 1 小时 50 分钟");
    expect(within(row).getByText("会议").nextElementSibling).toHaveTextContent("4");
    expect(within(row).getByText("待办").nextElementSibling).toHaveTextContent("5");
  });

  it("会议数取服务端字段，不受 meetings 分页影响", () => {
    renderPage({ meetings: [] });
    expect(within(panels()[0]).getByText("会议 36")).toBeInTheDocument();
  });

  it("满 3 周显示「已 N 周没有会议」，不满不显示", () => {
    renderPage();
    const [cvm, blackcard, caca, mdt] = rows();
    expect(within(mdt).getByText("已 7 周没有会议")).toBeInTheDocument();
    expect(within(caca).getByText("已 4 周没有会议")).toBeInTheDocument();
    expect(within(blackcard).queryByText(/没有会议/)).not.toBeInTheDocument();
    expect(within(cvm).queryByText(/没有会议/)).not.toBeInTheDocument();
    expect(within(panels()[0]).queryByText(/没有会议/)).not.toBeInTheDocument();
  });

  it("分界：差 2 周不显示，差 3 周才显示", () => {
    renderPage({
      projects: [
        { ...CVM, id: "a", name: "差两周", weeks_since_last_meeting: 2 },
        { ...CVM, id: "b", name: "差三周", weeks_since_last_meeting: 3 },
      ],
    });
    const byName = (name: string) => rows().find((row) => within(row).queryByRole("button", { name })) as HTMLElement;
    const [three, two] = [byName("差三周"), byName("差两周")];
    expect(within(three).getByText("已 3 周没有会议")).toBeInTheDocument();
    expect(within(two).queryByText(/没有会议/)).not.toBeInTheDocument();
  });

  it("列表行上最近一场会的标题、日期、录音时长都在", () => {
    renderPage();
    const mdt = rows()[3];
    expect(within(mdt).getByText("MDT2.0病例汇报模板讨论")).toBeInTheDocument();
    expect(mdt).toHaveTextContent("08-14 最近一场 · 录音 15 小时 32 分钟");
  });

  it("满 3 周的提醒在面板上也有", () => {
    renderPage({ projects: [{ ...YIMI, weeks_since_last_meeting: 4 }] });
    expect(within(panels()[0]).getByText("已 4 周没有会议")).toBeInTheDocument();
  });

  it("没有会议的项目写「还没有会议」", () => {
    renderPage({
      projects: [
        { ...CVM, latest_meeting_date: null, latest_meeting_title: null, meeting_count: 0, recording_ms: 0, weeks_since_last_meeting: null, weekly_meetings: Array(12).fill(0) },
      ],
    });
    expect(within(rows()[0]).getByText("还没有会议")).toBeInTheDocument();
  });

  it("近 12 周节奏：12 根柱子，本周单独标色，0 场是短横线，悬停写几周前、几场", () => {
    renderPage();
    const bars = within(panels()[0]).getByRole("img", { name: "近 12 周会议节奏" }).children;
    expect(bars).toHaveLength(12);
    expect(bars[11]).toHaveClass("is-now");
    expect(Array.from(bars).filter((bar) => bar.classList.contains("is-now"))).toHaveLength(1);
    expect(bars[11]).toHaveAttribute("title", "本周 · 1 场");
    expect(bars[10]).toHaveAttribute("title", "1 周前 · 3 场");
    expect(bars[0]).toHaveAttribute("title", "11 周前 · 0 场");
    expect(bars[0]).toHaveClass("is-zero");
    expect(bars[8]).not.toHaveClass("is-zero");
    expect(within(panels()[0]).getByText("近 12 周 19 场")).toBeInTheDocument();
  });

  it("节奏数组不足 12 格时在前面补 0；本周是 0 场也标色", () => {
    renderPage({ projects: [{ ...BLACKCARD, weekly_meetings: [2, 0] }] });
    const bars = within(rows()[0]).getByRole("img", { name: "近 12 周会议节奏" }).children;
    expect(bars).toHaveLength(12);
    expect(bars[10]).toHaveAttribute("title", "1 周前 · 2 场");
    expect(bars[11]).toHaveClass("is-now", "is-zero");
    expect(bars[11]).toHaveAttribute("title", "本周 · 0 场");
  });

  it("旧后端没有新字段时照常显示，不报错", () => {
    renderPage({
      projects: [
        { id: "old", name: "老项目", color: "#888888", seat: 1, meeting_count: 3, requirement_counts: { active: 1, done: 0, shelved: 0, all: 1 }, open_task_count: 2 },
        { id: "old2", name: "老项目二", color: "#888888", meeting_count: 1, open_task_count: 0 },
      ],
    });
    expect(within(panels()[0]).getByText("进行中需求 1")).toBeInTheDocument();
    expect(within(panels()[0]).getByText("待办 2")).toBeInTheDocument();
    expect(within(panels()[0]).queryByRole("img", { name: "近 12 周会议节奏" })).not.toBeInTheDocument();
    expect(rowNames()).toEqual(["老项目二"]);
  });
});

describe("ProjectsPage 进入项目", () => {
  it("点面板的任意位置、点名字、点「进入」都打开项目详情", async () => {
    const onOpenProject = vi.fn();
    renderPage({ onOpenProject });
    const panel = panels()[0];

    await userEvent.click(within(panel).getByText("EDC 系统选型与产研对接决策"));
    expect(onOpenProject).toHaveBeenLastCalledWith("p-yimi");
    await userEvent.click(within(panel).getByRole("button", { name: "医米科研用药" }));
    expect(onOpenProject).toHaveBeenLastCalledWith("p-yimi");
    await userEvent.click(within(panel).getByRole("button", { name: "进入医米科研用药" }));
    expect(onOpenProject).toHaveBeenLastCalledWith("p-yimi");
    expect(onOpenProject).toHaveBeenCalledTimes(3);
  });

  it("点列表行的任意位置或「进入」打开项目详情，各只打开一次", async () => {
    const onOpenProject = vi.fn();
    renderPage({ onOpenProject });

    await userEvent.click(within(rows()[3]).getByText("MDT2.0病例汇报模板讨论"));
    expect(onOpenProject).toHaveBeenLastCalledWith("p-mdt");

    onOpenProject.mockClear();
    await userEvent.click(within(rows()[0]).getByRole("button", { name: "进入CVM 云讲堂" }));
    expect(onOpenProject).toHaveBeenCalledTimes(1);
    expect(onOpenProject).toHaveBeenCalledWith("p-cvm");
  });
});

describe("ProjectsPage 我的方向", () => {
  it("方向条按座次列出已排的，未排的收在「未排座次」里", () => {
    renderPage();
    const seats = within(screen.getByRole("group", { name: "已排座次的项目" }));
    expect(seats.getAllByRole("button").map((button) => button.textContent)).toEqual([
      expect.stringContaining("医米科研用药"),
      expect.stringContaining("恒瑞健康"),
    ]);
    // 芯片上的数字是未完成任务数
    expect(seats.getByRole("button", { name: /医米科研用药/ })).toHaveTextContent("6");
    expect(screen.getByRole("button", { name: /未排座次/ })).toHaveTextContent("+4");
  });

  it("点选方向条上的项目就只显示这个项目，再点一次恢复", async () => {
    renderPage();
    const seats = within(screen.getByRole("group", { name: "已排座次的项目" }));

    await userEvent.click(seats.getByRole("button", { name: /恒瑞健康/ }));
    expect(panels()).toHaveLength(1);
    expect(within(panels()[0]).getByRole("heading", { name: "恒瑞健康" })).toBeInTheDocument();
    expect(rows()).toHaveLength(0);

    await userEvent.click(seats.getByRole("button", { name: /恒瑞健康/ }));
    expect(panels()).toHaveLength(2);
    expect(rows()).toHaveLength(4);
  });

  it("从「未排座次」里点选一个项目，列表只剩它", async () => {
    renderPage();
    await userEvent.click(screen.getByRole("button", { name: /未排座次/ }));
    const menu = within(screen.getByRole("dialog", { name: "未排座次的项目" }));
    await userEvent.click(menu.getByRole("button", { name: /^MDT/ }));

    expect(panels()).toHaveLength(0);
    expect(rowNames()).toEqual(["MDT"]);
  });

  it("拖动调座次：整排保存到座次接口，并刷新项目列表", async () => {
    const saveProjectSeats = vi.fn().mockResolvedValue({ seats: [] });
    const onProjectsChanged = vi.fn();
    renderPage({ apiClient: makeClient({ saveProjectSeats } as Partial<ApiClient>), onProjectsChanged });
    // jsdom 没有布局：已排座次的项目按先后摆成一排，第 i 个占 [i*100, i*100+80)
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (this: HTMLElement) {
      const index = Array.from(document.querySelectorAll("[data-seat-id]")).indexOf(this);
      const left = index * 100;
      return { left, right: left + 80, width: 80, top: 0, bottom: 30, height: 30, x: left, y: 0, toJSON: () => ({}) };
    });

    const group = screen.getByRole("group", { name: "已排座次的项目" });
    const transfer = dataTransfer();
    const chip = within(group).getByRole("button", { name: /恒瑞健康/ });
    fireEvent.dragStart(chip, { dataTransfer: transfer });
    const over = createEvent.dragOver(group, { dataTransfer: transfer });
    Object.defineProperty(over, "clientX", { value: 10 });
    fireEvent(group, over);
    const drop = createEvent.drop(group, { dataTransfer: transfer });
    Object.defineProperty(drop, "clientX", { value: 10 });
    fireEvent(group, drop);

    await waitFor(() => expect(saveProjectSeats).toHaveBeenCalledWith(["p-hengrui", "p-yimi"]));
    await waitFor(() => expect(onProjectsChanged).toHaveBeenCalled());
    expect(await screen.findByText("座次已保存")).toBeInTheDocument();
  });

  it("「排入座次」把未排的项目接到最后一位", async () => {
    const saveProjectSeats = vi.fn().mockResolvedValue({ seats: [] });
    renderPage({ apiClient: makeClient({ saveProjectSeats } as Partial<ApiClient>) });
    await userEvent.click(screen.getByRole("button", { name: /未排座次/ }));
    const menu = within(screen.getByRole("dialog", { name: "未排座次的项目" }));
    const item = menu.getByRole("button", { name: /^MDT/ }).closest("li") as HTMLElement;
    await userEvent.click(within(item).getByRole("button", { name: "排入座次" }));

    await waitFor(() => expect(saveProjectSeats).toHaveBeenCalledWith(["p-yimi", "p-hengrui", "p-mdt"]));
  });

  it("座次没保存成功：提示重拖，并刷新项目列表", async () => {
    const saveProjectSeats = vi.fn().mockRejectedValue(new Error("项目有变化"));
    const onProjectsChanged = vi.fn();
    renderPage({ apiClient: makeClient({ saveProjectSeats } as Partial<ApiClient>), onProjectsChanged });
    await userEvent.click(screen.getByRole("button", { name: /未排座次/ }));
    const menu = within(screen.getByRole("dialog", { name: "未排座次的项目" }));
    const item = menu.getByRole("button", { name: /^MDT/ }).closest("li") as HTMLElement;
    await userEvent.click(within(item).getByRole("button", { name: "排入座次" }));

    expect(await screen.findByText("座次没保存：项目有变化，已刷新，请再拖一次")).toBeInTheDocument();
    expect(onProjectsChanged).toHaveBeenCalled();
  });
});

describe("ProjectsPage 未排座次太多时折叠", () => {
  const MANY: Project[] = Array.from({ length: 10 }, (_, index) => ({
    id: `p-${index}`,
    name: `项目${index + 1}`,
    color: "#888888",
    seat: null,
    meeting_count: index,
    open_task_count: 0,
    latest_meeting_date: `2026-09-${String(20 - index).padStart(2, "0")}T10:00:00+08:00`,
    material_roots: [],
  }));

  it("默认只列前 8 行，其余折成「还有 N 个项目」，点开就地展开、可以收起", async () => {
    renderPage({ projects: MANY });
    expect(rows()).toHaveLength(8);
    expect(rowNames()[7]).toBe("项目8");

    await userEvent.click(screen.getByRole("button", { name: /还有 2 个项目/ }));
    expect(rows()).toHaveLength(10);
    expect(rowNames()[9]).toBe("项目10");

    await userEvent.click(screen.getByRole("button", { name: /收起/ }));
    expect(rows()).toHaveLength(8);
  });

  it("不超过 8 个就不出现折叠按钮", () => {
    renderPage({ projects: MANY.slice(0, 8) });
    expect(rows()).toHaveLength(8);
    expect(screen.queryByRole("button", { name: /还有/ })).not.toBeInTheDocument();
  });
});

describe("ProjectsPage 查询", () => {
  it("按项目名称查询，座次面板和列表行一起过滤", async () => {
    renderPage();
    await userEvent.type(screen.getByPlaceholderText("输入项目名称"), "MDT");
    await userEvent.click(screen.getByRole("button", { name: "查询" }));

    expect(panels()).toHaveLength(0);
    expect(rowNames()).toEqual(["MDT"]);
  });

  it("按材料根目录已挂/未挂筛选", async () => {
    renderPage();
    await userEvent.selectOptions(screen.getByLabelText("材料根目录"), "attached");
    await userEvent.click(screen.getByRole("button", { name: "查询" }));

    expect(panels()).toHaveLength(1);
    expect(within(panels()[0]).getByRole("heading", { name: "医米科研用药" })).toBeInTheDocument();
    expect(rows()).toHaveLength(0);
  });

  it("重置清空查询条件", async () => {
    renderPage();
    await userEvent.type(screen.getByPlaceholderText("输入项目名称"), "MDT");
    await userEvent.click(screen.getByRole("button", { name: "查询" }));
    expect(rows()).toHaveLength(1);

    await userEvent.click(screen.getByRole("button", { name: "重置" }));
    expect(rows()).toHaveLength(4);
    expect(panels()).toHaveLength(2);
    expect(screen.getByPlaceholderText("输入项目名称")).toHaveValue("");
  });

  it("什么都对不上时写没有符合条件的项目", async () => {
    renderPage();
    await userEvent.type(screen.getByPlaceholderText("输入项目名称"), "不存在的项目");
    await userEvent.click(screen.getByRole("button", { name: "查询" }));
    expect(screen.getByText("没有符合条件的项目")).toBeInTheDocument();
  });
});

describe("ProjectsPage 项目总文件夹（2a）", () => {
  it("标题下一行写项目总文件夹", async () => {
    const projectParent = vi.fn().mockResolvedValue({
      path: "/Volumes/资料盘/项目",
      state: "online",
      reason: null,
      suggested: null,
      unclaimed: { state: "ready", folders: [], total: 0 },
    });
    renderPage({ apiClient: makeClient({ projectParent, setProjectParent: vi.fn() } as Partial<ApiClient>) });

    const heading = screen.getByRole("heading", { name: "项目" }).parentElement!;
    expect(await within(heading).findByText("/Volumes/资料盘/项目")).toBeInTheDocument();
    expect(within(heading).getByRole("button", { name: "改" })).toBeInTheDocument();
  });

});

describe("ProjectsPage 新建 / 编辑项目", () => {
  it("新建项目成功后关闭弹窗、通知父级刷新，并直接进入新项目详情", async () => {
    const onProjectsChanged = vi.fn();
    const onOpenProject = vi.fn();
    const createProjectWith = vi.fn().mockResolvedValue({ id: "new", name: "互联网医院", color: "#3f51b5" });
    renderPage({ apiClient: makeClient({ createProjectWith } as Partial<ApiClient>), onOpenProject, onProjectsChanged });

    await userEvent.click(screen.getByRole("button", { name: "＋ 新建项目" }));
    await userEvent.type(screen.getByPlaceholderText("例如：互联网医院"), "互联网医院");
    await userEvent.click(screen.getByRole("button", { name: "创建" }));

    expect(createProjectWith).toHaveBeenCalledWith({ name: "互联网医院", color: expect.stringMatching(/^#/) });
    await waitFor(() => {
      expect(screen.queryByRole("dialog", { name: "新建项目" })).not.toBeInTheDocument();
    });
    expect(onProjectsChanged).toHaveBeenCalled();
    expect(onOpenProject).toHaveBeenCalledWith("new");
  });

  it("点编辑只开编辑弹窗，不会同时打开项目详情", async () => {
    const onOpenProject = vi.fn();
    renderPage({ onOpenProject });
    await userEvent.click(within(panels()[0]).getByRole("button", { name: "编辑医米科研用药" }));
    expect(screen.getByRole("dialog", { name: "编辑项目" })).toBeInTheDocument();
    expect(onOpenProject).not.toHaveBeenCalled();
  });

  it("编辑项目保存后留在列表原地刷新，不跳转详情", async () => {
    const onProjectsChanged = vi.fn();
    const onOpenProject = vi.fn();
    const updateProject = vi.fn().mockResolvedValue({ id: "p-yimi", name: "医米科研用药", color: "#2c8d83" });
    renderPage({ apiClient: makeClient({ updateProject } as Partial<ApiClient>), onOpenProject, onProjectsChanged });

    await userEvent.click(within(panels()[0]).getByRole("button", { name: "编辑医米科研用药" }));
    await userEvent.click(screen.getByRole("button", { name: "保存" }));

    await waitFor(() => {
      expect(screen.queryByRole("dialog", { name: "编辑项目" })).not.toBeInTheDocument();
    });
    expect(onProjectsChanged).toHaveBeenCalled();
    expect(onOpenProject).not.toHaveBeenCalled();
  });

  it("编辑项目预填当前名称与颜色", async () => {
    renderPage();
    await userEvent.click(within(rows()[3]).getByRole("button", { name: "编辑MDT" }));

    expect(screen.getByRole("dialog", { name: "编辑项目" })).toBeInTheDocument();
    expect(screen.getByPlaceholderText("例如：互联网医院")).toHaveValue("MDT");
  });
});

describe("ProjectsPage 移动端只读", () => {
  it("canEdit 为 false 时不显示新建项目、编辑与新建标签", () => {
    renderPage({ canEdit: false });

    expect(screen.queryByRole("button", { name: "＋ 新建项目" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "编辑" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "新建标签" })).not.toBeInTheDocument();
    // 全部项目概览只在电脑上有
    expect(screen.queryByRole("link", { name: "全部项目图" })).not.toBeInTheDocument();
  });

  it("电脑上标题旁有［全部项目图］，链接到全部项目概览", () => {
    renderPage();
    expect(screen.getByRole("link", { name: "全部项目图" })).toHaveAttribute("href", "#graph");
  });
});

describe("ProjectsPage 标签目录", () => {
  it("创建标签", async () => {
    const onCreateTag = vi.fn().mockResolvedValue(undefined);
    renderPage({ onCreateTag });

    await userEvent.type(screen.getByLabelText("标签名称"), "需复盘");
    await userEvent.click(screen.getByRole("button", { name: "新建标签" }));

    expect(onCreateTag).toHaveBeenCalledWith("需复盘", expect.stringMatching(/^#/));
    expect(await screen.findByText("标签已创建")).toBeInTheDocument();
  });
});
