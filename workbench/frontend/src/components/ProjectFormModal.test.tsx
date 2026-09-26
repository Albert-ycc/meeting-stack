import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { ProjectFormModal } from "./ProjectFormModal";
import { ApiError } from "../api";
import type { ApiClient } from "../api";
import type { FolderMatchesPayload, MaterialBrowsePayload, Project } from "../types";

function makeClient(overrides: Partial<ApiClient> = {}) {
  return {
    createProjectWith: vi.fn().mockResolvedValue({ id: "p1", name: "互联网医院", color: "#3f51b5" }),
    updateProject: vi.fn().mockResolvedValue({ id: "p1", name: "云图科研用药", color: "#2c8d83" }),
    browseMaterials: vi.fn().mockResolvedValue({
      base: "/Volumes/资料盘",
      path: "/Volumes/资料盘",
      parent: null,
      breadcrumbs: [{ name: "资料盘", path: "/Volumes/资料盘" }],
      dirs: [{ name: "蓝鲸云", path: "/Volumes/资料盘/蓝鲸云" }],
    } satisfies MaterialBrowsePayload),
    ...overrides,
  } as unknown as ApiClient;
}

const EXISTING_PROJECT: Project = {
  id: "p1",
  name: "云图科研用药",
  color: "#2c8d83",
  material_roots: [
    { id: 1, project_id: "p1", path: "/Volumes/资料盘/蓝鲸云/云图科研用药", exists: true, created_at: "2026-09-01T00:00:00Z" },
  ],
};

describe("ProjectFormModal 新建", () => {
  it("填名称、选颜色后创建，不带材料根目录时不传该字段", async () => {
    const createProjectWith = vi.fn().mockResolvedValue({ id: "p2", name: "互联网医院", color: "#5090ff" });
    const onSaved = vi.fn();
    const onClose = vi.fn();
    render(
      <ProjectFormModal
        apiClient={makeClient({ createProjectWith } as Partial<ApiClient>)}
        canPickFolders
        mode="create"
        onClose={onClose}
        onSaved={onSaved}
      />,
    );

    await userEvent.type(screen.getByPlaceholderText("例如：互联网医院"), "互联网医院");
    await userEvent.click(screen.getByRole("button", { name: "选择颜色 #5090ff" }));
    await userEvent.click(screen.getByRole("button", { name: "创建" }));

    expect(createProjectWith).toHaveBeenCalledWith({ name: "互联网医院", color: "#5090ff" });
    expect(onSaved).toHaveBeenCalledWith({ id: "p2", name: "互联网医院", color: "#5090ff" });
    expect(onClose).toHaveBeenCalled();
  });

  it("添加材料根目录后创建时带上路径列表", async () => {
    const createProjectWith = vi.fn().mockResolvedValue({ id: "p3", name: "新项目", color: "#3f51b5" });
    render(
      <ProjectFormModal
        apiClient={makeClient({ createProjectWith } as Partial<ApiClient>)}
        canPickFolders
        mode="create"
        onClose={vi.fn()}
        onSaved={vi.fn()}
      />,
    );

    await userEvent.type(screen.getByPlaceholderText("例如：互联网医院"), "新项目");
    await userEvent.click(screen.getByRole("button", { name: "＋ 添加目录" }));
    await userEvent.click(await screen.findByText("蓝鲸云"));
    await userEvent.click(screen.getByRole("button", { name: "确定" }));

    expect(screen.getByText("/Volumes/资料盘/蓝鲸云")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "创建" }));

    expect(createProjectWith).toHaveBeenCalledWith({
      name: "新项目",
      color: "#3f51b5",
      material_roots: ["/Volumes/资料盘/蓝鲸云"],
    });
  });

  it("保存失败时显示错误原因，不关闭弹窗", async () => {
    const createProjectWith = vi.fn().mockRejectedValue(new Error("项目名称已存在"));
    const onClose = vi.fn();
    render(
      <ProjectFormModal
        apiClient={makeClient({ createProjectWith } as Partial<ApiClient>)}
        canPickFolders
        mode="create"
        onClose={onClose}
        onSaved={vi.fn()}
      />,
    );

    await userEvent.type(screen.getByPlaceholderText("例如：互联网医院"), "互联网医院");
    await userEvent.click(screen.getByRole("button", { name: "创建" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("项目名称已存在");
    expect(onClose).not.toHaveBeenCalled();
  });

  it("D26：新建撞同名时 409 就地显示，已填名称/颜色/根目录原样保留，弹窗不关", async () => {
    const createProjectWith = vi.fn().mockRejectedValue(new Error("已有同名项目"));
    const onClose = vi.fn();
    const onSaved = vi.fn();
    render(
      <ProjectFormModal
        apiClient={makeClient({ createProjectWith } as Partial<ApiClient>)}
        canPickFolders
        mode="create"
        onClose={onClose}
        onSaved={onSaved}
      />,
    );

    await userEvent.type(screen.getByPlaceholderText("例如：互联网医院"), "云图科研用药");
    await userEvent.click(screen.getByRole("button", { name: "选择颜色 #2c8d83" }));
    await userEvent.click(screen.getByRole("button", { name: "＋ 添加目录" }));
    await userEvent.click(await screen.findByText("蓝鲸云"));
    await userEvent.click(screen.getByRole("button", { name: "确定" }));
    await userEvent.click(screen.getByRole("button", { name: "创建" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("已有同名项目");
    expect(onClose).not.toHaveBeenCalled();
    expect(onSaved).not.toHaveBeenCalled();
    // 已填内容原样保留，不用重填
    expect(screen.getByPlaceholderText("例如：互联网医院")).toHaveValue("云图科研用药");
    expect(screen.getByRole("button", { name: "选择颜色 #2c8d83" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByText("/Volumes/资料盘/蓝鲸云")).toBeInTheDocument();
  });

  it("D26：编辑改名撞同名时 409 就地显示，弹窗不关", async () => {
    const updateProject = vi.fn().mockRejectedValue(new Error("已有同名项目"));
    const onClose = vi.fn();
    render(
      <ProjectFormModal
        apiClient={makeClient({ updateProject } as Partial<ApiClient>)}
        canPickFolders
        mode="edit"
        onClose={onClose}
        onSaved={vi.fn()}
        project={EXISTING_PROJECT}
      />,
    );

    await userEvent.clear(screen.getByPlaceholderText("例如：互联网医院"));
    await userEvent.type(screen.getByPlaceholderText("例如：互联网医院"), "ACME");
    await userEvent.click(screen.getByRole("button", { name: "保存" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("已有同名项目");
    expect(onClose).not.toHaveBeenCalled();
    expect(screen.getByPlaceholderText("例如：互联网医院")).toHaveValue("ACME");
  });

  it("点取消关闭弹窗", async () => {
    const onClose = vi.fn();
    render(<ProjectFormModal apiClient={makeClient()} canPickFolders mode="create" onClose={onClose} onSaved={vi.fn()} />);
    await userEvent.click(screen.getByRole("button", { name: "取消" }));
    expect(onClose).toHaveBeenCalled();
  });
});

describe("ProjectFormModal 编辑", () => {
  it("预填已有信息，移除根目录后保存整体替换为空列表", async () => {
    const updateProject = vi.fn().mockResolvedValue({ ...EXISTING_PROJECT });
    render(
      <ProjectFormModal
        apiClient={makeClient({ updateProject } as Partial<ApiClient>)}
        canPickFolders
        mode="edit"
        onClose={vi.fn()}
        onSaved={vi.fn()}
        project={EXISTING_PROJECT}
      />,
    );

    expect(screen.getByPlaceholderText("例如：互联网医院")).toHaveValue("云图科研用药");
    expect(screen.getByRole("button", { name: "选择颜色 #2c8d83" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByText("/Volumes/资料盘/蓝鲸云/云图科研用药")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "移除 /Volumes/资料盘/蓝鲸云/云图科研用药" }));
    await userEvent.click(screen.getByRole("button", { name: "保存" }));

    expect(updateProject).toHaveBeenCalledWith("p1", { name: "云图科研用药", color: "#2c8d83", material_roots: [] });
  });

  it("只改名称和颜色时不提交根目录", async () => {
    const updateProject = vi.fn().mockResolvedValue({ ...EXISTING_PROJECT });
    render(
      <ProjectFormModal
        apiClient={makeClient({ updateProject } as Partial<ApiClient>)}
        canPickFolders
        mode="edit"
        onClose={vi.fn()}
        onSaved={vi.fn()}
        project={EXISTING_PROJECT}
      />,
    );

    const nameInput = screen.getByPlaceholderText("例如：互联网医院");
    await userEvent.clear(nameInput);
    await userEvent.type(nameInput, "云图科研用药二期");
    await userEvent.click(screen.getByRole("button", { name: "保存" }));

    expect(updateProject).toHaveBeenCalledWith("p1", { name: "云图科研用药二期", color: "#2c8d83" });
  });

  it("canPickFolders 为 false 时材料根目录只读，隐藏添加/移除入口", () => {
    render(
      <ProjectFormModal
        apiClient={makeClient()}
        canPickFolders={false}
        mode="edit"
        onClose={vi.fn()}
        onSaved={vi.fn()}
        project={EXISTING_PROJECT}
      />,
    );

    expect(screen.getByText("/Volumes/资料盘/蓝鲸云/云图科研用药")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "＋ 添加目录" })).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "移除 /Volumes/资料盘/蓝鲸云/云图科研用药" }),
    ).not.toBeInTheDocument();
  });
});

function folderPayload(name?: string): FolderMatchesPayload {
  const recent = [
    { path: "/Volumes/资料盘/项目/数据中台", name: "数据中台", match: null, modified_at: "2026-09-20T00:00:00Z" },
    { path: "/Volumes/资料盘/项目/云图看板", name: "云图看板", match: null, modified_at: "2026-09-18T00:00:00Z" },
  ];
  const matches =
    name === "云图看板"
      ? [{ path: "/Volumes/资料盘/项目/云图看板", name: "云图看板", match: "exact" as const, modified_at: "2026-09-18T00:00:00Z" }]
      : [];
  return {
    matches,
    recent,
    create_parent: "/Volumes/资料盘/项目",
    create_parent_state: "online",
    create_name: (name ?? "").replace(/\//g, "-"),
    create_replaced: name?.includes("/") ? ["/"] : [],
  };
}

function folderClient(overrides: Partial<ApiClient> = {}) {
  return makeClient({
    folderMatches: vi.fn((name?: string) => Promise.resolve(folderPayload(name))),
    ...overrides,
  } as Partial<ApiClient>);
}

function renderCreate(apiClient: ApiClient, extra: Partial<Parameters<typeof ProjectFormModal>[0]> = {}) {
  const onSaved = vi.fn();
  const onClose = vi.fn();
  render(
    <ProjectFormModal apiClient={apiClient} canPickFolders mode="create" onClose={onClose} onSaved={onSaved} {...extra} />,
  );
  return { onSaved, onClose };
}

describe("ProjectFormModal 新建：项目文件夹", () => {
  it("先列出还没挂的文件夹，选中后项目名自动填文件夹名，创建时挂上它", async () => {
    const createProjectWith = vi.fn().mockResolvedValue({ id: "p9", name: "数据中台", color: "#3f51b5" });
    renderCreate(folderClient({ createProjectWith } as Partial<ApiClient>));

    const group = await screen.findByRole("radiogroup", { name: "项目文件夹" });
    expect(within(group).getByRole("radio", { name: /先不挂文件夹/ })).toBeChecked();
    await userEvent.click(await within(group).findByRole("radio", { name: /数据中台/ }));

    expect(screen.getByPlaceholderText("例如：互联网医院")).toHaveValue("数据中台");
    await userEvent.click(screen.getByRole("button", { name: "创建" }));

    expect(createProjectWith).toHaveBeenCalledWith({
      name: "数据中台",
      color: "#3f51b5",
      folder: { mode: "mount", path: "/Volumes/资料盘/项目/数据中台" },
    });
  });

  it("有同名文件夹时默认挂它，并标出「同名」", async () => {
    const createProjectWith = vi.fn().mockResolvedValue({ id: "p9", name: "云图看板", color: "#3f51b5" });
    renderCreate(folderClient({ createProjectWith } as Partial<ApiClient>));

    await userEvent.type(screen.getByPlaceholderText("例如：互联网医院"), "云图看板");
    const exact = await screen.findByRole("radio", { name: /云图看板\s*同名/ });
    await waitFor(() => expect(exact).toBeChecked());
    // 默认位置下已经有这个文件夹，不再给「新建」
    expect(screen.queryByRole("radio", { name: /下新建/ })).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "创建" }));

    expect(createProjectWith).toHaveBeenCalledWith({
      name: "云图看板",
      color: "#3f51b5",
      folder: { mode: "mount", path: "/Volumes/资料盘/项目/云图看板" },
    });
  });

  it("都不合适时可以在默认位置新建文件夹，非法字符换掉并在选项里标出", async () => {
    const createProjectWith = vi.fn().mockResolvedValue({ id: "p9", name: "云图/看板", color: "#3f51b5" });
    renderCreate(folderClient({ createProjectWith } as Partial<ApiClient>));

    await userEvent.type(screen.getByPlaceholderText("例如：互联网医院"), "云图/看板");
    const createOption = await screen.findByRole("radio", { name: /新建「云图-看板」（\/ 已换成 -）/ });
    expect(createOption).not.toBeChecked();
    await userEvent.click(createOption);
    await userEvent.click(screen.getByRole("button", { name: "创建" }));

    expect(createProjectWith).toHaveBeenCalledWith({
      name: "云图/看板",
      color: "#3f51b5",
      folder: { mode: "create", path: "/Volumes/资料盘/项目", name: "云图/看板" },
    });
  });

  it("盘没插时项目照样建好，弹窗说明文件夹还没建，点「知道了」再走", async () => {
    const createProjectWith = vi.fn().mockResolvedValue({
      id: "p9",
      name: "云图看板",
      color: "#3f51b5",
      folder_pending: { path: "/Volumes/资料盘/项目/云图看板", reason: "资料盘未连接，插上后再建文件夹" },
    });
    const { onSaved, onClose } = renderCreate(folderClient({ createProjectWith } as Partial<ApiClient>));

    await userEvent.type(screen.getByPlaceholderText("例如：互联网医院"), "云图看板");
    await userEvent.click(await screen.findByRole("button", { name: "创建" }));

    expect(await screen.findByRole("status")).toHaveTextContent("资料盘未连接，插上后再建文件夹");
    expect(onSaved).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole("button", { name: "知道了" }));
    expect(onSaved).toHaveBeenCalledWith(expect.objectContaining({ id: "p9" }));
    expect(onClose).toHaveBeenCalled();
  });
});

describe("ProjectFormModal 新建：近似重名", () => {
  const suggestion = {
    project_id: "p1",
    name: "云图科研用药",
    also_names: ["云图"],
    matched: "云图科研用药",
    match: "similar",
  };

  it("撞上近似重名时问「是不是它」，点「用它」打开已有项目", async () => {
    const createProjectWith = vi
      .fn()
      .mockRejectedValue(new ApiError("已有相近的项目", 409, { detail: "已有相近的项目", suggestion }));
    const onUseExisting = vi.fn();
    const { onSaved, onClose } = renderCreate(makeClient({ createProjectWith } as Partial<ApiClient>), { onUseExisting });

    await userEvent.type(screen.getByPlaceholderText("例如：互联网医院"), "云图科研");
    await userEvent.click(screen.getByRole("button", { name: "创建" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("已有「云图科研用药」（又称 云图），是不是它？");
    await userEvent.click(screen.getByRole("button", { name: "用它" }));
    expect(onUseExisting).toHaveBeenCalledWith("p1");
    expect(onClose).toHaveBeenCalled();
    expect(onSaved).not.toHaveBeenCalled();
  });

  it("点「仍然新建」带 force 再建一次", async () => {
    const createProjectWith = vi
      .fn()
      .mockRejectedValueOnce(new ApiError("已有相近的项目", 409, { detail: "已有相近的项目", suggestion }))
      .mockResolvedValueOnce({ id: "p9", name: "云图科研", color: "#3f51b5" });
    const { onSaved } = renderCreate(makeClient({ createProjectWith } as Partial<ApiClient>));

    await userEvent.type(screen.getByPlaceholderText("例如：互联网医院"), "云图科研");
    await userEvent.click(screen.getByRole("button", { name: "创建" }));
    await userEvent.click(await screen.findByRole("button", { name: "仍然新建" }));

    expect(createProjectWith).toHaveBeenLastCalledWith({ name: "云图科研", color: "#3f51b5", force: true });
    expect(onSaved).toHaveBeenCalledWith(expect.objectContaining({ id: "p9" }));
  });
});

describe("ProjectFormModal 编辑：合并与删除", () => {
  const OTHER: Project = { id: "p2", name: "数据中台", color: "#3f51b5" };

  it("合并到别的项目：选目标、看说明、确认后回调目标项目", async () => {
    const mergeProject = vi.fn().mockResolvedValue(OTHER);
    const onMerged = vi.fn();
    render(
      <ProjectFormModal
        apiClient={makeClient({ mergeProject } as Partial<ApiClient>)}
        canPickFolders
        mode="edit"
        onClose={vi.fn()}
        onMerged={onMerged}
        onSaved={vi.fn()}
        project={{ ...EXISTING_PROJECT, meeting_count: 3 }}
        projects={[EXISTING_PROJECT, OTHER]}
      />,
    );

    expect(screen.queryByRole("button", { name: "删除项目" })).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "合并到…" }));
    expect(screen.getByRole("button", { name: "确认合并" })).toBeDisabled();
    await userEvent.selectOptions(screen.getByRole("combobox", { name: "合并到哪个项目" }), "p2");
    expect(screen.getByText(/以后算作「数据中台」的另一个叫法/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "确认合并" }));

    expect(mergeProject).toHaveBeenCalledWith("p1", "p2");
    expect(onMerged).toHaveBeenCalledWith(OTHER);
  });

  it("没有会议和需求的项目可以删除", async () => {
    const deleteProject = vi.fn().mockResolvedValue({ ok: true, tasks_unassigned: 0, terms_to_public: 0 });
    const onDeleted = vi.fn();
    render(
      <ProjectFormModal
        apiClient={makeClient({ deleteProject } as Partial<ApiClient>)}
        canPickFolders
        mode="edit"
        onClose={vi.fn()}
        onDeleted={onDeleted}
        onSaved={vi.fn()}
        project={{ ...EXISTING_PROJECT, meeting_count: 0, requirement_counts: { active: 0, done: 0, shelved: 0, all: 0 } }}
      />,
    );

    await userEvent.click(screen.getByRole("button", { name: "删除项目" }));
    await userEvent.click(screen.getByRole("button", { name: "确认删除" }));

    expect(deleteProject).toHaveBeenCalledWith("p1");
    expect(onDeleted).toHaveBeenCalled();
  });
});
