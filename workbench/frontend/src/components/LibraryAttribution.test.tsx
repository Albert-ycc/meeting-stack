import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { AttributionSummary, MeetingSummary } from "../types";
import { LibraryPage } from "./LibraryPage";
import { OverviewPage } from "./OverviewPage";
import type { ApiClient } from "../api";

const SUMMARY: AttributionSummary = {
  needs_review_recent: 2,
  needs_review_total: 3,
  new_project_names: [
    { name: "智慧园区", norm_key: "智慧园区", meeting_count: 2, meeting_ids: ["m-3", "m-4"], last_at: "2026-09-20" },
  ],
  auto_30d: 9,
  corrected_30d: 1,
};

const MEETINGS: MeetingSummary[] = [
  {
    id: "m-1",
    title: "评审会",
    status: "published",
    tags: [],
    recording_date: "2026-09-25T10:00:00",
    attribution_state: "needs_review",
    candidates: [
      { project_id: "p-a", project_name: "云图AI", count: 2, llm: true, current: false },
      { project_id: "p-b", project_name: "数据中台", count: 1, llm: false, current: false },
    ],
  },
  {
    id: "m-2",
    title: "刚录的会",
    status: "published",
    tags: [],
    recording_date: "2026-09-25T09:00:00",
    attribution_state: "ai_pending",
  },
  {
    id: "m-3",
    title: "园区沟通",
    status: "published",
    tags: [],
    recording_date: "2026-09-25T08:00:00",
    attribution_state: "new_project",
    new_project_name: "智慧园区",
  },
  {
    id: "m-4",
    title: "闲聊",
    status: "published",
    tags: [],
    recording_date: "2026-09-25T07:00:00",
    attribution_state: "manual_none",
  },
];

function renderLibrary(props: Partial<Parameters<typeof LibraryPage>[0]> = {}) {
  const onFilter = vi.fn();
  render(
    <LibraryPage
      attributionSummary={SUMMARY}
      filters={{}}
      limit={50}
      meetings={MEETINGS}
      offset={0}
      onFilter={onFilter}
      onOpen={vi.fn()}
      onPageChange={vi.fn()}
      projects={[]}
      state="ready"
      tags={[]}
      total={MEETINGS.length}
      {...props}
    />,
  );
  return { onFilter };
}

describe("LibraryPage 归属列", () => {
  it("没归项目的会按归属状态说清楚", () => {
    renderLibrary();

    expect(screen.getByText("待你选")).toBeInTheDocument();
    expect(screen.getByText("等 AI 判断")).toBeInTheDocument();
    expect(screen.getByText("像新项目「智慧园区」")).toBeInTheDocument();
    expect(screen.getByText("不归项目")).toBeInTheDocument();
  });

  it("筛选栏的「待归属」「像新项目」切换 attribution 筛选", async () => {
    const { onFilter } = renderLibrary();

    await userEvent.click(screen.getByRole("button", { name: "待归属 3" }));
    expect(onFilter).toHaveBeenLastCalledWith({ attribution: "needs_review" });
    await userEvent.click(screen.getByRole("button", { name: "像新项目 2" }));
    expect(onFilter).toHaveBeenLastCalledWith({ attribution: "new_project" });
  });

  it("再点一次已选中的 chip 就取消", async () => {
    const { onFilter } = renderLibrary({ filters: { attribution: "needs_review" } });

    await userEvent.click(screen.getByRole("button", { name: "待归属 3" }));
    expect(onFilter).toHaveBeenLastCalledWith({ attribution: undefined });
  });

  it("项目筛选可以只看未归项目的会", async () => {
    const { onFilter } = renderLibrary();

    await userEvent.selectOptions(screen.getByLabelText("筛选项目"), "none");
    expect(onFilter).toHaveBeenLastCalledWith({ project_id: "none" });
  });

  it("待你选的行直接点候选项目，不打开会议", async () => {
    const onAssignProject = vi.fn().mockResolvedValue(undefined);
    const onOpen = vi.fn();
    renderLibrary({ onAssignProject, onOpen });

    const strip = screen.getByRole("group", { name: "评审会 选项目" });
    await userEvent.click(within(strip).getByRole("button", { name: "数据中台" }));

    expect(onAssignProject).toHaveBeenCalledWith("m-1", "p-b");
    expect(onOpen).not.toHaveBeenCalled();
  });

  it("没给 onAssignProject（手机）时不出候选按钮", () => {
    renderLibrary();
    expect(screen.queryByRole("group", { name: "评审会 选项目" })).not.toBeInTheDocument();
  });
});

describe("OverviewPage 等你选项目", () => {
  const client = {
    tasks: vi.fn().mockResolvedValue({ items: [], total: 0 }),
    meetings: vi.fn().mockResolvedValue({ items: [], total: 0, limit: 500, offset: 0 }),
  } as unknown as ApiClient;

  it("有待你选的会时出计数卡，点了去资料库", async () => {
    const onOpenAttributionReview = vi.fn();
    render(
      <OverviewPage
        apiClient={client}
        attributionSummary={SUMMARY}
        health={null}
        jobs={[]}
        jobsAvailable
        meetings={[]}
        onOpenAttributionReview={onOpenAttributionReview}
        onOpenJobs={vi.fn()}
        onOpenLibrary={vi.fn()}
        onOpenTasks={vi.fn()}
      />,
    );

    await userEvent.click(screen.getByRole("button", { name: /等你选项目/ }));
    expect(onOpenAttributionReview).toHaveBeenCalled();
  });

  it("没有时不出这张卡", () => {
    render(
      <OverviewPage
        apiClient={client}
        attributionSummary={{ ...SUMMARY, needs_review_recent: 0 }}
        health={null}
        jobs={[]}
        jobsAvailable
        meetings={[]}
        onOpenJobs={vi.fn()}
        onOpenLibrary={vi.fn()}
        onOpenTasks={vi.fn()}
      />,
    );
    expect(screen.queryByText("等你选项目")).not.toBeInTheDocument();
  });
});
