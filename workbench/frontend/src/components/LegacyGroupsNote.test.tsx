import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { LegacyGroupsNote } from "./LegacyGroupsNote";
import type { ApiClient } from "../api";

const SUMMARY = {
  event_id: 7,
  at: "2026-09-26T00:00:00Z",
  undone: false,
  groups: [
    { scope: "云图", count: 3, project_id: "p1", project_name: "云图科研用药" },
    { scope: "互联网医院", count: 5, project_id: null, project_name: null },
  ],
};

function makeClient(overrides: Partial<ApiClient> = {}) {
  return {
    legacyGroups: vi.fn().mockResolvedValue({ summary: SUMMARY }),
    undoLegacyGroups: vi.fn().mockResolvedValue({ restored: 8 }),
    dismissLegacyGroups: vi.fn().mockResolvedValue({ ok: true }),
    ...overrides,
  } as unknown as ApiClient;
}

describe("LegacyGroupsNote", () => {
  it("说明整理了几个旧分组，点查看列出去向", async () => {
    render(<LegacyGroupsNote apiClient={makeClient()} canWrite onChanged={vi.fn()} />);

    expect(await screen.findByText("升级时已自动整理 2 个旧分组")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "查看" }));
    expect(screen.getByText("「云图」3 条 → 项目「云图科研用药」")).toBeInTheDocument();
    expect(screen.getByText("「互联网医院」5 条 → 公共")).toBeInTheDocument();
  });

  it("撤销后重读词典，说明改成已撤销", async () => {
    const apiClient = makeClient();
    const onChanged = vi.fn();
    render(<LegacyGroupsNote apiClient={apiClient} canWrite onChanged={onChanged} />);

    await userEvent.click(await screen.findByRole("button", { name: "撤销" }));

    expect(apiClient.undoLegacyGroups).toHaveBeenCalled();
    expect(onChanged).toHaveBeenCalled();
    expect(screen.getByText("已撤销自动整理，旧分组恢复原样")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "撤销" })).not.toBeInTheDocument();
  });

  it("知道了就收起；没有整理过时不显示；只读时没有撤销", async () => {
    const apiClient = makeClient();
    const { unmount } = render(<LegacyGroupsNote apiClient={apiClient} canWrite onChanged={vi.fn()} />);
    await userEvent.click(await screen.findByRole("button", { name: "知道了" }));
    expect(apiClient.dismissLegacyGroups).toHaveBeenCalled();
    expect(screen.queryByText(/旧分组/)).not.toBeInTheDocument();
    unmount();

    render(<LegacyGroupsNote apiClient={makeClient()} canWrite={false} onChanged={vi.fn()} />);
    expect(await screen.findByText("升级时已自动整理 2 个旧分组")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "撤销" })).not.toBeInTheDocument();
  });

  it("没有整理记录时不显示", async () => {
    const apiClient = makeClient({ legacyGroups: vi.fn().mockResolvedValue({ summary: null }) } as Partial<ApiClient>);
    const { container } = render(<LegacyGroupsNote apiClient={apiClient} canWrite onChanged={vi.fn()} />);
    await waitFor(() => expect(apiClient.legacyGroups).toHaveBeenCalled());
    expect(container).toBeEmptyDOMElement();
  });
});
