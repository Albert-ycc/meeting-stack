import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { ProjectFormModal } from "./ProjectFormModal";
import type { ApiClient } from "../api";
import type { MaterialBrowsePayload, Project } from "../types";

function makeClient(overrides: Partial<ApiClient> = {}) {
  return {
    createProject: vi.fn().mockResolvedValue({ id: "p1", name: "互联网医院", color: "#3f51b5" }),
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
    const createProject = vi.fn().mockResolvedValue({ id: "p2", name: "互联网医院", color: "#5090ff" });
    const onSaved = vi.fn();
    const onClose = vi.fn();
    render(
      <ProjectFormModal
        apiClient={makeClient({ createProject } as Partial<ApiClient>)}
        canPickFolders
        mode="create"
        onClose={onClose}
        onSaved={onSaved}
      />,
    );

    await userEvent.type(screen.getByPlaceholderText("例如：互联网医院"), "互联网医院");
    await userEvent.click(screen.getByRole("button", { name: "选择颜色 #5090ff" }));
    await userEvent.click(screen.getByRole("button", { name: "创建" }));

    expect(createProject).toHaveBeenCalledWith("互联网医院", "#5090ff", undefined);
    expect(onSaved).toHaveBeenCalledWith({ id: "p2", name: "互联网医院", color: "#5090ff" });
    expect(onClose).toHaveBeenCalled();
  });

  it("添加材料根目录后创建时带上路径列表", async () => {
    const createProject = vi.fn().mockResolvedValue({ id: "p3", name: "新项目", color: "#3f51b5" });
    render(
      <ProjectFormModal
        apiClient={makeClient({ createProject } as Partial<ApiClient>)}
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

    expect(createProject).toHaveBeenCalledWith("新项目", "#3f51b5", ["/Volumes/资料盘/蓝鲸云"]);
  });

  it("保存失败时显示错误原因，不关闭弹窗", async () => {
    const createProject = vi.fn().mockRejectedValue(new Error("项目名称已存在"));
    const onClose = vi.fn();
    render(
      <ProjectFormModal
        apiClient={makeClient({ createProject } as Partial<ApiClient>)}
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
    const createProject = vi.fn().mockRejectedValue(new Error("已有同名项目"));
    const onClose = vi.fn();
    const onSaved = vi.fn();
    render(
      <ProjectFormModal
        apiClient={makeClient({ createProject } as Partial<ApiClient>)}
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
