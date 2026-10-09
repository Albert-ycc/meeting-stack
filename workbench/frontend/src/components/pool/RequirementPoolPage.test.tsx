import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError, type ApiClient } from "../../api";
import type { PoolItem, RequirementContext, RequirementDetail, RequirementMergeResult } from "../../types";
import { installClipboard, uninstallClipboard, writtenText } from "./clipboardStub";
import { higherPriority } from "./MergeRequirementDialog";
import {
  candidateItem,
  CVM,
  EXPORT_SOURCE,
  HENGRUI,
  HUAXIA,
  JD_SOURCE,
  mergeTargetsPayload,
  poolPayload,
  requirementItem,
  taskItem,
  YIMI,
} from "./poolFixtures";
import { mergedMessage, RequirementPoolPage } from "./RequirementPoolPage";

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
    await expectToast("已丢掉「京东仓签收凭证」，30 天内可在已丢掉里撤销");
    expect(await screen.findByText("没有待认领的候选")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "已丢掉 1 条" }));
    const dialog = await screen.findByRole("dialog", { name: "已丢掉的候选" });
    expect(within(dialog).getByText("京东仓签收凭证")).toBeInTheDocument();
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
    await expectToast("已合并到「京东科研仓对接」，这场会和原话已加进去");
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
    const handlers = renderPage({ requirementPool }, { flash: { message: "已认领「科室会预约后台导出」，挂上墙了" }, onFlashShown });

    expect(await screen.findByText("墙上还没有需求")).toBeInTheDocument();
    expect(screen.getByText("会后 AI 会从纪要里抽需求候选，放进待认领")).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("已认领「科室会预约后台导出」，挂上墙了");
    expect(onFlashShown).toHaveBeenCalledTimes(1);

    await userEvent.click(within(screen.getByText("墙上还没有需求").parentElement!).getByRole("button", { name: /新建需求/ }));
    expect(handlers.onCreateRequirement).toHaveBeenCalledTimes(1);
  });

  it("本机记着的筛选被写坏（非法 JSON、类型不对）时不白屏，按默认筛选取", async () => {
    window.localStorage.setItem("meeting-workbench:view:requirementPool.tab", JSON.stringify("nope"));
    window.localStorage.setItem("meeting-workbench:view:requirementPool.projects", "null");
    window.localStorage.setItem("meeting-workbench:view:requirementPool.priorities", JSON.stringify(["P9", 1]));
    window.localStorage.setItem("meeting-workbench:view:requirementPool.q", "123");
    const requirementPool = vi.fn().mockResolvedValue(poolPayload());
    renderPage({ requirementPool });

    expect(await screen.findByRole("article", { name: "需求：京东科研仓对接" })).toBeInTheDocument();
    expect(requirementPool).toHaveBeenCalledWith({
      status: "active",
      project_id: undefined,
      priority: undefined,
      q: undefined,
      limit: 500,
    });
  });

  it("记着的项目已经删掉、合并掉了：从筛选里去掉，重新取", async () => {
    window.localStorage.setItem(
      "meeting-workbench:view:requirementPool.projects",
      JSON.stringify([YIMI, "project-deleted", "unassigned"]),
    );
    const requirementPool = vi.fn().mockResolvedValue(poolPayload());
    renderPage({ requirementPool });

    await waitFor(() =>
      expect(requirementPool).toHaveBeenLastCalledWith(expect.objectContaining({ project_id: YIMI })),
    );
    expect(requirementPool).toHaveBeenCalledWith(
      expect.objectContaining({ project_id: `${YIMI},project-deleted,unassigned` }),
    );
  });

  it("丢掉失败（候选在别处已经处理了）：提示原因，墙上重新取", async () => {
    window.localStorage.setItem("meeting-workbench:view:requirementPool.tab", JSON.stringify("pending"));
    const requirementPool = vi
      .fn()
      .mockResolvedValueOnce(poolPayload({ status: "pending", items: [candidateItem()] }))
      .mockResolvedValue(poolPayload({ status: "pending", items: [] }));
    const dropCandidate = vi.fn().mockRejectedValue(new Error("这条候选已经丢掉了"));
    renderPage({ requirementPool, dropCandidate });

    const poster = await screen.findByRole("article", { name: "候选：京东仓签收凭证" });
    await userEvent.click(within(poster).getByRole("button", { name: "丢掉" }));
    await expectToast("这条候选已经丢掉了");
    expect(await screen.findByText("没有待认领的候选")).toBeInTheDocument();
    expect(requirementPool).toHaveBeenCalledTimes(2);
  });

  it("座次没存上（项目在别处改过）：提示后重新取，用新的一排再拖", async () => {
    const requirementPool = vi.fn().mockResolvedValue(poolPayload());
    const saveProjectSeats = vi.fn().mockRejectedValue(new Error("项目不存在：project-a44ff42eac0740c6"));
    renderPage({ requirementPool, saveProjectSeats });
    await screen.findByRole("article", { name: "需求：京东科研仓对接" });

    await userEvent.click(screen.getByRole("button", { name: /未排座次/ }));
    await userEvent.click(screen.getByRole("button", { name: "排入座次" }));
    await expectToast("座次没保存：项目有变化，已刷新，请再拖一次");
    expect(screen.getByRole("status")).not.toHaveTextContent("project-a44ff42eac0740c6");
    await waitFor(() => expect(requirementPool).toHaveBeenCalledTimes(2));
  });

  it("换页签那次没取到：不把上一个页签的海报留在新页签下，报错并能重试", async () => {
    const requirementPool = vi
      .fn()
      .mockResolvedValueOnce(poolPayload())
      .mockRejectedValueOnce(new ApiError("网络抖了一下", 0, null))
      .mockResolvedValue(poolPayload({ status: "done", items: [] }));
    renderPage({ requirementPool });
    await screen.findByRole("article", { name: "需求：京东科研仓对接" });

    await userEvent.click(screen.getByRole("tab", { name: /已完成/ }));
    expect(await screen.findByRole("alert")).toHaveTextContent("需求池读取失败");
    expect(screen.getByRole("tab", { name: /已完成/ })).toHaveAttribute("aria-selected", "true");
    expect(screen.queryByRole("article", { name: "需求：京东科研仓对接" })).toBeNull();

    await userEvent.click(within(screen.getByRole("alert")).getByRole("button", { name: "重试" }));
    expect(await screen.findByText("还没有已完成的需求")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("同一页签里重新取没取到（比如丢掉以后）：墙留着，但要说一声没刷新成功", async () => {
    window.localStorage.setItem("meeting-workbench:view:requirementPool.tab", JSON.stringify("pending"));
    const requirementPool = vi
      .fn()
      .mockResolvedValueOnce(poolPayload({ status: "pending", items: [candidateItem()] }))
      .mockRejectedValue(new ApiError("网络抖了一下", 0, null));
    const dropCandidate = vi.fn().mockResolvedValue({});
    renderPage({ requirementPool, dropCandidate });

    const poster = await screen.findByRole("article", { name: "候选：京东仓签收凭证" });
    await userEvent.click(within(poster).getByRole("button", { name: "丢掉" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("没刷新成功");
    expect(screen.getByRole("article", { name: "候选：京东仓签收凭证" })).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------- 需求池改版收尾（261001 版 PRD）

/* 复制出去的背景：来源原话取自京东科研仓对接那场会的逐字稿（00:13:45，825270 ms），和 poolFixtures 同一份 */
const JD_BACKGROUND =
  "# 京东科研仓对接（医米科研用药 · 需求 · P0 · 进行中）\n\n## 来源原话\n\n- 00:13:45 " + JD_SOURCE.quote + "\n";
const JD_PATHS = [
  "/Volumes/资料盘/会议纪要与录音/260916 医米京东科研仓系统对接",
  "/Volumes/资料盘/医朵云/医米科研用药/对接京东科研仓",
];

function contextOf(overrides: Partial<RequirementContext> = {}): RequirementContext {
  return { markdown: JD_BACKGROUND, paths: JD_PATHS, cards_missing: 0, ...overrides };
}

describe("RequirementPoolPage「接下」（R02-9，S05-b，R02-9 的轻提示）", () => {
  afterEach(() => uninstallClipboard());

  function renderWall(api: Partial<ApiClient>) {
    const requirementPool = vi.fn().mockResolvedValue(poolPayload());
    const handlers = renderPage({ requirementPool, ...api });
    return handlers;
  }

  it("点「接下」：背景还没取到就在点击的那一下写剪贴板；取到后复制的是这条需求的背景，提示「已复制需求背景和 2 个文件路径」，按钮变「已复制」；不进详情、不改状态", async () => {
    const stubs = installClipboard();
    let finish: (context: RequirementContext) => void = () => undefined;
    const requirementContext = vi.fn(() => new Promise<RequirementContext>((resolve) => (finish = resolve)));
    const updateRequirement = vi.fn();
    const handlers = renderWall({ requirementContext, updateRequirement } as Partial<ApiClient>);
    const poster = await screen.findByRole("article", { name: "需求：京东科研仓对接" });

    fireEvent.click(within(poster).getByRole("button", { name: /接下/ }));
    // 点击处理函数一返回：接口发出去了，剪贴板也已经在写（内容是个还没兑现的 Promise）
    expect(requirementContext).toHaveBeenCalledWith("requirement-jd");
    expect(stubs.write).toHaveBeenCalledTimes(1);
    expect(within(poster).getByRole("button", { name: "复制中…" })).toBeDisabled();

    await act(async () => finish(contextOf()));
    expect(await writtenText(stubs.write)).toBe(JD_BACKGROUND);
    await expectToast("已复制需求背景和 2 个文件路径，去 Claude Code 粘贴");
    expect(within(poster).getByRole("button", { name: "已复制" })).toBeInTheDocument();
    expect(handlers.onOpenRequirement).not.toHaveBeenCalled();
    expect(updateRequirement).not.toHaveBeenCalled();
  });

  it("浏览器没有 ClipboardItem：等背景到了用 writeText 复制，同样提示、按钮同样变「已复制」", async () => {
    const stubs = installClipboard({}, false);
    const requirementContext = vi.fn().mockResolvedValue(contextOf({ paths: [JD_PATHS[0]] }));
    renderWall({ requirementContext } as Partial<ApiClient>);

    const poster = await screen.findByRole("article", { name: "需求：京东科研仓对接" });
    fireEvent.click(within(poster).getByRole("button", { name: /接下/ }));

    await expectToast("已复制需求背景和 1 个文件路径，去 Claude Code 粘贴");
    expect(stubs.write).not.toHaveBeenCalled();
    expect(stubs.writeText).toHaveBeenCalledWith(JD_BACKGROUND);
    expect(within(poster).getByRole("button", { name: "已复制" })).toBeInTheDocument();
  });

  it("手势里的写入被拒：退回 writeText，通了就照常提示", async () => {
    const stubs = installClipboard({ write: vi.fn().mockRejectedValue(new DOMException("denied", "NotAllowedError")) });
    renderWall({ requirementContext: vi.fn().mockResolvedValue(contextOf()) } as Partial<ApiClient>);

    const poster = await screen.findByRole("article", { name: "需求：京东科研仓对接" });
    fireEvent.click(within(poster).getByRole("button", { name: /接下/ }));

    await expectToast("已复制需求背景和 2 个文件路径，去 Claude Code 粘贴");
    expect(stubs.writeText).toHaveBeenCalledWith(JD_BACKGROUND);
  });

  it("两条路都不通：提示失败原因和出路，按钮回到「接下」，不假装复制了", async () => {
    installClipboard({
      write: vi.fn().mockRejectedValue(new DOMException("denied", "NotAllowedError")),
      writeText: vi.fn().mockRejectedValue(new DOMException("denied", "NotAllowedError")),
    });
    renderWall({ requirementContext: vi.fn().mockResolvedValue(contextOf()) } as Partial<ApiClient>);

    const poster = await screen.findByRole("article", { name: "需求：京东科研仓对接" });
    fireEvent.click(within(poster).getByRole("button", { name: /接下/ }));

    await expectToast("没复制成功：浏览器不让写剪贴板，到需求详情里点「复制给 Claude Code」再试");
    await waitFor(() => expect(within(poster).getByRole("button", { name: /接下/ })).toBeEnabled());
    expect(within(poster).queryByRole("button", { name: /已复制/ })).not.toBeInTheDocument();
  });

  it("取背景失败：提示「没复制成功：」加接口给的原因，不写剪贴板的备用路径", async () => {
    const stubs = installClipboard();
    const requirementContext = vi.fn().mockRejectedValue(new ApiError("需求不存在：requirement-jd", 404, { detail: "需求不存在：requirement-jd" }));
    renderWall({ requirementContext } as Partial<ApiClient>);

    const poster = await screen.findByRole("article", { name: "需求：京东科研仓对接" });
    fireEvent.click(within(poster).getByRole("button", { name: /接下/ }));

    await expectToast("没复制成功：需求不存在：requirement-jd");
    expect(stubs.writeText).not.toHaveBeenCalled();
    expect(within(poster).getByRole("button", { name: /接下/ })).toBeEnabled();
  });

  it("没有任何文件路径的需求：提示不说「0 个文件路径」", async () => {
    installClipboard();
    renderWall({ requirementContext: vi.fn().mockResolvedValue(contextOf({ paths: [] })) } as Partial<ApiClient>);

    const poster = await screen.findByRole("article", { name: "需求：京东科研仓对接" });
    fireEvent.click(within(poster).getByRole("button", { name: /接下/ }));
    await expectToast("已复制需求背景，去 Claude Code 粘贴");
  });
});

describe("RequirementPoolPage 丢掉候选后的撤销（R01-15，S01-b）", () => {
  beforeEach(() => {
    window.localStorage.setItem("meeting-workbench:view:requirementPool.tab", JSON.stringify("pending"));
  });

  const pending = () => poolPayload({ status: "pending", items: [candidateItem()] });
  const empty = () => poolPayload({ status: "pending", items: [], dropped_count: 1 });

  it("提示「已丢掉「X」，30 天内可在已丢掉里撤销」带「撤销」：点了调 restoreCandidate，候选回到待认领，墙上重新取", async () => {
    const requirementPool = vi.fn().mockResolvedValueOnce(pending()).mockResolvedValueOnce(empty()).mockResolvedValue(pending());
    const dropCandidate = vi.fn().mockResolvedValue({});
    const restoreCandidate = vi.fn().mockResolvedValue({});
    renderPage({ requirementPool, dropCandidate, restoreCandidate });

    const poster = await screen.findByRole("article", { name: "候选：京东仓签收凭证" });
    fireEvent.click(within(poster).getByRole("button", { name: "丢掉" }));
    await expectToast("已丢掉「京东仓签收凭证」，30 天内可在已丢掉里撤销");
    expect(await screen.findByText("没有待认领的候选")).toBeInTheDocument();

    fireEvent.click(within(screen.getByRole("status")).getByRole("button", { name: "撤销" }));
    await waitFor(() => expect(restoreCandidate).toHaveBeenCalledWith("candidate-receipt"));
    await expectToast("「京东仓签收凭证」回到待认领了");
    expect(await screen.findByRole("article", { name: "候选：京东仓签收凭证" })).toBeInTheDocument();
    expect(within(screen.getByRole("status")).queryByRole("button", { name: "撤销" })).not.toBeInTheDocument();
  });

  it("撤销失败（比如在别处已经撤销了）：提示后端给的原因，墙上换成最新的", async () => {
    const requirementPool = vi.fn().mockResolvedValueOnce(pending()).mockResolvedValue(empty());
    const dropCandidate = vi.fn().mockResolvedValue({});
    const restoreCandidate = vi.fn().mockRejectedValue(new ApiError("这条候选不在已丢掉里", 409, { detail: "这条候选不在已丢掉里" }));
    renderPage({ requirementPool, dropCandidate, restoreCandidate });

    const poster = await screen.findByRole("article", { name: "候选：京东仓签收凭证" });
    fireEvent.click(within(poster).getByRole("button", { name: "丢掉" }));
    await expectToast("已丢掉");
    fireEvent.click(within(screen.getByRole("status")).getByRole("button", { name: "撤销" }));

    await expectToast("这条候选不在已丢掉里");
    await waitFor(() => expect(requirementPool).toHaveBeenCalledTimes(3));
  });

  it("10 秒的撤销窗口里换了页签：撤销之后刷新用的是现在的页签，不是点「丢掉」那一刻的", async () => {
    const requirementPool = vi.fn().mockImplementation(async (filters: { status: string }) =>
      filters.status === "pending" ? pending() : poolPayload({ status: filters.status as "active", items: [] }),
    );
    const dropCandidate = vi.fn().mockResolvedValue({});
    const restoreCandidate = vi.fn().mockResolvedValue({});
    renderPage({ requirementPool, dropCandidate, restoreCandidate });

    const poster = await screen.findByRole("article", { name: "候选：京东仓签收凭证" });
    fireEvent.click(within(poster).getByRole("button", { name: "丢掉" }));
    await expectToast("已丢掉");
    fireEvent.click(screen.getByRole("tab", { name: /进行中/ }));
    await waitFor(() => expect(requirementPool).toHaveBeenLastCalledWith(expect.objectContaining({ status: "active" })));

    fireEvent.click(within(screen.getByRole("status")).getByRole("button", { name: "撤销" }));
    await waitFor(() => expect(restoreCandidate).toHaveBeenCalledTimes(1));
    await expectToast("回到待认领了");
    await waitFor(() => expect(requirementPool.mock.calls.length).toBeGreaterThanOrEqual(4));
    expect(requirementPool).toHaveBeenLastCalledWith(expect.objectContaining({ status: "active" }));
  });
});

describe("RequirementPoolPage 合并后的撤销（R01-14，S03-b）", () => {
  const MERGE_TARGETS = {
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
    ],
  };

  async function mergeFromWall(api: Partial<ApiClient>) {
    window.localStorage.setItem("meeting-workbench:view:requirementPool.tab", JSON.stringify("pending"));
    const requirementPool = vi.fn().mockResolvedValue(poolPayload({ status: "pending", items: [candidateItem()] }));
    const candidateMergeTargets = vi.fn().mockResolvedValue(MERGE_TARGETS);
    const mergeCandidate = vi.fn().mockResolvedValue(requirementItem());
    const handlers = renderPage({ requirementPool, candidateMergeTargets, mergeCandidate, ...api });
    const poster = await screen.findByRole("article", { name: "候选：京东仓签收凭证" });
    fireEvent.click(within(poster).getByRole("button", { name: "合并" }));
    const dialog = await screen.findByRole("dialog", { name: "合并到已有需求" });
    await within(dialog).findByRole("radio", { name: /京东科研仓对接/ });
    fireEvent.click(within(dialog).getByRole("button", { name: "合并" }));
    await expectToast("已合并到「京东科研仓对接」，这场会和原话已加进去");
    return { requirementPool, handlers };
  }

  it("在需求池里合并：提示「已合并到「X」，这场会和原话已加进去」带「撤销」；点了调 undoCandidateMerge，成功后刷新墙并提示「已撤销合并」", async () => {
    const undoCandidateMerge = vi.fn().mockResolvedValue({});
    const { requirementPool, handlers } = await mergeFromWall({ undoCandidateMerge } as Partial<ApiClient>);
    const callsBefore = requirementPool.mock.calls.length;

    fireEvent.click(within(screen.getByRole("status")).getByRole("button", { name: "撤销" }));
    await waitFor(() => expect(undoCandidateMerge).toHaveBeenCalledWith("candidate-receipt"));
    await expectToast("已撤销合并");
    await waitFor(() => expect(requirementPool.mock.calls.length).toBeGreaterThan(callsBefore));
    // 合并、撤销各刷新一次项目列表
    await waitFor(() => expect(handlers.onProjectsChanged).toHaveBeenCalledTimes(2));
    expect(within(screen.getByRole("status")).queryByRole("button", { name: "撤销" })).not.toBeInTheDocument();
  });

  it("撤销合并时有任务这 10 分钟里改挂到了别处、没有退回：提示里说一声", async () => {
    const undoCandidateMerge = vi.fn().mockResolvedValue({ kept_task_count: 1 });
    await mergeFromWall({ undoCandidateMerge } as Partial<ApiClient>);

    fireEvent.click(within(screen.getByRole("status")).getByRole("button", { name: "撤销" }));

    await expectToast("已撤销合并；有 1 条任务已经挂到别处，没有退回");
  });

  it("撤销失败（比如合并已经超过 10 分钟）：提示后端返回的原因，墙上换成最新的", async () => {
    const undoCandidateMerge = vi
      .fn()
      .mockRejectedValue(new ApiError("合并超过 10 分钟，不能撤销了", 409, { detail: "合并超过 10 分钟，不能撤销了" }));
    const { requirementPool } = await mergeFromWall({ undoCandidateMerge } as Partial<ApiClient>);
    const callsBefore = requirementPool.mock.calls.length;

    fireEvent.click(within(screen.getByRole("status")).getByRole("button", { name: "撤销" }));
    await expectToast("合并超过 10 分钟，不能撤销了");
    await waitFor(() => expect(requirementPool.mock.calls.length).toBeGreaterThan(callsBefore));
  });

  it("从认领页合并回来（App 带来的 flash 有 undoMergeCandidateId）：提示同样带「撤销」，点了调 undoCandidateMerge", async () => {
    const requirementPool = vi.fn().mockResolvedValue(poolPayload({ status: "pending", items: [candidateItem()] }));
    const undoCandidateMerge = vi.fn().mockResolvedValue({});
    renderPage(
      { requirementPool, undoCandidateMerge },
      { flash: { message: "已合并到「京东科研仓对接」，这场会和原话已加进去", undoMergeCandidateId: "candidate-receipt" } },
    );

    await expectToast("已合并到「京东科研仓对接」，这场会和原话已加进去");
    fireEvent.click(within(screen.getByRole("status")).getByRole("button", { name: "撤销" }));
    await waitFor(() => expect(undoCandidateMerge).toHaveBeenCalledWith("candidate-receipt"));
    await expectToast("已撤销合并");
  });

  it("flash 没有 undoMergeCandidateId（认领、新建）：提示上没有「撤销」", async () => {
    const requirementPool = vi.fn().mockResolvedValue(poolPayload());
    renderPage({ requirementPool }, { flash: { message: "需求已创建" } });

    await expectToast("需求已创建");
    expect(within(screen.getByRole("status")).queryByRole("button", { name: "撤销" })).not.toBeInTheDocument();
  });
});

describe("RequirementPoolPage 认领后的高亮（R01-13，S02-c）", () => {
  const scrollIntoView = vi.fn();

  // 刚认领的那条：云课堂的科室会预约后台导出，P2；来源原话是 00:09:36 那句（576900 ms）
  const claimed = requirementItem({
    id: "requirement-export",
    title: "科室会预约后台导出",
    summary: "预约审核页现在不能导出 Excel，运营要把科室会预约名单拉出来对表。",
    priority: "P2",
    project_id: CVM,
    project_name: "CVM 云讲堂",
    project_seat: null,
    source: EXPORT_SOURCE,
    meeting_count: 1,
    follow_up_count: 0,
    folder_count: 0,
  });
  const wall = () => poolPayload({ items: [requirementItem(), claimed], total: 2 });

  beforeEach(() => {
    Element.prototype.scrollIntoView = scrollIntoView;
    vi.useFakeTimers({ shouldAdvanceTime: true });
  });
  afterEach(() => {
    vi.useRealTimers();
    Reflect.deleteProperty(Element.prototype, "scrollIntoView");
    scrollIntoView.mockReset();
  });

  it("flash 带着 highlightId：那张海报滚进视野、描边 3 秒，同时提示 flash.message", async () => {
    const requirementPool = vi.fn().mockResolvedValue(wall());
    renderPage({ requirementPool }, { flash: { message: "已认领「科室会预约后台导出」，挂上墙了", highlightId: "requirement-export" } });

    const poster = await screen.findByRole("article", { name: "需求：科室会预约后台导出" });
    await waitFor(() => expect(poster).toHaveClass("poster--highlight"));
    expect(screen.getByRole("article", { name: "需求：京东科研仓对接" })).not.toHaveClass("poster--highlight");
    expect(scrollIntoView).toHaveBeenCalledTimes(1);
    expect(scrollIntoView).toHaveBeenCalledWith(expect.objectContaining({ block: "center" }));
    expect(scrollIntoView.mock.contexts[0]).toBe(poster);
    await expectToast("已认领「科室会预约后台导出」，挂上墙了");

    act(() => {
      vi.advanceTimersByTime(2_900);
    });
    expect(poster).toHaveClass("poster--highlight");
    act(() => {
      vi.advanceTimersByTime(200);
    });
    expect(poster).not.toHaveClass("poster--highlight");
  });

  it("墙面还没取到时先记着：取到了才描边，3 秒从描边那一刻起算", async () => {
    let arrive: (payload: ReturnType<typeof wall>) => void = () => undefined;
    const requirementPool = vi.fn(() => new Promise<ReturnType<typeof wall>>((resolve) => (arrive = resolve)));
    renderPage({ requirementPool }, { flash: { message: "已认领「科室会预约后台导出」，挂上墙了", highlightId: "requirement-export" } });

    // 墙面要等 5 秒才回来：这 5 秒里不会有描边，也不会把 3 秒耗掉
    act(() => {
      vi.advanceTimersByTime(5_000);
    });
    expect(scrollIntoView).not.toHaveBeenCalled();

    await act(async () => arrive(wall()));
    const poster = await screen.findByRole("article", { name: "需求：科室会预约后台导出" });
    await waitFor(() => expect(poster).toHaveClass("poster--highlight"));
    act(() => {
      vi.advanceTimersByTime(2_900);
    });
    expect(poster).toHaveClass("poster--highlight");
    act(() => {
      vi.advanceTimersByTime(200);
    });
    expect(poster).not.toHaveClass("poster--highlight");
  });

  it("要高亮的海报不在当前列表里（筛掉了、在别的页签）：不报错、不描边，提示照常", async () => {
    const requirementPool = vi.fn().mockResolvedValue(poolPayload());
    renderPage({ requirementPool }, { flash: { message: "已认领「科室会预约后台导出」，挂上墙了", highlightId: "requirement-export" } });

    await screen.findByRole("article", { name: "需求：京东科研仓对接" });
    await expectToast("已认领「科室会预约后台导出」，挂上墙了");
    expect(document.querySelector(".poster--highlight")).toBeNull();
    expect(scrollIntoView).not.toHaveBeenCalled();
  });

  it("flash 没有 highlightId（合并回来、座次保存）：不滚动、不描边", async () => {
    const requirementPool = vi.fn().mockResolvedValue(wall());
    renderPage({ requirementPool }, { flash: { message: "已合并到「京东科研仓对接」，这场会和原话已加进去" } });

    await screen.findByRole("article", { name: "需求：科室会预约后台导出" });
    expect(document.querySelector(".poster--highlight")).toBeNull();
    expect(scrollIntoView).not.toHaveBeenCalled();
  });
});

// ---------------------------------------------------------------- 需求并需求 + 卡片改状态（261009）

async function openPosterMenu(title = "京东科研仓对接") {
  const poster = await screen.findByRole("article", { name: `需求：${title}` });
  await userEvent.click(within(poster).getByRole("button", { name: `更多操作：${title}` }));
  return poster;
}

async function expectAlertToast(text: string) {
  await waitFor(() => expect(document.querySelector(".app-toast--error")).toHaveTextContent(text));
}

/** 详情接口的返回：海报上的字段之外，改状态只用到 title、status、closed_task_count、tasks */
function detailOf(item: PoolItem, overrides: Partial<RequirementDetail> = {}): RequirementDetail {
  return {
    id: item.id,
    project_id: item.project_id ?? YIMI,
    project_name: item.project_name ?? "",
    project_color: item.project_color ?? "",
    title: item.title,
    priority: item.priority ?? "P2",
    status: item.status === "pending" ? "active" : item.status,
    created_at: item.created_at,
    updated_at: item.updated_at,
    open_task_count: item.open_task_count,
    meeting_count: item.meeting_count,
    latest_meeting_date: item.latest_meeting_date,
    folder_count: item.folder_count,
    folders: [],
    meetings: [],
    tasks: [],
    closed_task_count: 0,
    ...overrides,
  };
}

describe("RequirementPoolPage 卡片改状态（D9～D11、D14、D16）", () => {
  const ACTIVE_COUNTS = { pending: 3, active: 9, done: 1, shelved: 1, all: 14 };
  const OPEN_TASKS = [
    taskItem(),
    taskItem({ id: "task-fields", title: "整理四类接口的字段对照表", status: "in_progress" }),
    taskItem({ id: "task-kickoff", title: "约京东科研仓开对接启动会", status: "done" }),
    taskItem({ id: "task-old", title: "评估顺丰冷链替代方案", status: "cancelled" }),
  ];

  it("没有没做完的待办：「⋯」里标记完成直接改，不弹确认；提示「已完成「X」」带［撤销］，卡片离开进行中墙、页签计数跟着变", async () => {
    const item = requirementItem();
    const requirementPool = vi
      .fn()
      .mockResolvedValueOnce(poolPayload())
      .mockResolvedValue(poolPayload({ items: [], total: 0, counts: { ...ACTIVE_COUNTS, active: 8, done: 2 } }));
    const updateRequirement = vi.fn().mockResolvedValue(detailOf(item, { status: "done" }));
    const requirement = vi.fn();
    const handlers = renderPage({ requirementPool, updateRequirement, requirement });

    await openPosterMenu();
    await userEvent.click(screen.getByRole("menuitem", { name: "标记完成" }));

    expect(updateRequirement).toHaveBeenCalledWith("requirement-jd", { status: "done" });
    expect(requirement).not.toHaveBeenCalled();
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
    await expectToast("已完成「京东科研仓对接」");
    expect(within(screen.getByRole("status")).getByRole("button", { name: "撤销" })).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByRole("article", { name: "需求：京东科研仓对接" })).not.toBeInTheDocument());
    expect(screen.getByRole("tab", { name: /进行中/ })).toHaveTextContent("进行中8");
    expect(screen.getByRole("tab", { name: /已完成/ })).toHaveTextContent("已完成2");
    expect(requirementPool).toHaveBeenLastCalledWith(expect.objectContaining({ status: "active" }));
    expect(handlers.onOpenRequirement).not.toHaveBeenCalled();
    await waitFor(() => expect(handlers.onProjectsChanged).toHaveBeenCalledTimes(1));
  });

  it("撤销（D11）：调 status-undo，提示「「X」回到进行中了」，卡片回到墙上", async () => {
    const item = requirementItem();
    const requirementPool = vi
      .fn()
      .mockResolvedValueOnce(poolPayload())
      .mockResolvedValueOnce(poolPayload({ items: [], total: 0, counts: { ...ACTIVE_COUNTS, active: 8, done: 2 } }))
      .mockResolvedValue(poolPayload());
    const updateRequirement = vi.fn().mockResolvedValue(detailOf(item, { status: "done" }));
    const undoRequirementStatus = vi.fn().mockResolvedValue(detailOf(item, { status: "active" }));
    renderPage({ requirementPool, updateRequirement, undoRequirementStatus });

    await openPosterMenu();
    await userEvent.click(screen.getByRole("menuitem", { name: "标记完成" }));
    await expectToast("已完成「京东科研仓对接」");
    await waitFor(() => expect(screen.queryByRole("article", { name: "需求：京东科研仓对接" })).not.toBeInTheDocument());

    await userEvent.click(within(screen.getByRole("status")).getByRole("button", { name: "撤销" }));
    expect(undoRequirementStatus).toHaveBeenCalledWith("requirement-jd");
    await expectToast("「京东科研仓对接」回到进行中了");
    expect(await screen.findByRole("article", { name: "需求：京东科研仓对接" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: /进行中/ })).toHaveTextContent("进行中9");
  });

  it("撤销被拒（过了 10 分钟）：红色提示后端给的原因，墙上换成最新的", async () => {
    const item = requirementItem();
    const requirementPool = vi.fn().mockResolvedValue(poolPayload());
    const updateRequirement = vi.fn().mockResolvedValue(detailOf(item, { status: "done" }));
    const undoRequirementStatus = vi
      .fn()
      .mockRejectedValue(new ApiError("改状态超过 10 分钟，不能撤销了", 409, { detail: "改状态超过 10 分钟，不能撤销了" }));
    renderPage({ requirementPool, updateRequirement, undoRequirementStatus });

    await openPosterMenu();
    await userEvent.click(screen.getByRole("menuitem", { name: "标记完成" }));
    await expectToast("已完成「京东科研仓对接」");
    const calls = requirementPool.mock.calls.length;
    await userEvent.click(within(screen.getByRole("status")).getByRole("button", { name: "撤销" }));
    await expectAlertToast("改状态超过 10 分钟，不能撤销了");
    await waitFor(() => expect(requirementPool.mock.calls.length).toBeGreaterThan(calls));
  });

  it("有没做完的待办（D10）：先弹「还有 N 条待办没做完」列出标题；「一起关掉」是默认，回车就发 close_open_tasks: true", async () => {
    const item = requirementItem({ open_task_count: 2, task_count: 4 });
    const requirementPool = vi.fn().mockResolvedValue(poolPayload({ items: [item] }));
    const requirement = vi.fn().mockResolvedValue(detailOf(item, { tasks: OPEN_TASKS }));
    const updateRequirement = vi.fn().mockResolvedValue(detailOf(item, { status: "done", closed_task_count: 2 }));
    renderPage({ requirementPool, requirement, updateRequirement });

    await openPosterMenu();
    await userEvent.click(screen.getByRole("menuitem", { name: "标记完成" }));

    const dialog = await screen.findByRole("alertdialog", { name: "还有 2 条待办没做完" });
    expect(requirement).toHaveBeenCalledWith("requirement-jd");
    expect(within(dialog).getAllByRole("listitem").map((row) => row.textContent)).toEqual([
      "跟京东确认签收凭证怎么回传给医米",
      "整理四类接口的字段对照表",
    ]);
    expect(within(dialog).getByText("关掉＝记为已取消，待办页不再挂着")).toBeInTheDocument();
    const closeAll = within(dialog).getByRole("button", { name: "一起关掉" });
    expect(closeAll).toHaveClass("confirm-modal__primary");
    expect(updateRequirement).not.toHaveBeenCalled();
    await waitFor(() => expect(closeAll).toHaveFocus());

    await userEvent.keyboard("{Enter}");
    expect(updateRequirement).toHaveBeenCalledWith("requirement-jd", { status: "done", close_open_tasks: true });
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
    await expectToast("已完成「京东科研仓对接」，一起关掉 2 条待办");
  });

  it("有没做完的待办时搁置，点［待办留着］：发 close_open_tasks: false，提示「已搁置「X」」", async () => {
    const item = requirementItem({ open_task_count: 2, task_count: 4 });
    const requirementPool = vi.fn().mockResolvedValue(poolPayload({ items: [item] }));
    const requirement = vi.fn().mockResolvedValue(detailOf(item, { tasks: OPEN_TASKS }));
    const updateRequirement = vi.fn().mockResolvedValue(detailOf(item, { status: "shelved" }));
    renderPage({ requirementPool, requirement, updateRequirement });

    await openPosterMenu();
    await userEvent.click(screen.getByRole("menuitem", { name: "搁置" }));
    const dialog = await screen.findByRole("alertdialog", { name: "还有 2 条待办没做完" });
    await userEvent.click(within(dialog).getByRole("button", { name: "待办留着" }));

    expect(updateRequirement).toHaveBeenCalledWith("requirement-jd", { status: "shelved", close_open_tasks: false });
    await expectToast("已搁置「京东科研仓对接」");
  });

  it("确认弹窗里点 ✕、按 Esc：什么都不改", async () => {
    const item = requirementItem({ open_task_count: 2, task_count: 4 });
    const requirementPool = vi.fn().mockResolvedValue(poolPayload({ items: [item] }));
    const requirement = vi.fn().mockResolvedValue(detailOf(item, { tasks: OPEN_TASKS }));
    const updateRequirement = vi.fn();
    renderPage({ requirementPool, requirement, updateRequirement });

    await openPosterMenu();
    await userEvent.click(screen.getByRole("menuitem", { name: "标记完成" }));
    let dialog = await screen.findByRole("alertdialog", { name: "还有 2 条待办没做完" });
    await userEvent.click(within(dialog).getByRole("button", { name: "关闭" }));
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();

    await openPosterMenu();
    await userEvent.click(screen.getByRole("menuitem", { name: "标记完成" }));
    dialog = await screen.findByRole("alertdialog", { name: "还有 2 条待办没做完" });
    await userEvent.keyboard("{Escape}");
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
    expect(updateRequirement).not.toHaveBeenCalled();
  });

  it("海报上的数是旧的、待办在别处已经做完了：照没有待办直接改", async () => {
    const item = requirementItem({ open_task_count: 1, task_count: 2 });
    const requirementPool = vi.fn().mockResolvedValue(poolPayload({ items: [item] }));
    const requirement = vi.fn().mockResolvedValue(detailOf(item, { tasks: [taskItem({ status: "done" })] }));
    const updateRequirement = vi.fn().mockResolvedValue(detailOf(item, { status: "done" }));
    renderPage({ requirementPool, requirement, updateRequirement });

    await openPosterMenu();
    await userEvent.click(screen.getByRole("menuitem", { name: "标记完成" }));
    await waitFor(() => expect(updateRequirement).toHaveBeenCalledWith("requirement-jd", { status: "done" }));
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
  });

  it("已完成页签的卡：「⋯」里重新打开，提示「已重新打开「X」」", async () => {
    window.localStorage.setItem("meeting-workbench:view:requirementPool.tab", JSON.stringify("done"));
    const item = requirementItem({ status: "done", status_changed_at: "2026-10-08T17:30:00+00:00" });
    const requirementPool = vi.fn().mockResolvedValue(poolPayload({ status: "done", items: [item] }));
    const updateRequirement = vi.fn().mockResolvedValue(detailOf(item, { status: "active" }));
    renderPage({ requirementPool, updateRequirement });

    const poster = await openPosterMenu();
    expect(within(poster).getByText("10-09").parentElement).toHaveTextContent("10-09 完成");
    await userEvent.click(screen.getByRole("menuitem", { name: "重新打开" }));
    expect(updateRequirement).toHaveBeenCalledWith("requirement-jd", { status: "active" });
    await expectToast("已重新打开「京东科研仓对接」");
  });

  it("没改成：红色提示后端给的原因，墙上重新取", async () => {
    const requirementPool = vi.fn().mockResolvedValue(poolPayload());
    const updateRequirement = vi.fn().mockRejectedValue(new ApiError("需求不存在", 404, { detail: "需求不存在" }));
    renderPage({ requirementPool, updateRequirement });

    await openPosterMenu();
    const calls = requirementPool.mock.calls.length;
    await userEvent.click(screen.getByRole("menuitem", { name: "标记完成" }));
    await expectAlertToast("需求不存在");
    await waitFor(() => expect(requirementPool.mock.calls.length).toBeGreaterThan(calls));
  });

  it("「待办都清了，完成了吗？」的［标记完成］（D14）：没做完的为 0，直接改", async () => {
    const item = requirementItem({ task_count: 3, open_task_count: 0 });
    const requirementPool = vi.fn().mockResolvedValue(poolPayload({ items: [item] }));
    const updateRequirement = vi.fn().mockResolvedValue(detailOf(item, { status: "done" }));
    const handlers = renderPage({ requirementPool, updateRequirement });

    const poster = await screen.findByRole("article", { name: "需求：京东科研仓对接" });
    expect(within(poster).getByText("待办都清了，完成了吗？")).toBeInTheDocument();
    await userEvent.click(within(poster).getByRole("button", { name: "标记完成" }));
    expect(updateRequirement).toHaveBeenCalledWith("requirement-jd", { status: "done" });
    await expectToast("已完成「京东科研仓对接」");
    expect(handlers.onOpenRequirement).not.toHaveBeenCalled();
  });

  it("「已搁置」页签的空状态指向卡片「⋯」里的搁置（F5）", async () => {
    window.localStorage.setItem("meeting-workbench:view:requirementPool.tab", JSON.stringify("shelved"));
    renderPage({ requirementPool: vi.fn().mockResolvedValue(poolPayload({ status: "shelved", items: [], total: 0 })) });

    expect(await screen.findByText("还没有搁置的需求")).toBeInTheDocument();
    expect(screen.getByText("暂时不跟进的需求，在海报右下的「⋯」里点搁置")).toBeInTheDocument();
    expect(screen.queryByText(/修改需求里改成已搁置/)).not.toBeInTheDocument();
  });

  it("「已完成」页签的空状态指向卡片「⋯」里的标记完成（D16）", async () => {
    window.localStorage.setItem("meeting-workbench:view:requirementPool.tab", JSON.stringify("done"));
    renderPage({ requirementPool: vi.fn().mockResolvedValue(poolPayload({ status: "done", items: [], total: 0 })) });

    expect(await screen.findByText("还没有已完成的需求")).toBeInTheDocument();
    expect(screen.getByText("做完的需求，在海报右下的「⋯」里点标记完成")).toBeInTheDocument();
    expect(screen.queryByText(/修改需求里改成已完成/)).not.toBeInTheDocument();
  });
});

describe("RequirementPoolPage 并入其他需求（D4、D7）", () => {
  async function openMergeDialog(api: Partial<ApiClient>) {
    const requirementPool = vi.fn().mockResolvedValue(poolPayload());
    const requirementMergeTargets = vi.fn().mockResolvedValue(mergeTargetsPayload());
    const handlers = renderPage({ requirementPool, requirementMergeTargets, ...api });
    await openPosterMenu();
    await userEvent.click(screen.getByRole("menuitem", { name: "并入其他需求…" }));
    const dialog = await screen.findByRole("dialog", { name: "把「京东科研仓对接」并入" });
    await within(dialog).findByRole("radio", { name: /京东仓签收凭证/ });
    return { dialog, requirementPool, requirementMergeTargets, handlers };
  }

  function mergeResult(overrides: Partial<RequirementMergeResult> = {}): RequirementMergeResult {
    return {
      ...detailOf(requirementItem({ id: "requirement-receipt", title: "京东仓签收凭证", priority: "P1" })),
      merged_from: { id: "requirement-jd", title: "京东科研仓对接", undo_until: "2026-10-09T03:10:00+00:00" },
      ...overrides,
    };
  }

  it("弹窗：标题、搜索框，列同项目的其他需求（三种状态），每行需求名、等级、状态、待办数、会议数；没选时不预览、［并入］置灰", async () => {
    const { dialog, requirementMergeTargets } = await openMergeDialog({});

    expect(requirementMergeTargets).toHaveBeenCalledWith("requirement-jd", undefined);
    expect(within(dialog).getByRole("heading", { name: "把「京东科研仓对接」并入" })).toBeInTheDocument();
    expect(within(dialog).getByRole("textbox", { name: "搜需求名称" })).toBeInTheDocument();
    expect(within(dialog).getByText("医米科研用药里的其他需求")).toBeInTheDocument();
    const rows = within(dialog).getAllByRole("radio").map((radio) => radio.closest("label")!.textContent);
    expect(rows).toEqual([
      "京东仓签收凭证1 待办 · 1 场会进行中P1",
      "EDC 系统选型0 待办 · 1 场会已搁置P3",
      "扫码入组强提醒0 待办 · 2 场会已完成P2",
    ]);
    expect(within(dialog).queryByText(/带过去/)).not.toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "并入" })).toBeDisabled();
  });

  it("选中后预览带过去的东西（为 0 的也写），合并后的等级取较高的", async () => {
    const { dialog } = await openMergeDialog({});

    await userEvent.click(within(dialog).getByRole("radio", { name: /京东仓签收凭证/ }));
    // 待办数和墙上海报同一口径：没做完的（F2），名下总共 3 条、没做完的 2 条
    expect(within(dialog).getByText("带过去：2 待办 · 2 场会 · 2 句原话 · 1 个文件夹")).toBeInTheDocument();
    // 这条 P2、主需求 P1：取 P1
    expect(within(dialog).getByText("合并后 P1（取较高）· 10 分钟内可撤销")).toBeInTheDocument();

    await userEvent.click(within(dialog).getByRole("radio", { name: /EDC 系统选型/ }));
    // 这条 P2、主需求 P3：取 P2
    expect(within(dialog).getByText("合并后 P2（取较高）· 10 分钟内可撤销")).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "并入" })).toBeEnabled();
  });

  it("带过去的东西都是 0 时照写 0", async () => {
    const zero = mergeTargetsPayload({
      moving: { open_task_count: 0, task_count: 0, meeting_count: 0, source_count: 0, folder_count: 0, priority: "P0" },
    });
    const { dialog } = await openMergeDialog({ requirementMergeTargets: vi.fn().mockResolvedValue(zero) });
    await userEvent.click(within(dialog).getByRole("radio", { name: /扫码入组强提醒/ }));
    expect(within(dialog).getByText("带过去：0 待办 · 0 场会 · 0 句原话 · 0 个文件夹")).toBeInTheDocument();
    expect(within(dialog).getByText("合并后 P0（取较高）· 10 分钟内可撤销")).toBeInTheDocument();
  });

  it("搜索框打完字停一下按名称查；搜不到时说一声", async () => {
    const requirementMergeTargets = vi
      .fn()
      .mockResolvedValueOnce(mergeTargetsPayload())
      .mockResolvedValueOnce(mergeTargetsPayload({ items: [mergeTargetsPayload().items[0]] }))
      .mockResolvedValue(mergeTargetsPayload({ items: [] }));
    const { dialog } = await openMergeDialog({ requirementMergeTargets });

    await userEvent.type(within(dialog).getByRole("textbox", { name: "搜需求名称" }), "签收");
    await waitFor(() => expect(requirementMergeTargets).toHaveBeenLastCalledWith("requirement-jd", "签收"));
    await waitFor(() => expect(within(dialog).getAllByRole("radio")).toHaveLength(1));
    expect(requirementMergeTargets.mock.calls.filter(([, q]) => q === "签")).toHaveLength(0);

    await userEvent.type(within(dialog).getByRole("textbox", { name: "搜需求名称" }), "回单");
    expect(await within(dialog).findByText("没有名称含「签收回单」的需求")).toBeInTheDocument();
  });

  it("并入：调 merge 接口，提示「已并入「主需求」」带［撤销］，这条从墙上消失（墙重新取）", async () => {
    const mergeRequirement = vi.fn().mockResolvedValue(mergeResult());
    const { dialog, requirementPool, handlers } = await openMergeDialog({ mergeRequirement });
    requirementPool.mockResolvedValue(poolPayload({ items: [], total: 0 }));

    await userEvent.click(within(dialog).getByRole("radio", { name: /京东仓签收凭证/ }));
    await userEvent.click(within(dialog).getByRole("button", { name: "并入" }));

    expect(mergeRequirement).toHaveBeenCalledWith("requirement-jd", "requirement-receipt");
    await expectToast("已并入「京东仓签收凭证」");
    expect(within(screen.getByRole("status")).getByRole("button", { name: "撤销" })).toBeInTheDocument();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    await waitFor(() => expect(screen.queryByRole("article", { name: "需求：京东科研仓对接" })).not.toBeInTheDocument());
    await waitFor(() => expect(handlers.onProjectsChanged).toHaveBeenCalledTimes(1));
  });

  it("撤销并入：调 unmerge（被并掉那条的 id），提示「已撤销合并」，这条回到墙上", async () => {
    const mergeRequirement = vi.fn().mockResolvedValue(mergeResult());
    const undoRequirementMerge = vi.fn().mockResolvedValue(detailOf(requirementItem()));
    const { dialog, requirementPool } = await openMergeDialog({ mergeRequirement, undoRequirementMerge });
    requirementPool.mockResolvedValueOnce(poolPayload({ items: [], total: 0 }));

    await userEvent.click(within(dialog).getByRole("radio", { name: /京东仓签收凭证/ }));
    await userEvent.click(within(dialog).getByRole("button", { name: "并入" }));
    await expectToast("已并入「京东仓签收凭证」");
    await waitFor(() => expect(screen.queryByRole("article", { name: "需求：京东科研仓对接" })).not.toBeInTheDocument());

    await userEvent.click(within(screen.getByRole("status")).getByRole("button", { name: "撤销" }));
    expect(undoRequirementMerge).toHaveBeenCalledWith("requirement-jd");
    await expectToast("已撤销合并");
    expect(await screen.findByRole("article", { name: "需求：京东科研仓对接" })).toBeInTheDocument();
  });

  it("并入失败（比如主需求刚被删）：原因写在弹窗里，弹窗不关，可以换一条再并", async () => {
    const mergeRequirement = vi.fn().mockRejectedValue(new ApiError("要并入的需求不存在", 404, { detail: "要并入的需求不存在" }));
    const { dialog } = await openMergeDialog({ mergeRequirement });

    await userEvent.click(within(dialog).getByRole("radio", { name: /京东仓签收凭证/ }));
    await userEvent.click(within(dialog).getByRole("button", { name: "并入" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("要并入的需求不存在");
    expect(within(dialog).getByRole("button", { name: "并入" })).toBeEnabled();
  });

  it("取消、Esc 关掉弹窗，不并", async () => {
    const mergeRequirement = vi.fn();
    const { dialog } = await openMergeDialog({ mergeRequirement });
    await userEvent.click(within(dialog).getByRole("button", { name: "取消" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(mergeRequirement).not.toHaveBeenCalled();
  });

  it("higherPriority：P0 最高", () => {
    expect(higherPriority("P2", "P1")).toBe("P1");
    expect(higherPriority("P0", "P3")).toBe("P0");
    expect(higherPriority("P2", "P2")).toBe("P2");
  });
});

describe("RequirementPoolPage 候选并进已完成的需求（D13）", () => {
  it("合并目标里列出已完成的；并进去后提示里说「「X」已重新打开」", async () => {
    window.localStorage.setItem("meeting-workbench:view:requirementPool.tab", JSON.stringify("pending"));
    const requirementPool = vi.fn().mockResolvedValue(poolPayload({ status: "pending", items: [candidateItem()] }));
    const candidateMergeTargets = vi.fn().mockResolvedValue({
      project_id: YIMI,
      items: [
        {
          id: "requirement-jd",
          title: "京东科研仓对接",
          status: "done",
          priority: "P0",
          meeting_title: "260916 医米京东科研仓系统对接",
          recording_date: "2026-09-16T19:01:50-07:00",
          recommended: true,
        },
      ],
    });
    const mergeCandidate = vi.fn().mockResolvedValue(detailOf(requirementItem(), { reopened: true }));
    renderPage({ requirementPool, candidateMergeTargets, mergeCandidate });

    const poster = await screen.findByRole("article", { name: "候选：京东仓签收凭证" });
    await userEvent.click(within(poster).getByRole("button", { name: "合并" }));
    const dialog = await screen.findByRole("dialog", { name: "合并到已有需求" });
    expect(within(dialog).getByText("医米科研用药 · 进行中、已搁置和已完成的需求")).toBeInTheDocument();
    expect(await within(dialog).findByText("已完成")).toHaveClass("merge-target__status--done");

    await userEvent.click(within(dialog).getByRole("button", { name: "合并" }));
    await expectToast("已合并到「京东科研仓对接」，这场会和原话已加进去；「京东科研仓对接」已重新打开");
  });

  it("mergedMessage：没重新打开时和原来一样", () => {
    expect(mergedMessage("京东科研仓对接")).toBe("已合并到「京东科研仓对接」，这场会和原话已加进去");
    expect(mergedMessage("京东科研仓对接", false)).toBe("已合并到「京东科研仓对接」，这场会和原话已加进去");
    expect(mergedMessage("京东科研仓对接", true)).toBe(
      "已合并到「京东科研仓对接」，这场会和原话已加进去；「京东科研仓对接」已重新打开",
    );
  });
});

describe("RequirementPoolPage 旧标签页里对已经并走的需求操作（F3）", () => {
  // 后端对被并掉的 id 一律回 404，body 和详情接口一样带 merged_into（B4）
  const goneError = () =>
    new ApiError("这条需求已并入「京东仓签收凭证」", 404, {
      detail: "这条需求已并入「京东仓签收凭证」",
      merged_into: { id: "requirement-receipt", title: "京东仓签收凭证" },
    });

  it("改状态（没有待办，直接 PATCH）：提示后端的原因，墙上重新取、这张海报消失", async () => {
    const requirementPool = vi
      .fn()
      .mockResolvedValueOnce(poolPayload())
      .mockResolvedValue(poolPayload({ items: [], total: 0 }));
    const updateRequirement = vi.fn().mockRejectedValue(goneError());
    renderPage({ requirementPool, updateRequirement });

    await openPosterMenu();
    await userEvent.click(screen.getByRole("menuitem", { name: "标记完成" }));
    await expectAlertToast("这条需求已并入「京东仓签收凭证」");
    await waitFor(() => expect(screen.queryByRole("article", { name: "需求：京东科研仓对接" })).not.toBeInTheDocument());
  });

  it("改状态（有待办，先取详情）：取详情就被拒，同样提示后端的原因、不弹确认，墙上重新取", async () => {
    const item = requirementItem({ open_task_count: 2, task_count: 3 });
    const requirementPool = vi
      .fn()
      .mockResolvedValueOnce(poolPayload({ items: [item] }))
      .mockResolvedValue(poolPayload({ items: [], total: 0 }));
    const requirement = vi.fn().mockRejectedValue(goneError());
    const updateRequirement = vi.fn();
    renderPage({ requirementPool, requirement, updateRequirement });

    await openPosterMenu();
    await userEvent.click(screen.getByRole("menuitem", { name: "搁置" }));
    await expectAlertToast("这条需求已并入「京东仓签收凭证」");
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
    expect(updateRequirement).not.toHaveBeenCalled();
    await waitFor(() => expect(screen.queryByRole("article", { name: "需求：京东科研仓对接" })).not.toBeInTheDocument());
  });

  it("开并入弹窗：只写后端的原因，不说「这个项目里没有别的需求」；关掉弹窗墙上重新取", async () => {
    const requirementPool = vi
      .fn()
      .mockResolvedValueOnce(poolPayload())
      .mockResolvedValue(poolPayload({ items: [], total: 0 }));
    const requirementMergeTargets = vi.fn().mockRejectedValue(goneError());
    renderPage({ requirementPool, requirementMergeTargets });

    await openPosterMenu();
    await userEvent.click(screen.getByRole("menuitem", { name: "并入其他需求…" }));
    const dialog = await screen.findByRole("dialog", { name: "把「京东科研仓对接」并入" });
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("这条需求已并入「京东仓签收凭证」");
    expect(within(dialog).queryByText("这个项目里没有别的需求")).not.toBeInTheDocument();
    expect(within(dialog).queryByText("正在读取…")).not.toBeInTheDocument();
    expect(within(dialog).queryByText("医米科研用药里的其他需求")).not.toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "并入" })).toBeDisabled();

    await userEvent.click(within(dialog).getByRole("button", { name: "取消" }));
    await waitFor(() => expect(screen.queryByRole("article", { name: "需求：京东科研仓对接" })).not.toBeInTheDocument());
  });

  it("搜索时没取到：已经列出来的留着，原因写在下面", async () => {
    const requirementMergeTargets = vi
      .fn()
      .mockResolvedValueOnce(mergeTargetsPayload())
      .mockRejectedValue(new ApiError("服务没有响应，稍后重试", 0));
    renderPage({ requirementPool: vi.fn().mockResolvedValue(poolPayload()), requirementMergeTargets });

    await openPosterMenu();
    await userEvent.click(screen.getByRole("menuitem", { name: "并入其他需求…" }));
    const dialog = await screen.findByRole("dialog", { name: "把「京东科研仓对接」并入" });
    await within(dialog).findByRole("radio", { name: /京东仓签收凭证/ });
    await userEvent.type(within(dialog).getByRole("textbox", { name: "搜需求名称" }), "签收");
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("服务没有响应，稍后重试");
    expect(within(dialog).getAllByRole("radio")).toHaveLength(3);
  });
});

describe("并入弹窗搜索框聚焦时只有一圈外框（F6）", () => {
  const css = Object.values(
    import.meta.glob("./PoolDialogs.css", { query: "?raw", import: "default", eager: true }) as Record<string, string>,
  )[0].replace(/\/\*[\s\S]*?\*\//g, "");
  const rule = (selector: string) => {
    const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    return css.match(new RegExp(`(?:^|\\})\\s*${escaped}\\s*\\{([^}]*)\\}`))?.[1] ?? "";
  };

  it("输入框聚焦不叠全站的 focus 外框，外面那一圈换成 --signal（和选来源弹窗的搜索框一样）", () => {
    expect(rule(".merge-search input:focus-visible")).toMatch(/outline:\s*none/);
    expect(rule(".merge-search:focus-within")).toMatch(/border-color:\s*var\(--signal\)/);
  });
});
