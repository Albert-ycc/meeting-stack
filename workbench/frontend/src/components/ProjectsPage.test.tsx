import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { ProjectsPage } from "./ProjectsPage";
import type { ApiClient } from "../api";
import type { Project } from "../types";

function makeClient(overrides: Partial<ApiClient> = {}) {
  return {
    createProject: vi.fn().mockResolvedValue({ id: "new", name: "互联网医院", color: "#3f51b5" }),
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

const PROJECTS: Project[] = [
  {
    id: "p1",
    name: "云图科研用药",
    color: "#2c8d83",
    meeting_count: 33,
    requirement_counts: { active: 6, done: 5, shelved: 1, all: 12 },
    open_task_count: 17,
    material_roots: [
      { id: 1, project_id: "p1", path: "/Volumes/资料盘/蓝鲸云/云图科研用药", exists: true, created_at: "2026-09-01T00:00:00Z" },
    ],
  },
  {
    id: "p2",
    name: "Keep",
    color: "#3ecf8e",
    meeting_count: 0,
    requirement_counts: { active: 0, done: 0, shelved: 0, all: 0 },
    open_task_count: 0,
    material_roots: [],
  },
];

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

function tableRows() {
  return screen.getAllByRole("row").slice(1); // 去掉表头行
}

describe("ProjectsPage 表格与查询", () => {
  it("按会议数倒序排列", () => {
    renderPage();
    const rows = tableRows();
    expect(within(rows[0]).getByText("云图科研用药")).toBeInTheDocument();
    expect(within(rows[1]).getByText("Keep")).toBeInTheDocument();
  });

  it("材料根目录列：有挂显示路径，没挂显示未挂", () => {
    renderPage();
    expect(screen.getByRole("cell", { name: "/Volumes/资料盘/蓝鲸云/云图科研用药" })).toBeInTheDocument();
    expect(screen.getByRole("cell", { name: "未挂" })).toBeInTheDocument();
  });

  it("按项目名称查询过滤表格", async () => {
    renderPage();
    await userEvent.type(screen.getByPlaceholderText("输入项目名称"), "Keep");
    await userEvent.click(screen.getByRole("button", { name: "查询" }));

    const rows = tableRows();
    expect(rows).toHaveLength(1);
    expect(within(rows[0]).getByText("Keep")).toBeInTheDocument();
  });

  it("按材料根目录已挂/未挂筛选", async () => {
    renderPage();
    await userEvent.selectOptions(screen.getByLabelText("材料根目录"), "unattached");
    await userEvent.click(screen.getByRole("button", { name: "查询" }));

    const rows = tableRows();
    expect(rows).toHaveLength(1);
    expect(within(rows[0]).getByText("Keep")).toBeInTheDocument();
  });

  it("重置清空查询条件", async () => {
    renderPage();
    await userEvent.type(screen.getByPlaceholderText("输入项目名称"), "Keep");
    await userEvent.click(screen.getByRole("button", { name: "查询" }));
    expect(tableRows()).toHaveLength(1);

    await userEvent.click(screen.getByRole("button", { name: "重置" }));
    expect(tableRows()).toHaveLength(2);
    expect(screen.getByPlaceholderText("输入项目名称")).toHaveValue("");
  });

  it("会议数取服务端字段，不受 meetings 分页影响", () => {
    renderPage({ meetings: [] });
    const rows = tableRows();
    expect(within(rows[0]).getByRole("cell", { name: "33" })).toBeInTheDocument();
  });

  it("点项目名称或查看都会打开项目详情", async () => {
    const onOpenProject = vi.fn();
    renderPage({ onOpenProject });
    await userEvent.click(screen.getByRole("button", { name: /云图科研用药/ }));
    expect(onOpenProject).toHaveBeenCalledWith("p1");

    onOpenProject.mockClear();
    const rows = tableRows();
    await userEvent.click(within(rows[1]).getByRole("button", { name: "查看" }));
    expect(onOpenProject).toHaveBeenCalledWith("p2");
  });
});

describe("ProjectsPage 新建 / 编辑项目", () => {
  it("新建项目成功后关闭弹窗、通知父级刷新，并直接进入新项目详情", async () => {
    const onProjectsChanged = vi.fn();
    const onOpenProject = vi.fn();
    const createProject = vi.fn().mockResolvedValue({ id: "new", name: "互联网医院", color: "#3f51b5" });
    renderPage({ apiClient: makeClient({ createProject } as Partial<ApiClient>), onOpenProject, onProjectsChanged });

    await userEvent.click(screen.getByRole("button", { name: "＋ 新建项目" }));
    await userEvent.type(screen.getByPlaceholderText("例如：互联网医院"), "互联网医院");
    await userEvent.click(screen.getByRole("button", { name: "创建" }));

    expect(createProject).toHaveBeenCalledWith("互联网医院", expect.stringMatching(/^#/), undefined);
    await waitFor(() => {
      expect(screen.queryByRole("dialog", { name: "新建项目" })).not.toBeInTheDocument();
    });
    expect(onProjectsChanged).toHaveBeenCalled();
    expect(onOpenProject).toHaveBeenCalledWith("new");
  });

  it("编辑项目保存后留在列表原地刷新，不跳转详情", async () => {
    const onProjectsChanged = vi.fn();
    const onOpenProject = vi.fn();
    const updateProject = vi.fn().mockResolvedValue({ id: "p1", name: "云图科研用药", color: "#2c8d83" });
    renderPage({ apiClient: makeClient({ updateProject } as Partial<ApiClient>), onOpenProject, onProjectsChanged });

    const rows = tableRows();
    await userEvent.click(within(rows[0]).getByRole("button", { name: "编辑" }));
    await userEvent.click(screen.getByRole("button", { name: "保存" }));

    await waitFor(() => {
      expect(screen.queryByRole("dialog", { name: "编辑项目" })).not.toBeInTheDocument();
    });
    expect(onProjectsChanged).toHaveBeenCalled();
    expect(onOpenProject).not.toHaveBeenCalled();
  });

  it("编辑项目预填当前名称与颜色", async () => {
    renderPage();
    const rows = tableRows();
    await userEvent.click(within(rows[0]).getByRole("button", { name: "编辑" }));

    expect(screen.getByRole("dialog", { name: "编辑项目" })).toBeInTheDocument();
    expect(screen.getByPlaceholderText("例如：互联网医院")).toHaveValue("云图科研用药");
  });
});

describe("ProjectsPage 移动端只读", () => {
  it("canEdit 为 false 时不显示新建项目、编辑与新建标签", () => {
    renderPage({ canEdit: false });

    expect(screen.queryByRole("button", { name: "＋ 新建项目" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "编辑" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "新建标签" })).not.toBeInTheDocument();
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
