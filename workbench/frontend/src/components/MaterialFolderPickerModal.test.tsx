import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { ApiClient } from "../api";
import type { MaterialFolderStat, ProjectSubfoldersPayload } from "../types";
import { MaterialFolderPickerModal } from "./MaterialFolderPickerModal";

const folderA: MaterialFolderStat = {
  name: "V1.5.7-北辰仓快递配送-260914",
  path: "/root/V1.5.7-北辰仓快递配送-260914",
  exists: true,
  file_count: 91,
  file_count_capped: false,
  modified_at: "2026-09-14T00:00:00Z",
};

const folderB: MaterialFolderStat = {
  name: "V1.5.7-北辰直邮-260907",
  path: "/root/V1.5.7-北辰直邮-260907",
  exists: true,
  file_count: 2000,
  file_count_capped: true,
  modified_at: "2026-09-07T00:00:00Z",
};

describe("MaterialFolderPickerModal", () => {
  it("shows the single root as weak text and lists its folders with capped counts", async () => {
    const payload: ProjectSubfoldersPayload = {
      roots: [{ root_id: 1, root_path: "/Volumes/资料盘/蓝鲸云/云图科研用药", exists: true, folders: [folderA, folderB] }],
    };
    const projectMaterialSubfolders = vi.fn().mockResolvedValue(payload);
    render(
      <MaterialFolderPickerModal
        apiClient={{ projectMaterialSubfolders } as unknown as ApiClient}
        onCancel={vi.fn()}
        onConfirm={vi.fn()}
        projectId="project-a"
        projectName="云图科研用药"
        selectedFolders={[folderA]}
      />,
    );

    expect(await screen.findByText("/Volumes/资料盘/蓝鲸云/云图科研用药")).toBeInTheDocument();
    expect(screen.getByText("2000+ 个文件")).toBeInTheDocument();
    expect(screen.getByText("已选 1 个")).toBeInTheDocument();
    expect(screen.getByRole("checkbox", { name: /V1\.5\.7-北辰仓快递配送-260914/ })).toBeChecked();
  });

  it("switches between multiple roots and accumulates selections across them", async () => {
    const payload: ProjectSubfoldersPayload = {
      roots: [
        { root_id: 1, root_path: "/root-one", exists: true, folders: [folderA] },
        { root_id: 2, root_path: "/root-two", exists: true, folders: [folderB] },
      ],
    };
    const projectMaterialSubfolders = vi.fn().mockResolvedValue(payload);
    const onConfirm = vi.fn();
    render(
      <MaterialFolderPickerModal
        apiClient={{ projectMaterialSubfolders } as unknown as ApiClient}
        onCancel={vi.fn()}
        onConfirm={onConfirm}
        projectId="project-a"
        projectName="云图科研用药"
        selectedFolders={[]}
      />,
    );

    await userEvent.click(await screen.findByRole("checkbox", { name: /V1\.5\.7-北辰仓快递配送-260914/ }));
    await userEvent.click(screen.getByRole("tab", { name: "root-two" }));
    await userEvent.click(await screen.findByRole("checkbox", { name: /V1\.5\.7-北辰直邮-260907/ }));
    await userEvent.click(screen.getByRole("button", { name: "确定" }));

    expect(onConfirm).toHaveBeenCalledWith([folderA, folderB]);
  });

  it("filters the visible folder list by name", async () => {
    const payload: ProjectSubfoldersPayload = {
      roots: [{ root_id: 1, root_path: "/root", exists: true, folders: [folderA, folderB] }],
    };
    const projectMaterialSubfolders = vi.fn().mockResolvedValue(payload);
    render(
      <MaterialFolderPickerModal
        apiClient={{ projectMaterialSubfolders } as unknown as ApiClient}
        onCancel={vi.fn()}
        onConfirm={vi.fn()}
        projectId="project-a"
        projectName="云图科研用药"
        selectedFolders={[]}
      />,
    );

    await screen.findByText("V1.5.7-北辰仓快递配送-260914");
    await userEvent.type(screen.getByLabelText("搜索文件夹"), "直邮");

    expect(screen.queryByText("V1.5.7-北辰仓快递配送-260914")).not.toBeInTheDocument();
    expect(screen.getByText("V1.5.7-北辰直邮-260907")).toBeInTheDocument();
  });

  it("shows the no-root empty state with a shortcut to go hang a material root", async () => {
    const payload: ProjectSubfoldersPayload = { roots: [] };
    const projectMaterialSubfolders = vi.fn().mockResolvedValue(payload);
    const onOpenProject = vi.fn();
    const onCancel = vi.fn();
    render(
      <MaterialFolderPickerModal
        apiClient={{ projectMaterialSubfolders } as unknown as ApiClient}
        onCancel={onCancel}
        onConfirm={vi.fn()}
        onOpenProject={onOpenProject}
        projectId="project-sfe"
        projectName="蓝鲸云SFE方案整合"
        selectedFolders={[]}
      />,
    );

    expect(await screen.findByText("蓝鲸云SFE方案整合 还没有材料根目录")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "去挂根目录" }));
    expect(onOpenProject).toHaveBeenCalledWith("project-sfe");
    expect(onCancel).toHaveBeenCalled();
  });
});
