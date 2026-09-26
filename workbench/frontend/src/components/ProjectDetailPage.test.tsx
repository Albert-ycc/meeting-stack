import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { ProjectDetailPage } from "./ProjectDetailPage";
import type { ApiClient } from "../api";
import type {
  ProjectBoard,
  ProjectMeetingRow,
  RequirementsPayload,
  RequirementSummary,
} from "../types";

const baseBoard: ProjectBoard = {
  id: "project-1",
  name: "云图科研用药",
  color: "#2c8d83",
  meeting_count: 33,
  requirement_counts: { active: 6, done: 5, shelved: 1, all: 12 },
  open_task_count: 17,
  material_roots: [
    { id: 1, project_id: "project-1", path: "/Volumes/资料盘/蓝鲸云/云图科研用药", exists: true, created_at: "2026-09-01T00:00:00Z" },
  ],
  meetings: [],
};

const EMPTY_REQUIREMENTS: RequirementsPayload = {
  items: [],
  total: 0,
  limit: 10,
  offset: 0,
  counts: { active: 0, done: 0, shelved: 0, all: 0 },
};

function requirementRow(overrides: Partial<RequirementSummary> = {}): RequirementSummary {
  return {
    id: "req-1",
    project_id: "project-1",
    project_name: "云图科研用药",
    project_color: "#2c8d83",
    title: "北辰仓快递配送",
    priority: "P0",
    status: "active",
    created_at: "2026-09-07T00:00:00Z",
    updated_at: "2026-09-09T00:00:00Z",
    open_task_count: 3,
    meeting_count: 2,
    latest_meeting_date: "2026-09-09T06:00:00Z",
    folder_count: 2,
    ...overrides,
  };
}

function client(overrides: Partial<ApiClient> = {}) {
  return {
    projectBoard: vi.fn().mockResolvedValue(baseBoard),
    projectMaterialSubfolders: vi.fn().mockResolvedValue({ roots: [{ root_id: 1, root_path: baseBoard.material_roots![0].path, exists: true, folders: Array.from({ length: 29 }, (_, i) => ({ name: `f${i}`, path: `p${i}`, exists: true, file_count: 1, file_count_capped: false, modified_at: null })) }] }),
    projectMeetings: vi.fn().mockResolvedValue([]),
    requirements: vi.fn().mockResolvedValue(EMPTY_REQUIREMENTS),
    removeProjectMaterialRoot: vi.fn().mockResolvedValue({ ok: true }),
    addProjectMaterialRoot: vi.fn().mockResolvedValue({ id: 2, project_id: "project-1", path: "/x", exists: true, created_at: "2026-09-15T00:00:00Z" }),
    updateProject: vi.fn().mockResolvedValue(baseBoard),
    ...overrides,
  } as unknown as ApiClient;
}

function renderPage(overrides: Partial<Parameters<typeof ProjectDetailPage>[0]> = {}) {
  return render(
    <ProjectDetailPage
      apiClient={client()}
      canPickFolders
      canWrite
      onBack={vi.fn()}
      onOpenGlossary={vi.fn()}
      onOpenMeeting={vi.fn()}
      onOpenRequirement={vi.fn()}
      onOpenTask={vi.fn()}
      projectId="project-1"
      projects={[]}
      {...overrides}
    />,
  );
}

describe("ProjectDetailPage 词典区", () => {
  it("后端已带 glossary 字段：显示计数与前几条术语 chip", async () => {
    renderPage({
      apiClient: client({
        projectBoard: vi.fn().mockResolvedValue({
          ...baseBoard,
          glossary_count: 2,
          glossary_terms: [
            { id: "term-1", term: "生长激素", aliases: [], category: "药品" },
            { id: "term-2", term: "骨龄", aliases: ["骨骼年龄"], category: "术语" },
          ],
        }),
      } as Partial<ApiClient>),
    });

    expect(await screen.findByText("生长激素")).toBeTruthy();
    expect(screen.getByText("骨龄")).toBeTruthy();
    expect(screen.getByText("2 条术语")).toBeTruthy();
  });

  it("后端还没带 glossary 字段：不报错，渲染成空态", async () => {
    renderPage();

    expect(await screen.findByText("这个项目还没有挂靠的术语")).toBeTruthy();
    expect(screen.getByText("0 条术语")).toBeTruthy();
  });

  it("点击「在词典中查看」调用 onOpenGlossary 并带上当前项目 id", async () => {
    const onOpenGlossary = vi.fn();
    renderPage({ onOpenGlossary });

    fireEvent.click(await screen.findByText("在词典中查看 →"));

    await waitFor(() => expect(onOpenGlossary).toHaveBeenCalledWith("project-1"));
  });
});

describe("ProjectDetailPage 页头", () => {
  it("面包屑、色点、名称与会议·需求·未完成任务小计", async () => {
    renderPage();

    expect(await screen.findByRole("heading", { name: "云图科研用药" })).toBeInTheDocument();
    expect(screen.getByText("会议 33 场 · 需求 12 个 · 未完成任务 17 条")).toBeInTheDocument();
  });
});

describe("ProjectDetailPage 材料根目录卡", () => {
  it("展示路径、子文件夹数与复制路径", async () => {
    renderPage();

    expect(await screen.findByText("/Volumes/资料盘/蓝鲸云/云图科研用药")).toBeInTheDocument();
    expect(screen.getByText("29 个子文件夹")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "复制路径" })).toBeInTheDocument();
  });

  it("找不到目录时显示异常态与重新选择", async () => {
    renderPage({
      apiClient: client({
        projectBoard: vi.fn().mockResolvedValue({
          ...baseBoard,
          material_roots: [{ ...baseBoard.material_roots![0], exists: false }],
        }),
      } as Partial<ApiClient>),
    });

    expect(await screen.findByText("找不到该目录")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "重新选择" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "复制路径" })).not.toBeInTheDocument();
  });

  it("资料盘没插时只提示未连接，不让重新选择", async () => {
    renderPage({
      apiClient: client({
        projectBoard: vi.fn().mockResolvedValue({
          ...baseBoard,
          material_roots: [{ ...baseBoard.material_roots![0], exists: false, state: "volume_offline" }],
        }),
      } as Partial<ApiClient>),
    });

    expect(await screen.findByText("资料盘未连接，插上后自动恢复")).toBeInTheDocument();
    expect(screen.queryByText("找不到该目录")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "重新选择" })).not.toBeInTheDocument();
  });

  it("重新选择走原子替换，不再先删后加", async () => {
    const replaceProjectMaterialRoot = vi.fn().mockResolvedValue({
      ...baseBoard.material_roots![0],
      path: "/Volumes/资料盘/蓝鲸云",
      exists: true,
      state: "online",
    });
    const removeProjectMaterialRoot = vi.fn();
    const addProjectMaterialRoot = vi.fn();
    const browseMaterials = vi.fn().mockResolvedValue({
      base: "/Volumes/资料盘",
      path: "/Volumes/资料盘",
      parent: null,
      breadcrumbs: [{ name: "资料盘", path: "/Volumes/资料盘" }],
      dirs: [{ name: "蓝鲸云", path: "/Volumes/资料盘/蓝鲸云" }],
    });
    renderPage({
      apiClient: client({
        projectBoard: vi.fn().mockResolvedValue({
          ...baseBoard,
          material_roots: [{ ...baseBoard.material_roots![0], exists: false, state: "missing" }],
        }),
        replaceProjectMaterialRoot,
        removeProjectMaterialRoot,
        addProjectMaterialRoot,
        browseMaterials,
      } as Partial<ApiClient>),
    });

    await userEvent.click(await screen.findByRole("button", { name: "重新选择" }));
    await userEvent.click(await screen.findByText("蓝鲸云"));
    await userEvent.click(screen.getByRole("button", { name: "确定" }));

    expect(replaceProjectMaterialRoot).toHaveBeenCalledWith(
      "project-1",
      baseBoard.material_roots![0].id,
      "/Volumes/资料盘/蓝鲸云",
    );
    expect(removeProjectMaterialRoot).not.toHaveBeenCalled();
    expect(addProjectMaterialRoot).not.toHaveBeenCalled();
  });

  it("没有根目录时显示空态", async () => {
    renderPage({
      apiClient: client({
        projectBoard: vi.fn().mockResolvedValue({ ...baseBoard, material_roots: [] }),
      } as Partial<ApiClient>),
    });

    expect(await screen.findByText("还没有材料根目录")).toBeInTheDocument();
  });

  it("移除材料根目录需二次确认", async () => {
    const removeProjectMaterialRoot = vi.fn().mockResolvedValue({ ok: true });
    renderPage({ apiClient: client({ removeProjectMaterialRoot } as Partial<ApiClient>) });

    await screen.findByText("/Volumes/资料盘/蓝鲸云/云图科研用药");
    await userEvent.click(screen.getByRole("button", { name: "移除" }));

    const dialog = screen.getByRole("alertdialog", { name: "移除材料根目录" });
    expect(within(dialog).getByText("/Volumes/资料盘/蓝鲸云/云图科研用药")).toBeInTheDocument();
    await userEvent.click(within(dialog).getByRole("button", { name: "移除" }));

    expect(removeProjectMaterialRoot).toHaveBeenCalledWith("project-1", 1);
    await waitFor(() => expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument());
  });

  it("移除失败时原因写在确认弹窗里，弹窗不关", async () => {
    const removeProjectMaterialRoot = vi.fn().mockRejectedValue(new Error("目录正被需求引用"));
    renderPage({ apiClient: client({ removeProjectMaterialRoot } as Partial<ApiClient>) });

    await screen.findByText("/Volumes/资料盘/蓝鲸云/云图科研用药");
    await userEvent.click(screen.getByRole("button", { name: "移除" }));
    const dialog = screen.getByRole("alertdialog", { name: "移除材料根目录" });
    await userEvent.click(within(dialog).getByRole("button", { name: "移除" }));

    expect(await within(dialog).findByRole("alert")).toHaveTextContent("目录正被需求引用");
    expect(screen.getByRole("alertdialog", { name: "移除材料根目录" })).toBeInTheDocument();
  });

  it("复制路径成功后弹出轻提示", async () => {
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText: vi.fn().mockResolvedValue(undefined) },
    });
    renderPage();

    await userEvent.click(await screen.findByRole("button", { name: "复制路径" }));
    expect(await screen.findByText("已复制路径")).toBeInTheDocument();
  });

  it("canPickFolders 为 false 时隐藏添加/移除/重新选择，仍保留复制路径", async () => {
    renderPage({ canPickFolders: false });

    await screen.findByText("/Volumes/资料盘/蓝鲸云/云图科研用药");
    expect(screen.queryByRole("button", { name: "＋ 添加目录" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "移除" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "复制路径" })).toBeInTheDocument();
  });

  it("D27：挂根目录选中隐藏目录时后端 400，取径器就地显示原因，不关弹窗、不换成页面级提示", async () => {
    const addProjectMaterialRoot = vi.fn().mockRejectedValue(new Error("不能选择隐藏目录"));
    const browseMaterials = vi.fn().mockResolvedValue({
      base: "/Volumes/资料盘",
      path: "/Volumes/资料盘",
      parent: null,
      breadcrumbs: [{ name: "资料盘", path: "/Volumes/资料盘" }],
      dirs: [{ name: "蓝鲸云", path: "/Volumes/资料盘/蓝鲸云" }],
    });
    renderPage({ apiClient: client({ addProjectMaterialRoot, browseMaterials } as Partial<ApiClient>) });

    await userEvent.click(await screen.findByRole("button", { name: "＋ 添加目录" }));
    await userEvent.click(await screen.findByText("蓝鲸云"));
    await userEvent.click(screen.getByRole("button", { name: "确定" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("不能选择隐藏目录");
    // 取径器还开着（没被吞掉、没关闭）
    expect(screen.getByRole("dialog", { name: "添加材料根目录" })).toBeInTheDocument();
    // 页面级 notice 没有被这条错误占用
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });
});

describe("ProjectDetailPage 需求卡", () => {
  it("按页签取数并渲染表格", async () => {
    const requirements = vi.fn().mockImplementation(async (filters) => {
      if (filters.status === "done") {
        return {
          items: [requirementRow({ id: "req-done", title: "北辰直邮初版原型", status: "done", priority: "P0" })],
          total: 1,
          limit: 10,
          offset: 0,
          counts: { active: 6, done: 5, shelved: 1, all: 12 },
        };
      }
      return {
        items: [requirementRow()],
        total: 6,
        limit: 10,
        offset: 0,
        counts: { active: 6, done: 5, shelved: 1, all: 12 },
      };
    });
    const onOpenRequirement = vi.fn();
    renderPage({ apiClient: client({ requirements } as Partial<ApiClient>), onOpenRequirement });

    expect(await screen.findByText("北辰仓快递配送")).toBeInTheDocument();
    expect(requirements).toHaveBeenCalledWith(
      expect.objectContaining({ project_id: "project-1", status: "active", limit: 10, offset: 0 }),
    );

    await userEvent.click(screen.getByRole("tab", { name: /已完成/ }));
    expect(await screen.findByText("北辰直邮初版原型")).toBeInTheDocument();
    expect(requirements).toHaveBeenLastCalledWith(
      expect.objectContaining({ status: "done", offset: 0 }),
    );

    await userEvent.click(screen.getByRole("button", { name: "查看" }));
    expect(onOpenRequirement).toHaveBeenCalledWith("req-done");
  });

  it("没有需求时显示空态", async () => {
    renderPage();
    expect(await screen.findByText("还没有需求")).toBeInTheDocument();
  });
});

describe("ProjectDetailPage 会议卡", () => {
  function meetingRow(overrides: Partial<ProjectMeetingRow> = {}): ProjectMeetingRow {
    return {
      id: "m1",
      title: "260908 云图需求梳理与北辰科研仓对接",
      recording_date: "2026-09-08T06:05:00Z",
      duration_ms: 50 * 60_000,
      canonical_dir: null,
      requirements: [
        { id: "r1", title: "北辰仓快递配送", priority: "P0", status: "active", project_id: "project-1" },
        { id: "r2", title: "复审流程可配置", priority: "P1", status: "active", project_id: "project-1" },
        { id: "r3", title: "库存盘点", priority: "P3", status: "active", project_id: "project-1" },
      ],
      ...overrides,
    };
  }

  it("关联需求超过两个时折叠成「等 N 个」", async () => {
    renderPage({ apiClient: client({ projectMeetings: vi.fn().mockResolvedValue([meetingRow()]) } as Partial<ApiClient>) });

    expect(await screen.findByText("北辰仓快递配送")).toBeInTheDocument();
    expect(screen.getByText("复审流程可配置")).toBeInTheDocument();
    expect(screen.getByText("等 1 个")).toBeInTheDocument();
    expect(screen.queryByText("库存盘点")).not.toBeInTheDocument();
  });

  it("点打开跳到会议详情", async () => {
    const onOpenMeeting = vi.fn();
    renderPage({
      apiClient: client({ projectMeetings: vi.fn().mockResolvedValue([meetingRow()]) } as Partial<ApiClient>),
      onOpenMeeting,
    });

    await userEvent.click(await screen.findByRole("button", { name: "打开" }));
    expect(onOpenMeeting).toHaveBeenCalledWith("m1");
  });

  it("没有会议时显示空态", async () => {
    renderPage();
    expect(await screen.findByText("还没有会议")).toBeInTheDocument();
  });

  it("超过 8 场按页码条分页（客户端切片）", async () => {
    const rows = Array.from({ length: 9 }, (_, index) => meetingRow({ id: `m${index}`, title: `会议 ${index}`, requirements: [] }));
    renderPage({ apiClient: client({ projectMeetings: vi.fn().mockResolvedValue(rows) } as Partial<ApiClient>) });

    await screen.findByText("会议 0");
    expect(screen.queryByText("会议 8")).not.toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "2" }));
    expect(await screen.findByText("会议 8")).toBeInTheDocument();
  });
});

describe("ProjectDetailPage 编辑项目与新建需求", () => {
  it("编辑项目打开弹窗并预填当前信息", async () => {
    renderPage();
    await screen.findByRole("heading", { name: "云图科研用药" });

    await userEvent.click(screen.getByRole("button", { name: "编辑项目" }));
    expect(screen.getByRole("dialog", { name: "编辑项目" })).toBeInTheDocument();
    expect(screen.getByPlaceholderText("例如：互联网医院")).toHaveValue("云图科研用药");
  });

  it("canWrite 为 false 时不显示编辑项目 / 新建需求 / 添加目录", async () => {
    renderPage({ canWrite: false });
    await screen.findByRole("heading", { name: "云图科研用药" });

    expect(screen.queryByRole("button", { name: "编辑项目" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "＋ 新建需求" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "＋ 添加目录" })).not.toBeInTheDocument();
  });
});

describe("ProjectDetailPage 系统怎么认出这个项目", () => {
  const profile = {
    also_names: [{ name: "云图", source: "manual" as const }],
    folder_names: ["云图科研用药"],
    cue_terms: { total: 4, cue: 2 },
    auto_30d: 9,
    corrected_30d: 1,
  };

  it("board 带 profile 时显示识别卡，加叫法后重读项目", async () => {
    const projectBoard = vi.fn().mockResolvedValue({ ...baseBoard, profile });
    const updateProject = vi.fn().mockResolvedValue(baseBoard);
    const onProjectsChanged = vi.fn();
    renderPage({ apiClient: client({ projectBoard, updateProject }), onProjectsChanged });

    const card = await screen.findByRole("region", { name: "系统怎么认出这个项目" });
    expect(card).toHaveTextContent("自动归入 9 场、你改走 1 场");
    await userEvent.click(within(card).getByRole("button", { name: "＋ 添加叫法" }));
    await userEvent.type(within(card).getByRole("textbox", { name: "新的叫法" }), "云图EDC{Enter}");

    expect(updateProject).toHaveBeenCalledWith("project-1", { also_names: ["云图", "云图EDC"] });
    await waitFor(() => expect(projectBoard).toHaveBeenCalledTimes(2));
    expect(onProjectsChanged).toHaveBeenCalled();
  });

  it("编辑弹窗里合并到别的项目后跳到目标项目", async () => {
    const target = { id: "project-2", name: "数据中台", color: "#3f51b5" };
    const mergeProject = vi.fn().mockResolvedValue(target);
    const onOpenProject = vi.fn();
    renderPage({
      apiClient: client({ mergeProject }),
      onOpenProject,
      projects: [{ ...baseBoard }, target],
    });

    await userEvent.click(await screen.findByRole("button", { name: "编辑项目" }));
    await userEvent.click(screen.getByRole("button", { name: "合并到…" }));
    await userEvent.selectOptions(screen.getByRole("combobox", { name: "合并到哪个项目" }), "project-2");
    await userEvent.click(screen.getByRole("button", { name: "确认合并" }));

    expect(mergeProject).toHaveBeenCalledWith("project-1", "project-2");
    expect(onOpenProject).toHaveBeenCalledWith("project-2");
  });
});

describe("ProjectDetailPage 冷启动提示", () => {
  it("AI 建的、没挂文件夹的空项目给出挂文件夹、合并、删除", async () => {
    const orphan = {
      ...baseBoard,
      origin: "ai" as const,
      material_roots: [],
      meeting_count: 0,
      requirement_counts: { active: 0, done: 0, shelved: 0, all: 0 },
    };
    renderPage({
      apiClient: client({ projectBoard: vi.fn().mockResolvedValue(orphan) }),
      projects: [orphan, { id: "project-2", name: "数据中台", color: "#3f51b5" }],
    });

    const hint = await screen.findByRole("note");
    expect(hint).toHaveTextContent("这个项目是 AI 自动建的，还没挂文件夹");
    expect(within(hint).getByRole("button", { name: "挂上文件夹" })).toBeInTheDocument();
    expect(within(hint).getByRole("button", { name: "删除" })).toBeInTheDocument();
    await userEvent.click(within(hint).getByRole("button", { name: "合并到…" }));
    expect(screen.getByRole("combobox", { name: "合并到哪个项目" })).toBeInTheDocument();
  });

  it("同一个文件夹还挂在别的项目下时说清卡片写给谁", async () => {
    const shared = {
      ...baseBoard,
      material_roots: [
        {
          ...baseBoard.material_roots![0],
          shared_with: [{ project_id: "project-0", project_name: "云图老项目" }],
          cards_owner_id: "project-0",
        },
      ],
    };
    renderPage({ apiClient: client({ projectBoard: vi.fn().mockResolvedValue(shared) }) });

    expect(await screen.findByText(/也挂在「云图老项目」下/)).toHaveTextContent(
      "会议卡片只写给先挂上的「云图老项目」，不需要可以在这里移除",
    );
  });
});
