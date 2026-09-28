import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { INDEX_POLL_MS, ProjectDetailPage } from "./ProjectDetailPage";
import { ApiError, type ApiClient, type ProjectTimelinePayload } from "../api";
import { LinksFlagsContext } from "./links/LinksFlagsContext";
import type {
  MaterialCoverageRoot,
  MaterialIndexRoot,
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
  it("列出项目词（错写、也叫）和总数，另有公共词一行", async () => {
    const onOpenGlossary = vi.fn();
    renderPage({
      onOpenGlossary,
      apiClient: client({
        projectBoard: vi.fn().mockResolvedValue({
          ...baseBoard,
          glossary_count: 52,
          public_glossary_count: 41,
          glossary_terms: [
            { id: "term-1", term: "生长激素", aliases: [], category: "药品", also: ["GH"] },
            { id: "term-2", term: "骨龄", aliases: ["骨骼年龄"], category: "术语" },
          ],
        }),
      } as Partial<ApiClient>),
    });

    expect(await screen.findByText("生长激素")).toBeTruthy();
    expect(screen.getByText("也叫 GH")).toBeTruthy();
    expect(screen.getByText("骨骼年龄")).toBeTruthy();
    expect(screen.getByText("52 条项目词")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "还有 50 条，在词典中查看 →" }));
    expect(onOpenGlossary).toHaveBeenCalledWith("project-1");
    fireEvent.click(screen.getByRole("button", { name: "另有 41 条公共词也会用于本项目 →" }));
    expect(onOpenGlossary).toHaveBeenCalledWith("public");
  });

  it("后端还没带 glossary 字段：不报错，渲染成空态", async () => {
    renderPage();

    expect(await screen.findByText("项目词只在这个项目的会里用来纠错和识别项目")).toBeTruthy();
    expect(screen.getByText("0 条项目词")).toBeTruthy();
  });

  it("点击「在词典中查看」调用 onOpenGlossary 并带上当前项目 id", async () => {
    const onOpenGlossary = vi.fn();
    renderPage({ onOpenGlossary });

    fireEvent.click(await screen.findByText("在词典中查看 →"));

    await waitFor(() => expect(onOpenGlossary).toHaveBeenCalledWith("project-1"));
  });

  it("输入正确写法回车，再接着输入错写，空着回车就加入", async () => {
    const createGlossaryTerm = vi.fn().mockResolvedValue({});
    const projectBoard = vi.fn().mockResolvedValue(baseBoard);
    renderPage({ apiClient: client({ createGlossaryTerm, projectBoard } as Partial<ApiClient>) });

    await userEvent.type(await screen.findByLabelText("项目词的正确写法"), "初审规则{Enter}");
    await userEvent.type(screen.getByLabelText("错写"), "出审规则{Enter}");
    expect(screen.getByText("出审规则")).toBeTruthy();
    await userEvent.type(screen.getByLabelText("错写"), "{Enter}");

    await waitFor(() =>
      expect(createGlossaryTerm).toHaveBeenCalledWith({
        term: "初审规则",
        aliases: ["出审规则"],
        project_id: "project-1",
        source: "manual",
        confirmed: true,
      }),
    );
    expect(await screen.findByText("已加入项目词「初审规则」（错写：出审规则）")).toBeTruthy();
    expect(projectBoard).toHaveBeenCalledTimes(2);
  });

  it("撞上公共词典里的同名词：就地把新错写加到那条", async () => {
    const conflict = { term_id: "gt-1", term: "随访", project_id: null, project_name: null, aliases: ["随方"], also: [] };
    const createGlossaryTerm = vi.fn().mockRejectedValue(new ApiError("「随访」已在 公共 词典", 409, { conflict }));
    const mergeGlossaryTerm = vi.fn().mockResolvedValue({});
    renderPage({ apiClient: client({ createGlossaryTerm, mergeGlossaryTerm } as Partial<ApiClient>) });

    await userEvent.type(await screen.findByLabelText("项目词的正确写法"), "随访{Enter}");
    await userEvent.type(screen.getByLabelText("错写"), "随仿{Enter}{Enter}");

    expect(await screen.findByText("『随访』已在 公共 词典（错写：随方）")).toBeTruthy();
    await userEvent.click(screen.getByRole("button", { name: "把新错写加到那条" }));
    expect(mergeGlossaryTerm).toHaveBeenCalledWith("gt-1", { aliases: ["随仿"], make_public: false });
    expect(await screen.findByText("已加到 公共 的『随访』")).toBeTruthy();
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

  it("找不到目录、没有改名候选时显示异常态与重新选…", async () => {
    renderPage({
      apiClient: client({
        projectBoard: vi.fn().mockResolvedValue({
          ...baseBoard,
          material_roots: [{ ...baseBoard.material_roots![0], exists: false }],
        }),
      } as Partial<ApiClient>),
    });

    expect(await screen.findByText("找不到该目录")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "重新选…" })).toBeInTheDocument();
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
    expect(screen.queryByRole("button", { name: "重新选…" })).not.toBeInTheDocument();
  });

  it("重新选…走原子替换，不再先删后加", async () => {
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

    await userEvent.click(await screen.findByRole("button", { name: "重新选…" }));
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

describe("ProjectDetailPage 盘不在时建的项目", () => {
  const pendingBoard = (state: "waiting" | "stopped", reason: string | null = null): ProjectBoard => ({
    ...baseBoard,
    material_roots: [],
    pending_folder: { path: "/Volumes/资料盘/项目/云图看板", parent: "/Volumes/资料盘/项目", state, reason },
  });
  const browseMaterials = vi.fn().mockResolvedValue({
    base: "/Volumes/资料盘",
    path: "/Volumes/资料盘",
    parent: null,
    breadcrumbs: [{ name: "资料盘", path: "/Volumes/资料盘" }],
    dirs: [{ name: "新位置", path: "/Volumes/资料盘/新位置" }],
  });

  it("在等：一句话说插上后自动建，没有按钮", async () => {
    renderPage({ apiClient: client({ projectBoard: vi.fn().mockResolvedValue(pendingBoard("waiting")) }) });

    expect(await screen.findByText("资料盘未连接，插上后自动建 /Volumes/资料盘/项目/云图看板")).toBeInTheDocument();
    expect(screen.queryByText("还没有材料根目录")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "重新选位置…" })).not.toBeInTheDocument();
  });

  it("停了：写原因和［重新选位置…］，换了位置当场建好", async () => {
    const movePendingFolder = vi.fn().mockResolvedValue({
      ...baseBoard,
      pending_folder: null,
      material_roots: [{ id: 3, project_id: "project-1", path: "/Volumes/资料盘/新位置/云图看板", exists: true, created_at: "" }],
    });
    const projectBoard = vi.fn().mockResolvedValue(pendingBoard("stopped", "要放新文件夹的位置不存在了"));
    renderPage({ apiClient: client({ projectBoard, movePendingFolder, browseMaterials } as Partial<ApiClient>) });

    expect(await screen.findByText("要放新文件夹的位置不存在了")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "重新选位置…" }));
    const picker = screen.getByRole("dialog", { name: "重新选位置" });
    await userEvent.click(await within(picker).findByText("新位置"));
    await userEvent.click(within(picker).getByRole("button", { name: "确定" }));

    expect(movePendingFolder).toHaveBeenCalledWith("project-1", "/Volumes/资料盘/新位置");
    expect(await screen.findByText("已建好 /Volumes/资料盘/新位置/云图看板，挂到了这个项目")).toBeInTheDocument();
    expect(projectBoard).toHaveBeenCalledTimes(2);
  });

  it("取径器里选「不建了，以后自己挂文件夹」", async () => {
    const dropPendingFolder = vi.fn().mockResolvedValue({ ok: true });
    renderPage({
      apiClient: client({
        projectBoard: vi.fn().mockResolvedValue(pendingBoard("stopped", "没有权限在这里建文件夹")),
        movePendingFolder: vi.fn(),
        dropPendingFolder,
        browseMaterials,
      } as Partial<ApiClient>),
    });

    await userEvent.click(await screen.findByRole("button", { name: "重新选位置…" }));
    await userEvent.click(await screen.findByRole("button", { name: "不建了，以后自己挂文件夹" }));

    expect(dropPendingFolder).toHaveBeenCalledWith("project-1");
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(await screen.findByText("好的，不建了；以后在这里挂文件夹就行")).toBeInTheDocument();
  });
});

describe("ProjectDetailPage 文件夹改名后找回", () => {
  const missingBoard: ProjectBoard = {
    ...baseBoard,
    material_roots: [{ ...baseBoard.material_roots![0], exists: false, state: "missing" }],
  };
  const candidate = (name: string, strength: 1 | 2 | 3, text: string) => ({
    path: `/Volumes/资料盘/蓝鲸云/${name}`,
    name,
    modified_at: "2026-09-20T00:00:00Z",
    strength,
    evidence: { kind: strength === 3 ? ("cards" as const) : ("name" as const), text },
  });

  it("有默认候选：问是不是改了名，［是它］后说一起改了哪些并重读项目", async () => {
    const renameCandidates = vi.fn().mockResolvedValue({
      state: "ready",
      candidates: [candidate("云图科研用药-2026", 3, "里面有这个项目的会议卡片")],
      default_path: "/Volumes/资料盘/蓝鲸云/云图科研用药-2026",
    });
    const repointProjectMaterialRoot = vi.fn().mockResolvedValue({
      ...baseBoard.material_roots![0],
      path: "/Volumes/资料盘/蓝鲸云/云图科研用药-2026",
      moved_roots: [{ id: 7, project_id: "project-9", project_name: "北辰仓储", old: "/a", new: "/b" }],
      moved_folders: 3,
      cards_written: 2,
    });
    const projectBoard = vi.fn().mockResolvedValue(missingBoard);
    renderPage({ apiClient: client({ projectBoard, renameCandidates, repointProjectMaterialRoot } as Partial<ApiClient>) });

    const question = await screen.findByRole("group", { name: "是不是改了名" });
    expect(question).toHaveTextContent(
      "找不到这个文件夹了。是不是改名成了『云图科研用药-2026』？（里面有这个项目的会议卡片）",
    );
    expect(within(question).getByRole("button", { name: "不是" })).toBeInTheDocument();
    expect(within(question).getByRole("button", { name: "重新选…" })).toBeInTheDocument();
    expect(screen.queryByText("找不到该目录")).not.toBeInTheDocument();

    await userEvent.click(within(question).getByRole("button", { name: "是它" }));
    expect(repointProjectMaterialRoot).toHaveBeenCalledWith("project-1", 1, "/Volumes/资料盘/蓝鲸云/云图科研用药-2026");
    expect(
      await screen.findByText(
        "材料根目录已改到 /Volumes/资料盘/蓝鲸云/云图科研用药-2026，嵌在里面的『北辰仓储』文件夹一起改了，3 个需求文件夹一起改了，已补写 2 张会议卡片",
      ),
    ).toBeInTheDocument();
    expect(projectBoard).toHaveBeenCalledTimes(2);
  });

  it("［不是］记下来再重查候选", async () => {
    const renameCandidates = vi
      .fn()
      .mockResolvedValueOnce({
        state: "ready",
        candidates: [candidate("云图2026", 1, "名字相近")],
        default_path: "/Volumes/资料盘/蓝鲸云/云图2026",
      })
      .mockResolvedValueOnce({ state: "ready", candidates: [], default_path: null });
    const declineRenameCandidate = vi.fn().mockResolvedValue({ ok: true });
    renderPage({
      apiClient: client({
        projectBoard: vi.fn().mockResolvedValue(missingBoard),
        renameCandidates,
        declineRenameCandidate,
      } as Partial<ApiClient>),
    });

    await userEvent.click(await screen.findByRole("button", { name: "不是" }));
    expect(declineRenameCandidate).toHaveBeenCalledWith("project-1", 1, "/Volumes/资料盘/蓝鲸云/云图2026");
    // 没候选了：回到原来的说法
    expect(await screen.findByText("找不到该目录")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "重新选…" })).toBeInTheDocument();
  });

  it("前两名一样强时不默认选，逐个列出各带［是它］", async () => {
    const renameCandidates = vi.fn().mockResolvedValue({
      state: "ready",
      candidates: [candidate("云图A", 3, "里面有这个项目的会议卡片"), candidate("云图B", 3, "里面有这个项目的会议卡片")],
      default_path: null,
    });
    renderPage({ apiClient: client({ projectBoard: vi.fn().mockResolvedValue(missingBoard), renameCandidates }) });

    const question = await screen.findByRole("group", { name: "是不是改了名" });
    expect(question).toHaveTextContent("是不是改名成了下面哪一个？");
    expect(within(question).getByText("『云图A』（里面有这个项目的会议卡片）")).toBeInTheDocument();
    expect(within(question).getByText("『云图B』（里面有这个项目的会议卡片）")).toBeInTheDocument();
    expect(within(question).getAllByRole("button", { name: "是它" })).toHaveLength(2);
    expect(within(question).queryByRole("button", { name: "不是" })).not.toBeInTheDocument();
  });

  it("后台还在看时每 2 秒再问一次", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      const renameCandidates = vi
        .fn()
        .mockResolvedValueOnce({ state: "checking", candidates: [], default_path: null })
        .mockResolvedValueOnce({
          state: "ready",
          candidates: [candidate("云图2026", 2, "里面 12 个子文件夹有 11 个对得上")],
          default_path: "/Volumes/资料盘/蓝鲸云/云图2026",
        });
      renderPage({ apiClient: client({ projectBoard: vi.fn().mockResolvedValue(missingBoard), renameCandidates }) });

      expect(await screen.findByText("找不到该目录")).toBeInTheDocument();
      // 「找不到该目录」可能先于第一次询问画出来，等询问真的发出去再推时间
      await waitFor(() => expect(renameCandidates).toHaveBeenCalledTimes(1));
      await act(async () => {
        await vi.advanceTimersByTimeAsync(2000);
      });
      await waitFor(() => expect(renameCandidates).toHaveBeenCalledTimes(2));
      expect(await screen.findByText(/是不是改名成了『云图2026』？（里面 12 个子文件夹有 11 个对得上）/)).toBeInTheDocument();
    } finally {
      vi.useRealTimers();
    }
  });
});

describe("ProjectDetailPage 文件名索引", () => {
  const indexRoot = (rootId: number, overrides: Partial<MaterialIndexRoot>): MaterialIndexRoot => ({
    root_id: rootId,
    project_id: "project-1",
    path: `/Volumes/资料盘/蓝鲸云/r${rootId}`,
    state: "done",
    files: 0,
    name_only_dirs: 0,
    indexed_once: true,
    last_full_at: null,
    updated_at: null,
    error: null,
    ...overrides,
  });
  const fiveRoots: ProjectBoard = {
    ...baseBoard,
    material_roots: [1, 2, 3, 4, 5].map((id) => ({
      id,
      project_id: "project-1",
      path: `/Volumes/资料盘/蓝鲸云/r${id}`,
      exists: true,
      created_at: "2026-09-01T00:00:00Z",
    })),
  };

  it("每个根目录一行：认好了、正在认、盘没插、找不到、读不了", async () => {
    const fiveMinutesAgo = new Date(Date.now() - 5 * 60_000 - 20_000).toISOString();
    const getMaterialIndexStatus = vi.fn().mockResolvedValue({
      roots: [
        indexRoot(1, { state: "done", files: 1234, name_only_dirs: 3, updated_at: fiveMinutesAgo }),
        indexRoot(2, { state: "offline", files: 40 }),
        indexRoot(3, { state: "missing", files: 12 }),
        indexRoot(4, { state: "error", error: "没有读取权限" }),
        indexRoot(5, { state: "done", files: 8, updated_at: fiveMinutesAgo }),
      ],
    });
    renderPage({ apiClient: client({ projectBoard: vi.fn().mockResolvedValue(fiveRoots), getMaterialIndexStatus }) });

    expect(
      await screen.findByText("已认得 1,234 个文件名 · 5 分钟前（node_modules、.git 等 3 个文件夹只记了个数）"),
    ).toBeInTheDocument();
    expect(getMaterialIndexStatus).toHaveBeenCalledWith("project-1");
    expect(screen.getByText("资料盘未连接，插上后接着认")).toBeInTheDocument();
    expect(screen.getByText("找不到这个文件夹，先按上次认得的算")).toHaveClass("is-stopped");
    expect(screen.getByText("读不了：没有读取权限")).toHaveClass("is-stopped");
    // name_only_dirs 为 0 时不写括号
    expect(screen.getByText("已认得 8 个文件名 · 5 分钟前")).toBeInTheDocument();
  });

  it("还在认时隔一会儿再问一次进度，认完就不再问", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      const getMaterialIndexStatus = vi
        .fn()
        .mockResolvedValueOnce({ roots: [indexRoot(1, { state: "walking", files: 820, indexed_once: false })] })
        .mockResolvedValue({ roots: [indexRoot(1, { state: "done", files: 1500, updated_at: new Date().toISOString() })] });
      renderPage({ apiClient: client({ getMaterialIndexStatus }) });

      expect(await screen.findByText("正在认文件名，已认 820 个")).toBeInTheDocument();
      await act(async () => {
        await vi.advanceTimersByTimeAsync(INDEX_POLL_MS);
      });
      expect(getMaterialIndexStatus).toHaveBeenCalledTimes(2);
      expect(await screen.findByText("已认得 1,500 个文件名 · 刚刚")).toBeInTheDocument();
      await act(async () => {
        await vi.advanceTimersByTimeAsync(INDEX_POLL_MS * 2);
      });
      expect(getMaterialIndexStatus).toHaveBeenCalledTimes(2);
    } finally {
      vi.useRealTimers();
    }
  });

  it("旧后端没有这个接口时不写这一行", async () => {
    renderPage();
    expect(await screen.findByText(baseBoard.material_roots![0].path)).toBeInTheDocument();
    expect(document.querySelector(".material-root-row__index")).toBeNull();
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

describe("ProjectDetailPage 材料内容进度（3e）", () => {
  const indexRoot = (overrides: Partial<MaterialIndexRoot> = {}): MaterialIndexRoot => ({
    root_id: 1,
    project_id: "project-1",
    path: baseBoard.material_roots![0].path,
    state: "done",
    files: 1500,
    name_only_dirs: 3,
    indexed_once: true,
    last_full_at: null,
    updated_at: new Date().toISOString(),
    error: null,
    ...overrides,
  });
  const coverageRoot = (content: Partial<MaterialCoverageRoot["content"]> = {}, online = true): MaterialCoverageRoot => ({
    root_id: 1,
    project_id: "project-1",
    path: baseBoard.material_roots![0].path,
    state: "done",
    online,
    names: { files: 1500, name_only_dirs: 3, symlinks: 2 },
    content: {
      total: 1237,
      done: 1020,
      pending: 210,
      paused: null,
      waiting: [],
      unreadable: { password: 2, corrupt: 3, unsupported: 2, timeout: 0, permission: 0 },
      notes: { small_image: 0, no_text: 0, no_speech: 0, truncated: 0, meeting_audio: 0 },
      names_only: { cards: 12, other: 30 },
      ...content,
    },
  });

  it("在读：已读多少、还剩多少，读不了分原因，只收文件名的写在括号里，「只记了个数」只写一次", async () => {
    const getMaterialIndexStatus = vi.fn().mockResolvedValue({ roots: [indexRoot()] });
    const getMaterialCoverage = vi.fn().mockResolvedValue({ roots: [coverageRoot()] });
    renderPage({ apiClient: client({ getMaterialIndexStatus, getMaterialCoverage }) });

    expect(
      await screen.findByText(/正文、图片文字、录音已读 1,020 个，还剩 210 个，读不了 7 个（要密码 2、文件损坏 3、格式不支持 2）/),
    ).toBeInTheDocument();
    expect(getMaterialCoverage).toHaveBeenCalledWith("project-1");
    expect(
      screen.getByText(
        "（声档会议记录 12 个、压缩包等 30 个只收文件名；node_modules、.git 等 3 个文件夹只记了个数；符号链接 2 个没跟进去）",
      ),
    ).toBeInTheDocument();
    // 文件名那行不再带括号
    expect(screen.getByText("已认得 1,500 个文件名 · 刚刚")).toBeInTheDocument();
    expect(screen.getAllByText(/只记了个数/)).toHaveLength(1);
  });

  it("转写会议时先停；读完了写「内容都读完了」；识别程序没装单独一句", async () => {
    const getMaterialIndexStatus = vi.fn().mockResolvedValue({ roots: [indexRoot()] });
    const getMaterialCoverage = vi.fn().mockResolvedValue({ roots: [coverageRoot({ paused: "busy" })] });
    const first = renderPage({ apiClient: client({ getMaterialIndexStatus, getMaterialCoverage }) });
    expect(await screen.findByText(/还剩 210 个 · 转写会议时先停，转写完接着读/)).toBeInTheDocument();
    first.unmount();

    const hint = "要先装 ffmpeg 才能转写录音：在终端运行 brew install ffmpeg";
    const done = vi.fn().mockResolvedValue({
      roots: [
        coverageRoot({
          pending: 0,
          unreadable: { password: 0, corrupt: 0, unsupported: 0, timeout: 0, permission: 0 },
          names_only: { cards: 0, other: 0 },
        }),
      ],
    });
    const second = renderPage({ apiClient: client({ getMaterialIndexStatus, getMaterialCoverage: done }) });
    expect(await screen.findByText("内容都读完了")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "看看" })).toBeNull();
    second.unmount();

    const waiting = vi.fn().mockResolvedValue({
      roots: [coverageRoot({ pending: 0, waiting: [{ what: "ffmpeg", files: 4, hint }] })],
    });
    renderPage({ apiClient: client({ getMaterialIndexStatus, getMaterialCoverage: waiting }) });
    expect(await screen.findByText(hint)).toBeInTheDocument();
    expect(screen.getByText(/已读 1,020 个，还剩 4 个/)).toBeInTheDocument();
  });

  it("［看看］展开读不了的文件，每行［复制路径］［预览］，没列完时［再列 100 个］", async () => {
    const user = userEvent.setup();
    const item = (id: number) => ({
      file_id: id,
      name: `报价${id}.xlsx`,
      rel_path: `报价/报价${id}.xlsx`,
      path: `${baseBoard.material_roots![0].path}/报价/报价${id}.xlsx`,
      root_id: 1,
      reason: "password" as const,
      checked_at: null,
    });
    const getMaterialUnreadable = vi
      .fn()
      .mockResolvedValueOnce({ items: [item(1), item(2)], total: 3, next_offset: 2 })
      .mockResolvedValueOnce({ items: [item(3)], total: 3, next_offset: null });
    const onOpenPreview = vi.fn();
    renderPage({
      apiClient: client({
        getMaterialIndexStatus: vi.fn().mockResolvedValue({ roots: [indexRoot()] }),
        getMaterialCoverage: vi.fn().mockResolvedValue({ roots: [coverageRoot({ pending: 0 })] }),
        getMaterialUnreadable,
      }),
      onOpenPreview,
    });

    await user.click(await screen.findByRole("button", { name: "看看" }));
    expect(getMaterialUnreadable).toHaveBeenCalledWith("project-1", 1, 0);
    expect(await screen.findByText("报价/报价1.xlsx")).toBeInTheDocument();
    const row = screen.getByText("报价/报价2.xlsx").closest("li")!;
    expect(within(row).getByText("要密码")).toBeInTheDocument();
    await user.click(within(row).getByRole("button", { name: "预览" }));
    expect(onOpenPreview).toHaveBeenCalledWith(2);
    expect(within(row).getByRole("button", { name: "复制路径" })).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "再列 100 个" }));
    expect(getMaterialUnreadable).toHaveBeenLastCalledWith("project-1", 1, 2);
    expect(await screen.findByText("报价/报价3.xlsx")).toBeInTheDocument();
    expect(screen.getByText("报价/报价1.xlsx")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "再列 100 个" })).toBeNull();

    await user.click(screen.getByRole("button", { name: "收起" }));
    expect(screen.queryByText("报价/报价1.xlsx")).toBeNull();
  });

  it("文件名认完了但内容还在读时接着问，两样一起问；读完就停；盘不在的根目录不算", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      const getMaterialIndexStatus = vi.fn().mockResolvedValue({ roots: [indexRoot()] });
      const getMaterialCoverage = vi
        .fn()
        .mockResolvedValueOnce({ roots: [coverageRoot()] })
        .mockResolvedValue({ roots: [coverageRoot({ pending: 0 })] });
      const view = renderPage({ apiClient: client({ getMaterialIndexStatus, getMaterialCoverage }) });
      expect(await screen.findByText(/还剩 210 个/)).toBeInTheDocument();
      await act(async () => {
        await vi.advanceTimersByTimeAsync(INDEX_POLL_MS);
      });
      expect(getMaterialIndexStatus).toHaveBeenCalledTimes(2);
      expect(getMaterialCoverage).toHaveBeenCalledTimes(2);
      expect(await screen.findByText(/内容都读完了/)).toBeInTheDocument();
      await act(async () => {
        await vi.advanceTimersByTimeAsync(INDEX_POLL_MS * 2);
      });
      expect(getMaterialCoverage).toHaveBeenCalledTimes(2);
      view.unmount();

      const offline = vi.fn().mockResolvedValue({ roots: [coverageRoot({}, false)] });
      renderPage({ apiClient: client({ getMaterialIndexStatus, getMaterialCoverage: offline }) });
      expect(await screen.findByText(/还剩 210 个/)).toBeInTheDocument();
      await act(async () => {
        await vi.advanceTimersByTimeAsync(INDEX_POLL_MS * 2);
      });
      expect(offline).toHaveBeenCalledTimes(1);
    } finally {
      vi.useRealTimers();
    }
  });

  it("coverage 出错时照旧写文件名那行（带「只记了个数」）", async () => {
    renderPage({
      apiClient: client({
        getMaterialIndexStatus: vi.fn().mockResolvedValue({ roots: [indexRoot()] }),
        getMaterialCoverage: vi.fn().mockRejectedValue(new Error("boom")),
      }),
    });
    expect(
      await screen.findByText("已认得 1,500 个文件名 · 刚刚（node_modules、.git 等 3 个文件夹只记了个数）"),
    ).toBeInTheDocument();
    expect(document.querySelector(".material-root-row__content")).toBeNull();
  });
});

// ---------------------------------------------------------------- 4c：时间线

function timelinePayload(overrides: Partial<ProjectTimelinePayload> = {}): ProjectTimelinePayload {
  return {
    kind: "all",
    days: [
      {
        day: "2026-09-27",
        label: "今天",
        items: [
          { type: "meeting", at: "2026-09-27T06:30:00+00:00", time: "14:30",
            meeting: { id: "m1", title: "初审规则沟通", duration_sec: 2880, audio_url: "/api/media/412" },
            decisions: [{ id: "dec-1", text: "阈值先按 0.8 执行", start_ms: 754_000, later: { date: "2026-09-28", text: "阈值改成 0.7" } }],
            decisions_more: 5 },
          { type: "tasks", event: "confirmed", at: null, time: "15:02", tasks: [{ id: "t1", title: "写一版方案" }], more: 0 },
          { type: "tasks", event: "done", at: null, time: "15:30",
            tasks: [{ id: "t2", title: "甲" }, { id: "t3", title: "乙" }, { id: "t4", title: "丙" }], more: 1 },
          { type: "deliverable", at: null, time: "16:10", task: { id: "t2", title: "整理报价单" },
            deliverable: { id: 5, name: "报价单_v3.xlsx" } },
          { type: "files", at: null, time: "17:45", root_id: 1, folder: "能耗看板", added: 5, changed: 2,
            names: ["报价单_v3.xlsx", "排期表.xlsx"], prelog: false },
          { type: "files", at: null, time: "18:00", root_id: 1, folder: "", added: 2, changed: 0,
            names: ["a.docx", "b.docx"], prelog: false },
        ],
        more_dirs: 4,
      },
    ],
    requirements: [],
    next_before: "2026-09-27",
    file_log_since: "2026-09-27",
    state: { kind: "ok", reason: null, text: null, action: null },
    ...overrides,
  };
}

const LINKS_ON = { linksEnabled: true, semanticEnabled: true, llmConfigured: true };

function renderTimeline(api: Partial<ApiClient>, props: Partial<Parameters<typeof ProjectDetailPage>[0]> = {}) {
  return render(
    <LinksFlagsContext.Provider value={LINKS_ON}>
      <ProjectDetailPage
        apiClient={client({ meetingQuotes: vi.fn().mockResolvedValue({ quotes: [] }), ...api } as Partial<ApiClient>)}
        canPickFolders
        canWrite
        onBack={vi.fn()}
        onOpenGlossary={vi.fn()}
        onOpenMeeting={vi.fn()}
        onOpenRequirement={vi.fn()}
        onOpenTask={vi.fn()}
        projectId="project-1"
        projects={[]}
        {...props}
      />
    </LinksFlagsContext.Provider>,
  );
}

describe("ProjectDetailPage 的时间线（4c）", () => {
  it("在「AI 自动建的项目」提示之后、「材料根目录」之前，条目的字照规格", async () => {
    const projectTimeline = vi.fn().mockResolvedValue(timelinePayload());
    renderTimeline({
      projectTimeline,
      projectBoard: vi.fn().mockResolvedValue({ ...baseBoard, origin: "ai", material_roots: [] }),
    } as Partial<ApiClient>);

    const card = await screen.findByRole("region", { name: "时间线" });
    const hint = await screen.findByText(/这个项目是 AI 自动建的/);
    const roots = await screen.findByRole("heading", { name: "材料根目录" });
    expect(hint.compareDocumentPosition(card) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(card.compareDocumentPosition(roots) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    await waitFor(() => expect(projectTimeline).toHaveBeenCalledWith("project-1", { kind: "all", days: 7 }));

    const inCard = within(card);
    expect(await inCard.findByText("今天")).toBeInTheDocument();
    expect(inCard.getByRole("button", { name: "14:30 会议『初审规则沟通』· 48 分钟" })).toBeInTheDocument();
    expect(inCard.getByText("定了：阈值先按 0.8 执行 · 9月28日后来改了")).toBeInTheDocument();
    expect(inCard.getByRole("button", { name: "还有 5 条" })).toBeInTheDocument();
    expect(inCard.getByText("15:02 确认了任务：写一版方案")).toBeInTheDocument();
    expect(inCard.getByText("15:30 完成了 4 条任务：甲、乙、丙、…")).toBeInTheDocument();
    expect(inCard.getByText("16:10 『整理报价单』的产出：报价单_v3.xlsx")).toBeInTheDocument();
    expect(inCard.getByText("17:45 『能耗看板』里新增 5 个、改了 2 个：报价单_v3.xlsx、排期表.xlsx 等")).toBeInTheDocument();
    expect(inCard.getByText("18:00 项目文件夹里新增 2 个：a.docx、b.docx")).toBeInTheDocument();
    expect(inCard.getByText("另有 4 个文件夹有变化")).toBeInTheDocument();
  });

  it("筛选、［更早］和跨过记录起点的页脚", async () => {
    const projectTimeline = vi
      .fn()
      .mockResolvedValueOnce(timelinePayload())
      .mockResolvedValueOnce(timelinePayload({
        days: [{ day: "2026-09-20", label: "9月20日 周日", more_dirs: 0, items: [
          { type: "files", at: null, time: null, root_id: 1, folder: "能耗看板", added: 0, changed: 0, count: 3,
            names: ["方案.docx", "清单.xlsx", "排期.xlsx"], prelog: true },
        ] }],
        next_before: null,
      }))
      .mockResolvedValue(timelinePayload({ kind: "decisions", days: [], next_before: null }));
    renderTimeline({ projectTimeline } as Partial<ApiClient>);

    const card = await screen.findByRole("region", { name: "时间线" });
    await within(card).findByText("今天");
    expect(within(card).queryByText(/起记录/)).not.toBeInTheDocument();
    await userEvent.click(within(card).getByRole("button", { name: "更早" }));
    expect(projectTimeline).toHaveBeenLastCalledWith("project-1", { kind: "all", days: 7, before: "2026-09-27" });
    expect(await within(card).findByText("『能耗看板』里 3 个文件最后一次修改在这天：方案.docx、清单.xlsx、排期.xlsx")).toBeInTheDocument();
    expect(within(card).getByText("文件从 9月27日 起记录")).toBeInTheDocument();
    expect(within(card).queryByRole("button", { name: "更早" })).not.toBeInTheDocument();

    await userEvent.click(within(card).getByRole("button", { name: "决议" }));
    expect(projectTimeline).toHaveBeenLastCalledWith("project-1", { kind: "decisions", days: 7 });
    expect(await within(card).findByText("这个项目的会还没有列出决议")).toBeInTheDocument();
  });

  it("三种状态：没挂根目录时［挂上文件夹］走现有流程；在等时 15 秒重取", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      const noRoots = timelinePayload({
        state: { kind: "stopped", reason: "no_roots", text: "这个项目还没挂材料文件夹，时间线里只有会议和任务",
                 action: { kind: "attach_root", label: "挂上文件夹" } },
      });
      const waiting = timelinePayload({
        state: { kind: "waiting", reason: "offline", text: "资料盘未连接，插上后接着记文件的变化", action: null },
      });
      const projectTimeline = vi.fn().mockResolvedValueOnce(waiting).mockResolvedValue(noRoots);
      renderTimeline({ projectTimeline } as Partial<ApiClient>);
      const card = await screen.findByRole("region", { name: "时间线" });
      expect(await within(card).findByText("资料盘未连接，插上后接着记文件的变化")).toBeInTheDocument();
      await act(async () => {
        vi.advanceTimersByTime(15_100);
      });
      expect(await within(card).findByText("这个项目还没挂材料文件夹，时间线里只有会议和任务")).toBeInTheDocument();
      expect(projectTimeline).toHaveBeenCalledTimes(2);
      expect(within(card).getByRole("button", { name: "挂上文件夹" })).toBeInTheDocument();
    } finally {
      vi.useRealTimers();
    }
  });

  it("手机上（不能挑文件夹）没有［挂上文件夹］", async () => {
    const projectTimeline = vi.fn().mockResolvedValue(timelinePayload({
      state: { kind: "stopped", reason: "no_roots", text: "这个项目还没挂材料文件夹，时间线里只有会议和任务",
               action: { kind: "attach_root", label: "挂上文件夹" } },
    }));
    renderTimeline({ projectTimeline } as Partial<ApiClient>, { canPickFolders: false });
    const card = await screen.findByRole("region", { name: "时间线" });
    await within(card).findByText("这个项目还没挂材料文件夹，时间线里只有会议和任务");
    expect(within(card).queryByRole("button", { name: "挂上文件夹" })).not.toBeInTheDocument();
  });

  it("［决议］：［放到需求 ▾］先列这场会关联的需求，打开会议带时间", async () => {
    const decisions = timelinePayload({
      kind: "decisions",
      requirements: [{ id: "r1", title: "初审规则 V2" }, { id: "r2", title: "驻场排班" }],
      days: [{ day: "2026-09-27", label: "今天", more_dirs: 0, items: [
        { type: "decision", at: null, time: "14:30",
          decision: { id: "dec-u", text: "下周起统一口径", start_ms: 60_000, later: null },
          meeting: { id: "m1", title: "初审规则沟通", audio_url: null },
          requirement: null, how: "unplaced", linked_requirement_ids: ["r2"] },
        { type: "decision", at: null, time: "14:30",
          decision: { id: "dec-p", text: "阈值先按 0.8 执行", start_ms: 754_000, later: null },
          meeting: { id: "m1", title: "初审规则沟通", audio_url: null },
          requirement: { id: "r1", title: "初审规则 V2", how: "title" }, how: "title", linked_requirement_ids: ["r1", "r2"] },
      ] }],
    });
    const projectTimeline = vi.fn().mockResolvedValueOnce(timelinePayload()).mockResolvedValue(decisions);
    const placeDecision = vi.fn().mockResolvedValue({ decision: {}, undo: { placement: null, requirement_id: null }, undo_until: "2099-01-01T00:00:00Z" });
    const onOpenMeeting = vi.fn();
    renderTimeline({ projectTimeline, placeDecision } as Partial<ApiClient>, { onOpenMeeting });

    const card = await screen.findByRole("region", { name: "时间线" });
    await within(card).findByText("今天");
    await userEvent.click(within(card).getByRole("button", { name: "决议" }));
    expect(await within(card).findByText("初审规则 V2")).toBeInTheDocument();
    expect(within(card).getByText(/没归到具体需求/)).toBeInTheDocument();
    await userEvent.click(within(card).getByRole("button", { name: "放到需求 ▾" }));
    const options = within(card).getAllByRole("menuitem").map((item) => item.textContent);
    expect(options).toEqual(["驻场排班", "初审规则 V2"]);
    await userEvent.click(within(card).getByRole("menuitem", { name: "驻场排班" }));
    expect(placeDecision).toHaveBeenCalledWith("dec-u", { placement: "picked", requirement_id: "r2" });
    expect(await within(card).findByText("已放到『驻场排班』")).toBeInTheDocument();

    await userEvent.click(within(card).getAllByRole("button", { name: "打开会议" })[1]);
    expect(onOpenMeeting).toHaveBeenCalledWith("m1", 754_000);
  });

  it("在等时 15 秒静默重取只换第一页那几天，［更早］翻出来的天和翻页起点留着", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      const waiting = { kind: "waiting" as const, reason: "offline" as const, text: "资料盘未连接，插上后接着记文件的变化", action: null };
      const older = timelinePayload({
        state: waiting,
        days: [{ day: "2026-09-20", label: "9月20日 周日", more_dirs: 0, items: [
          { type: "tasks", event: "confirmed", at: null, time: "09:00", tasks: [{ id: "t9", title: "旧任务" }], more: 0 },
        ] }],
        next_before: "2026-09-13",
      });
      const refreshed = timelinePayload({
        state: waiting,
        days: [{ day: "2026-09-27", label: "今天", more_dirs: 0, items: [
          { type: "tasks", event: "confirmed", at: null, time: "19:00", tasks: [{ id: "t8", title: "新任务" }], more: 0 },
        ] }],
      });
      const projectTimeline = vi
        .fn()
        .mockResolvedValueOnce(timelinePayload({ state: waiting }))
        .mockResolvedValueOnce(older)
        .mockResolvedValue(refreshed);
      renderTimeline({ projectTimeline } as Partial<ApiClient>);
      const card = await screen.findByRole("region", { name: "时间线" });
      await within(card).findByText("今天");
      fireEvent.click(within(card).getByRole("button", { name: "更早" }));
      expect(await within(card).findByText("09:00 确认了任务：旧任务")).toBeInTheDocument();

      await act(async () => {
        vi.advanceTimersByTime(15_100);
      });
      expect(await within(card).findByText("19:00 确认了任务：新任务")).toBeInTheDocument();
      // 第一页那天换成了新的，更早那天还在
      expect(within(card).queryByText("15:02 确认了任务：写一版方案")).not.toBeInTheDocument();
      expect(within(card).getByText("09:00 确认了任务：旧任务")).toBeInTheDocument();
      // 翻页起点还是翻出来那一页给的
      fireEvent.click(within(card).getByRole("button", { name: "更早" }));
      await waitFor(() =>
        expect(projectTimeline).toHaveBeenLastCalledWith("project-1", { kind: "all", days: 7, before: "2026-09-13" }),
      );
    } finally {
      vi.useRealTimers();
    }
  });

  it("一页的天全被滤掉时自动往前取，不写「还没有…」", async () => {
    const empty = timelinePayload({ days: [], next_before: "2026-09-20" });
    const projectTimeline = vi
      .fn()
      .mockResolvedValueOnce(empty)
      .mockResolvedValueOnce(timelinePayload({
        days: [{ day: "2026-09-18", label: "9月18日 周五", more_dirs: 0, items: [
          { type: "tasks", event: "done", at: null, time: "10:00", tasks: [{ id: "t1", title: "写一版方案" }], more: 0 },
        ] }],
        next_before: null,
      }));
    renderTimeline({ projectTimeline } as Partial<ApiClient>);
    const card = await screen.findByRole("region", { name: "时间线" });
    expect(await within(card).findByText("10:00 完成了任务：写一版方案")).toBeInTheDocument();
    expect(projectTimeline).toHaveBeenLastCalledWith("project-1", { kind: "all", days: 7, before: "2026-09-20" });
    expect(within(card).queryByText("这个项目还没有会议、任务和文件的变化")).not.toBeInTheDocument();
  });

  it("往前取了几页仍是空的：只留［更早］，不写「还没有…」", async () => {
    const projectTimeline = vi.fn().mockResolvedValue(timelinePayload({ days: [], next_before: "2026-09-01" }));
    renderTimeline({ projectTimeline } as Partial<ApiClient>);
    const card = await screen.findByRole("region", { name: "时间线" });
    expect(await within(card).findByRole("button", { name: "更早" })).toBeInTheDocument();
    expect(projectTimeline).toHaveBeenCalledTimes(4);
    expect(within(card).queryByText("这个项目还没有会议、任务和文件的变化")).not.toBeInTheDocument();
  });

  it("［更早］还在路上时换了筛选：回来的旧页丢掉", async () => {
    let resolveMore: (payload: ProjectTimelinePayload) => void = () => undefined;
    const pending = new Promise<ProjectTimelinePayload>((resolve) => {
      resolveMore = resolve;
    });
    const projectTimeline = vi
      .fn()
      .mockResolvedValueOnce(timelinePayload())
      .mockReturnValueOnce(pending)
      .mockResolvedValue(timelinePayload({ kind: "tasks", days: [], next_before: null }));
    renderTimeline({ projectTimeline } as Partial<ApiClient>);
    const card = await screen.findByRole("region", { name: "时间线" });
    await within(card).findByText("今天");
    await userEvent.click(within(card).getByRole("button", { name: "更早" }));
    await userEvent.click(within(card).getByRole("button", { name: "任务" }));
    expect(await within(card).findByText("还没有确认或完成的任务")).toBeInTheDocument();
    await act(async () => {
      resolveMore(timelinePayload({
        days: [{ day: "2026-09-20", label: "9月20日 周日", more_dirs: 0, items: [
          { type: "tasks", event: "confirmed", at: null, time: "09:00", tasks: [{ id: "t9", title: "旧筛选的" }], more: 0 },
        ] }],
        next_before: "2026-09-13",
      }));
    });
    expect(within(card).queryByText(/旧筛选的/)).not.toBeInTheDocument();
    expect(within(card).getByText("还没有确认或完成的任务")).toBeInTheDocument();
    expect(within(card).queryByRole("button", { name: "更早" })).not.toBeInTheDocument();
  });

  it("［决议］：你说不属于需求的（none）和会没关联需求的（project）都写「没归到具体需求」", async () => {
    const decisionItem = (id: string, how: "none" | "project") => ({
      type: "decision" as const, at: null, time: "14:30",
      decision: { id, text: `决议${id}`, start_ms: null, later: null },
      meeting: { id: "m1", title: "初审规则沟通", audio_url: null },
      requirement: null, how, linked_requirement_ids: [],
    });
    const projectTimeline = vi.fn().mockResolvedValueOnce(timelinePayload()).mockResolvedValue(timelinePayload({
      kind: "decisions",
      requirements: [{ id: "r1", title: "初审规则 V2" }],
      days: [{ day: "2026-09-27", label: "今天", more_dirs: 0, items: [decisionItem("dec-n", "none"), decisionItem("dec-p", "project")] }],
    }));
    renderTimeline({ projectTimeline, placeDecision: vi.fn() } as Partial<ApiClient>);
    const card = await screen.findByRole("region", { name: "时间线" });
    await within(card).findByText("今天");
    await userEvent.click(within(card).getByRole("button", { name: "决议" }));
    await within(card).findByText("决议dec-n");
    for (const text of ["决议dec-n", "决议dec-p"]) {
      const row = within(card).getByText(text).closest(".decision-row") as HTMLElement;
      expect(within(row).getByText(/没归到具体需求/)).toBeInTheDocument();
      expect(within(row).getByRole("button", { name: "放到需求 ▾" })).toBeInTheDocument();
    }
    expect(within(card).queryByText("初审规则 V2")).not.toBeInTheDocument();
  });

  it("「还有 N 条」打开纪要；旧后台没有方法时不画时间线", async () => {
    const onOpenMeeting = vi.fn();
    renderTimeline({ projectTimeline: vi.fn().mockResolvedValue(timelinePayload()) } as Partial<ApiClient>, { onOpenMeeting });
    const card = await screen.findByRole("region", { name: "时间线" });
    await userEvent.click(await within(card).findByRole("button", { name: "还有 5 条" }));
    expect(onOpenMeeting).toHaveBeenCalledWith("m1", undefined, "minutes");
  });

  it("旧后台：没有 projectTimeline 或接口 404 时不画", async () => {
    const { unmount } = renderTimeline({});
    await screen.findByRole("heading", { name: "材料根目录" });
    expect(screen.queryByRole("region", { name: "时间线" })).not.toBeInTheDocument();
    unmount();
    const projectTimeline = vi.fn().mockRejectedValue(new ApiError("Not Found", 404, { detail: "Not Found" }));
    renderTimeline({ projectTimeline } as Partial<ApiClient>);
    await screen.findByRole("heading", { name: "材料根目录" });
    await waitFor(() => expect(projectTimeline).toHaveBeenCalled());
    await waitFor(() => expect(screen.queryByRole("region", { name: "时间线" })).not.toBeInTheDocument());
  });
});

describe("ProjectDetailPage 的问答卡（4g）", () => {
  it("有 askPrepare 时「问这个项目」卡在时间线上面；材料出处交给 onOpenPreviewTarget", async () => {
    const projectTimeline = vi.fn().mockResolvedValue(timelinePayload());
    const askPrepare = vi.fn();
    renderTimeline({ projectTimeline, askPrepare, ask: vi.fn(), askJob: vi.fn() } as Partial<ApiClient>, {
      onOpenPreviewTarget: vi.fn(),
      isMobile: true,
    });
    const timeline = await screen.findByRole("region", { name: "时间线" });
    const card = screen.getByRole("region", { name: "问这个项目" });
    expect(card.compareDocumentPosition(timeline) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(within(card).getByText("只在『云图科研用药』的会和材料里找；要把材料原文发出去时会先告诉你")).toBeInTheDocument();
    expect(askPrepare).not.toHaveBeenCalled();
  });

  it("没有 askPrepare（旧后台）时不画问答卡", async () => {
    renderTimeline({ projectTimeline: vi.fn().mockResolvedValue(timelinePayload()) } as Partial<ApiClient>);
    await screen.findByRole("region", { name: "时间线" });
    expect(screen.queryByRole("region", { name: "问这个项目" })).toBeNull();
  });
});
