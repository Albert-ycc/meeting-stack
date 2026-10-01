import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { ApiClient } from "../../api";
import { candidateItem, CVM, HENGRUI, HUAXIA, poolPayload, requirementItem, YIMI } from "./poolFixtures";
import { RequirementPoolPage } from "./RequirementPoolPage";

async function expectToast(text: string) {
  await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent(text));
}

function renderPage(api: Partial<ApiClient>, props: Partial<Parameters<typeof RequirementPoolPage>[0]> = {}) {
  const handlers = {
    onClaimCandidate: vi.fn(),
    onCreateRequirement: vi.fn(),
    onOpenMeeting: vi.fn(),
    onOpenRequirement: vi.fn(),
    onProjectsChanged: vi.fn(),
  };
  render(<RequirementPoolPage apiClient={api as ApiClient} canWrite {...handlers} {...props} />);
  return handlers;
}

describe("RequirementPoolPage", () => {
  it("默认看进行中，一次取全墙；页签带计数，待认领有候选时带橙点", async () => {
    const requirementPool = vi.fn().mockResolvedValue(poolPayload());
    const handlers = renderPage({ requirementPool });

    expect(await screen.findByRole("article", { name: "需求：京东科研仓对接" })).toBeInTheDocument();
    expect(requirementPool).toHaveBeenCalledWith({
      status: "active",
      project_id: undefined,
      priority: undefined,
      q: undefined,
      limit: 500,
    });
    expect(screen.getByRole("tab", { name: /进行中/ })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("tab", { name: /待认领/ })).toHaveTextContent("待认领3");
    expect(screen.getByRole("tab", { name: /待认领/ })).toHaveClass("has-dot");
    expect(screen.getByRole("tab", { name: /全部/ })).toHaveTextContent("全部14");
    expect(screen.getByText(/张挂在墙上/).closest(".pool-count")).toHaveTextContent("1张挂在墙上进行中");

    await userEvent.click(screen.getByRole("heading", { name: "京东科研仓对接" }));
    expect(handlers.onOpenRequirement).toHaveBeenCalledWith("requirement-jd");
  });

  it("页签和筛选记在本机：再进来按上次的页签、项目、优先级取", async () => {
    window.localStorage.setItem("meeting-workbench:view:requirementPool.tab", JSON.stringify("shelved"));
    window.localStorage.setItem("meeting-workbench:view:requirementPool.projects", JSON.stringify([YIMI, HENGRUI]));
    window.localStorage.setItem("meeting-workbench:view:requirementPool.priorities", JSON.stringify(["P0", "P1"]));
    const requirementPool = vi.fn().mockResolvedValue(poolPayload({ items: [] }));
    renderPage({ requirementPool });

    await waitFor(() =>
      expect(requirementPool).toHaveBeenCalledWith(
        expect.objectContaining({ status: "shelved", project_id: `${YIMI},${HENGRUI}`, priority: "P0,P1" }),
      ),
    );
    expect(await screen.findByText("没有符合筛选条件的需求")).toBeInTheDocument();
  });

  it("优先级多选、点「我的方向」里的项目筛选，清空筛选一次全清", async () => {
    const requirementPool = vi.fn().mockResolvedValue(poolPayload());
    renderPage({ requirementPool });
    await screen.findByRole("article", { name: "需求：京东科研仓对接" });

    await userEvent.click(screen.getByRole("button", { name: "P0" }));
    await userEvent.click(screen.getByRole("button", { name: "P1" }));
    await userEvent.click(screen.getByRole("button", { name: /医米科研用药/ }));
    await waitFor(() =>
      expect(requirementPool).toHaveBeenLastCalledWith(
        expect.objectContaining({ priority: "P0,P1", project_id: YIMI }),
      ),
    );

    await userEvent.click(screen.getByRole("button", { name: "清空筛选" }));
    await waitFor(() =>
      expect(requirementPool).toHaveBeenLastCalledWith(
        expect.objectContaining({ priority: undefined, project_id: undefined, q: undefined }),
      ),
    );
    expect(screen.queryByRole("button", { name: "清空筛选" })).not.toBeInTheDocument();
  });

  it("需求名称搜索打完字停一下再查", async () => {
    const requirementPool = vi.fn().mockResolvedValue(poolPayload());
    renderPage({ requirementPool });
    await screen.findByRole("article", { name: "需求：京东科研仓对接" });

    await userEvent.type(screen.getByRole("textbox", { name: "搜需求名称" }), "签收");
    await waitFor(() => expect(requirementPool).toHaveBeenLastCalledWith(expect.objectContaining({ q: "签收" })));
    expect(requirementPool.mock.calls.filter(([filters]) => filters.q === "签")).toHaveLength(0);
  });

  it("待认领：丢掉一条后重新取，已丢掉里能撤销", async () => {
    const pending = poolPayload({ status: "pending", items: [candidateItem()], dropped_count: 0 });
    const afterDrop = poolPayload({ status: "pending", items: [], dropped_count: 1 });
    const requirementPool = vi
      .fn()
      .mockResolvedValueOnce(poolPayload())
      .mockResolvedValueOnce(pending)
      .mockResolvedValueOnce(afterDrop)
      .mockResolvedValue(pending);
    const dropCandidate = vi.fn().mockResolvedValue({});
    const droppedCandidates = vi.fn().mockResolvedValue({
      items: [{ ...candidateItem({ dropped_at: "2026-10-01T02:00:00+00:00" }), status: "dropped", restore_until: "2026-10-31T02:00:00+00:00" }],
      total: 1,
      undo_days: 30,
    });
    const restoreCandidate = vi.fn().mockResolvedValue({});
    const handlers = renderPage({ requirementPool, dropCandidate, droppedCandidates, restoreCandidate });
    await screen.findByRole("article", { name: "需求：京东科研仓对接" });

    await userEvent.click(screen.getByRole("tab", { name: /待认领/ }));
    const poster = await screen.findByRole("article", { name: "候选：京东仓签收凭证" });
    expect(screen.getByText(/条候选/).closest(".pool-count")).toHaveTextContent("1条候选待认领");
    await userEvent.click(within(poster).getByRole("heading", { name: "京东仓签收凭证" }));
    expect(handlers.onClaimCandidate).toHaveBeenCalledWith("candidate-receipt");

    await userEvent.click(within(poster).getByRole("button", { name: "丢掉" }));
    expect(dropCandidate).toHaveBeenCalledWith("candidate-receipt");
    await expectToast("已丢掉「京东仓签收凭证」，30 天内可以在「已丢掉」里撤销");
    expect(await screen.findByText("没有待认领的候选")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "已丢掉 1 条" }));
    const dialog = await screen.findByRole("dialog", { name: "已丢掉的候选" });
    expect(within(dialog).getByText(/10-31 前可撤销/)).toBeInTheDocument();
    await userEvent.click(within(dialog).getByRole("button", { name: "撤销" }));
    expect(restoreCandidate).toHaveBeenCalledWith("candidate-receipt");
    await expectToast("「京东仓签收凭证」回到待认领了");
  });

  it("合并：弹层里预选 AI 推荐的那条，合并后提示并重新取", async () => {
    window.localStorage.setItem("meeting-workbench:view:requirementPool.tab", JSON.stringify("pending"));
    const requirementPool = vi.fn().mockResolvedValue(poolPayload({ status: "pending", items: [candidateItem()] }));
    const candidateMergeTargets = vi.fn().mockResolvedValue({
      project_id: YIMI,
      items: [
        {
          id: "requirement-jd",
          title: "京东科研仓对接",
          status: "active",
          priority: "P0",
          meeting_title: "260916 医米京东科研仓系统对接",
          recording_date: "2026-09-16T19:01:50-07:00",
          recommended: true,
        },
        {
          id: "requirement-edc",
          title: "EDC 系统选型",
          status: "shelved",
          priority: "P1",
          meeting_title: "EDC 系统选型与产研对接决策",
          recording_date: "2026-09-28T18:37:26-07:00",
          recommended: false,
        },
      ],
    });
    const mergeCandidate = vi.fn().mockResolvedValue(requirementItem());
    const handlers = renderPage({ requirementPool, candidateMergeTargets, mergeCandidate });

    const poster = await screen.findByRole("article", { name: "候选：京东仓签收凭证" });
    await userEvent.click(within(poster).getByRole("button", { name: "合并" }));
    const dialog = await screen.findByRole("dialog", { name: "合并到已有需求" });
    expect(within(dialog).getByRole("radio", { name: /京东科研仓对接/ })).toBeChecked();
    expect(within(dialog).getByText("AI 推荐")).toBeInTheDocument();
    expect(within(dialog).getByText("已搁置")).toBeInTheDocument();
    expect(candidateMergeTargets).toHaveBeenCalledWith("candidate-receipt", undefined);

    await userEvent.click(within(dialog).getByRole("button", { name: "合并" }));
    expect(mergeCandidate).toHaveBeenCalledWith("candidate-receipt", "requirement-jd", undefined);
    await expectToast("已合并到「京东科研仓对接」");
    expect(screen.queryByRole("dialog", { name: "合并到已有需求" })).not.toBeInTheDocument();
    await waitFor(() => expect(handlers.onProjectsChanged).toHaveBeenCalledTimes(1));
  });

  it("拖动座次后整排保存，提示「座次已保存」", async () => {
    const requirementPool = vi.fn().mockResolvedValue(poolPayload());
    const saveProjectSeats = vi.fn().mockResolvedValue({ seats: [] });
    renderPage({ requirementPool, saveProjectSeats });
    await screen.findByRole("article", { name: "需求：京东科研仓对接" });

    await userEvent.click(screen.getByRole("button", { name: /未排座次/ }));
    await userEvent.click(screen.getByRole("button", { name: "排入座次" }));

    expect(saveProjectSeats).toHaveBeenCalledWith([YIMI, HENGRUI, HUAXIA, CVM]);
    await expectToast("座次已保存");
  });

  it("墙上还没有需求时给新建入口；回到需求池带着的提示只显示一次", async () => {
    const requirementPool = vi.fn().mockResolvedValue(
      poolPayload({ items: [], total: 0, counts: { pending: 0, active: 0, done: 0, shelved: 0, all: 0 } }),
    );
    const onFlashShown = vi.fn();
    const handlers = renderPage({ requirementPool }, { flash: "已认领「科室会预约后台导出」，挂上墙了", onFlashShown });

    expect(await screen.findByText("墙上还没有需求")).toBeInTheDocument();
    expect(screen.getByText("会后 AI 会从纪要里抽需求候选，放进待认领")).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("已认领「科室会预约后台导出」，挂上墙了");
    expect(onFlashShown).toHaveBeenCalledTimes(1);

    await userEvent.click(within(screen.getByText("墙上还没有需求").parentElement!).getByRole("button", { name: /新建需求/ }));
    expect(handlers.onCreateRequirement).toHaveBeenCalledTimes(1);
  });
});
