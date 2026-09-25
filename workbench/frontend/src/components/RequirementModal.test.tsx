import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { ApiClient } from "../api";
import type { ProjectSubfoldersPayload, RequirementDetail } from "../types";
import { RequirementModal } from "./RequirementModal";

const projects = [
  { id: "project-a", name: "云图科研用药", color: "#2c8d83" },
  { id: "project-b", name: "MDT", color: "#8c772c" },
];

const subfolders: ProjectSubfoldersPayload = {
  roots: [
    {
      root_id: 1,
      root_path: "/Volumes/资料盘/蓝鲸云/云图科研用药",
      exists: true,
      folders: [
        {
          name: "V1.5.7-北辰仓快递配送-260914",
          path: "/Volumes/资料盘/蓝鲸云/云图科研用药/V1.5.7-北辰仓快递配送-260914",
          exists: true,
          file_count: 91,
          file_count_capped: false,
          modified_at: "2026-09-14T00:00:00Z",
        },
      ],
    },
  ],
};

function detail(overrides: Partial<RequirementDetail> = {}): RequirementDetail {
  return {
    id: "req-1",
    project_id: "project-a",
    project_name: "云图科研用药",
    project_color: "#2c8d83",
    title: "北辰仓快递配送",
    priority: "P0",
    status: "active",
    created_at: "2026-09-07T00:00:00Z",
    updated_at: "2026-09-07T00:00:00Z",
    open_task_count: 0,
    meeting_count: 0,
    latest_meeting_date: null,
    folder_count: 0,
    folders: [],
    meetings: [],
    tasks: [],
    ...overrides,
  };
}

describe("RequirementModal", () => {
  it("creates a requirement with the chosen project, priority and picked folders", async () => {
    const createRequirement = vi.fn().mockResolvedValue(detail());
    const projectMaterialSubfolders = vi.fn().mockResolvedValue(subfolders);
    const apiClient = { createRequirement, projectMaterialSubfolders } as unknown as ApiClient;
    const onSaved = vi.fn();
    render(
      <RequirementModal
        apiClient={apiClient}
        canPickFolders
        defaultProjectId="project-a"
        mode="create"
        onClose={vi.fn()}
        onSaved={onSaved}
        projects={projects}
      />,
    );

    await userEvent.type(screen.getByPlaceholderText("例如：北辰仓快递配送"), "老患者超期申领拦截");
    await userEvent.click(screen.getByRole("button", { name: "P1" }));
    await userEvent.click(screen.getByRole("button", { name: "选择文件夹" }));

    await screen.findByRole("dialog", { name: "选择材料文件夹" });
    await userEvent.click(await screen.findByRole("checkbox", { name: /V1\.5\.7-北辰仓快递配送-260914/ }));
    await userEvent.click(screen.getByRole("button", { name: "确定" }));

    expect(screen.getByText("V1.5.7-北辰仓快递配送-260914")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "创建" }));

    await waitFor(() =>
      expect(createRequirement).toHaveBeenCalledWith({
        project_id: "project-a",
        title: "老患者超期申领拦截",
        priority: "P1",
        folder_paths: ["/Volumes/资料盘/蓝鲸云/云图科研用药/V1.5.7-北辰仓快递配送-260914"],
      }),
    );
    expect(onSaved).toHaveBeenCalled();
  });

  it("clears picked folders when the project changes (D6)", async () => {
    const apiClient = {} as unknown as ApiClient;
    render(
      <RequirementModal
        apiClient={apiClient}
        canPickFolders
        mode="edit"
        onClose={vi.fn()}
        onSaved={vi.fn()}
        projects={projects}
        requirement={detail({
          folders: [
            {
              id: 1,
              name: "V1.5.7-北辰仓快递配送-260914",
              path: "/Volumes/资料盘/蓝鲸云/云图科研用药/V1.5.7-北辰仓快递配送-260914",
              exists: true,
              file_count: 91,
              file_count_capped: false,
              modified_at: "2026-09-14T00:00:00Z",
              preview_files: [],
            },
          ],
        })}
      />,
    );

    expect(screen.getByText("V1.5.7-北辰仓快递配送-260914")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /云图科研用药/ }));
    await userEvent.click(screen.getByRole("option", { name: "MDT" }));

    expect(screen.queryByText("V1.5.7-北辰仓快递配送-260914")).not.toBeInTheDocument();
  });

  it("fetches the full detail before editing a list-page summary so saving never wipes its folders", async () => {
    const fullDetail = detail({
      folders: [
        {
          id: 7,
          name: "老患者超期申领拦截-260909",
          path: "/Volumes/资料盘/蓝鲸云/云图科研用药/老患者超期申领拦截-260909",
          exists: true,
          file_count: 14,
          file_count_capped: false,
          modified_at: "2026-09-10T00:00:00Z",
          preview_files: [],
        },
      ],
    });
    const requirement = vi.fn().mockResolvedValue(fullDetail);
    const updateRequirement = vi.fn().mockResolvedValue(fullDetail);
    const apiClient = { requirement, updateRequirement } as unknown as ApiClient;
    render(
      <RequirementModal
        apiClient={apiClient}
        canPickFolders
        mode="edit"
        onClose={vi.fn()}
        onSaved={vi.fn()}
        projects={projects}
        // 摘要（列表页给的）没有 folders 字段
        requirement={{
          id: "req-1",
          project_id: "project-a",
          project_name: "云图科研用药",
          project_color: "#2c8d83",
          title: "老患者超期申领拦截",
          priority: "P1",
          status: "active",
          created_at: "2026-09-09T00:00:00Z",
          updated_at: "2026-09-09T00:00:00Z",
          open_task_count: 0,
          meeting_count: 0,
          latest_meeting_date: null,
          folder_count: 1,
        }}
      />,
    );

    expect(await screen.findByText("老患者超期申领拦截-260909")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "保存" }));

    await waitFor(() =>
      expect(updateRequirement).toHaveBeenCalledWith("req-1", {
        title: "老患者超期申领拦截",
        project_id: "project-a",
        priority: "P1",
        status: "active",
        folder_paths: ["/Volumes/资料盘/蓝鲸云/云图科研用药/老患者超期申领拦截-260909"],
      }),
    );
  });

  it("D22：从没有根目录的项目点「去挂根目录」时，整个需求弹窗一起关掉，不留已填内容", async () => {
    const projectMaterialSubfolders = vi.fn().mockResolvedValue({ roots: [] });
    const onClose = vi.fn();
    const onOpenProject = vi.fn();
    render(
      <RequirementModal
        apiClient={{ projectMaterialSubfolders } as unknown as ApiClient}
        canPickFolders
        defaultProjectId="project-a"
        mode="create"
        onClose={onClose}
        onOpenProject={onOpenProject}
        onSaved={vi.fn()}
        projects={projects}
      />,
    );

    await userEvent.type(screen.getByPlaceholderText("例如：北辰仓快递配送"), "填了一半的需求");
    await userEvent.click(screen.getByRole("button", { name: "选择文件夹" }));
    await userEvent.click(await screen.findByRole("button", { name: "去挂根目录" }));

    expect(onOpenProject).toHaveBeenCalledWith("project-a");
    expect(onClose).toHaveBeenCalled();
  });
});
