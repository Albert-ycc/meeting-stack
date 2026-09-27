import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { FolderSuggestionBanner } from "./FolderSuggestionBanner";
import type { ApiClient } from "../api";
import type { ColdStartFolderItem } from "../types";

const ITEMS: ColdStartFolderItem[] = [
  { project_id: "p1", project_name: "数据中台", path: "/Volumes/资料盘/项目/数据中台", folder_name: "数据中台", match: "exact" },
  {
    project_id: "p2",
    project_name: "云图科研用药",
    path: "/Volumes/资料盘/项目/云图科研用药二期资料",
    folder_name: "云图科研用药二期资料",
    match: "similar",
  },
];

function makeClient(overrides: Partial<ApiClient> = {}) {
  return {
    coldStartFolders: vi.fn().mockResolvedValue({ items: ITEMS, snoozed_until: null }),
    addProjectMaterialRoot: vi.fn().mockResolvedValue({}),
    declineFolderSuggestions: vi.fn().mockResolvedValue({ ok: true }),
    snoozeFolderSuggestions: vi.fn().mockResolvedValue({ snoozed_until: "2026-09-29T00:00:00Z" }),
    ...overrides,
  } as unknown as ApiClient;
}

describe("FolderSuggestionBanner", () => {
  it("同名的默认勾选、相近的不勾；挂上勾选的，没勾的记为不挂", async () => {
    const apiClient = makeClient();
    const onProjectsChanged = vi.fn();
    render(<FolderSuggestionBanner apiClient={apiClient} onProjectsChanged={onProjectsChanged} />);

    expect(await screen.findByText("2 个项目找到了同名或名字相近的文件夹，要挂上吗？")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "看看" }));

    const dialog = screen.getByRole("dialog", { name: "挂上同名文件夹" });
    const boxes = dialog.querySelectorAll<HTMLInputElement>("input[type=checkbox]");
    expect(boxes[0]).toBeChecked();
    expect(boxes[1]).not.toBeChecked();
    expect(screen.getByText("名字相近")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "挂上 1 个" }));

    expect(apiClient.addProjectMaterialRoot).toHaveBeenCalledWith("p1", "/Volumes/资料盘/项目/数据中台");
    expect(apiClient.addProjectMaterialRoot).toHaveBeenCalledTimes(1);
    expect(apiClient.declineFolderSuggestions).toHaveBeenCalledWith(["p2"]);
    expect(onProjectsChanged).toHaveBeenCalled();
    expect(await screen.findByText("已挂上 1 个文件夹")).toBeInTheDocument();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("没挂上的留在弹窗里说原因", async () => {
    const apiClient = makeClient({
      addProjectMaterialRoot: vi.fn().mockRejectedValue(new Error("这个文件夹已挂在「北辰」项目下")),
    } as Partial<ApiClient>);
    render(<FolderSuggestionBanner apiClient={apiClient} />);

    await userEvent.click(await screen.findByRole("button", { name: "看看" }));
    await userEvent.click(screen.getByRole("button", { name: "挂上 1 个" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("这个文件夹已挂在「北辰」项目下");
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    expect(screen.queryByText("云图科研用药")).not.toBeInTheDocument();
  });

  it("点稍后收起并记下", async () => {
    const apiClient = makeClient();
    render(<FolderSuggestionBanner apiClient={apiClient} />);

    await userEvent.click(await screen.findByRole("button", { name: "稍后" }));

    expect(apiClient.snoozeFolderSuggestions).toHaveBeenCalled();
    await waitFor(() => expect(screen.queryByRole("button", { name: "看看" })).not.toBeInTheDocument());
  });

  it("没有候选时什么都不显示", async () => {
    const apiClient = makeClient({
      coldStartFolders: vi.fn().mockResolvedValue({ items: [], snoozed_until: null }),
    } as Partial<ApiClient>);
    const { container } = render(<FolderSuggestionBanner apiClient={apiClient} />);
    await waitFor(() => expect(apiClient.coldStartFolders).toHaveBeenCalled());
    expect(container).toBeEmptyDOMElement();
  });
});
