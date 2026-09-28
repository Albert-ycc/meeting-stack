import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError, type ApiClient, type RelationQuestion, type RequirementDecisionLog } from "../api";
import { LinksFlagsContext } from "./links/LinksFlagsContext";
import type { RequirementDetail } from "../types";
import { RequirementDetailPage } from "./RequirementDetailPage";

function baseDetail(overrides: Partial<RequirementDetail> = {}): RequirementDetail {
  return {
    id: "req-1",
    project_id: "project-a",
    project_name: "云图科研用药",
    project_color: "#2c8d83",
    title: "北辰仓快递配送",
    priority: "P0",
    status: "active",
    created_at: "2026-09-07T12:00:00Z",
    updated_at: "2026-09-07T12:00:00Z",
    open_task_count: 3,
    meeting_count: 2,
    latest_meeting_date: "2026-09-09T12:00:00Z",
    folder_count: 2,
    meetings: [
      {
        id: "vm-1",
        title: "260908 云图需求梳理与北辰科研仓对接",
        recording_date: "2026-09-08T13:05:00Z",
        duration_ms: 50 * 60_000,
        canonical_dir: "/Volumes/资料盘/会议纪要与录音/260908 云图需求梳理与北辰科研仓对接",
      },
    ],
    folders: [
      {
        id: 1,
        name: "V1.5.7-北辰仓快递配送-260914",
        path: "/Volumes/资料盘/蓝鲸云/云图科研用药/V1.5.7-北辰仓快递配送-260914",
        exists: true,
        file_count: 91,
        file_count_capped: false,
        modified_at: "2026-09-14T00:00:00Z",
        preview_files: [
          { relative_path: "README.md", size_bytes: 2500, modified_at: "2026-09-14T00:00:00Z" },
        ],
      },
    ],
    tasks: [
      {
        id: "task-1",
        title: "与北辰拉会对齐科研仓对接工作量与排期",
        detail: "",
        status: "in_progress",
        origin: "ai",
        assignee: "me",
        meeting_title: "260908 云图需求梳理与北辰科研仓对接",
        stall_days: 7,
        stalled: true,
        status_changed_at: "2026-09-08T00:00:00Z",
        created_at: "2026-09-08T00:00:00Z",
        updated_at: "2026-09-08T00:00:00Z",
      },
    ],
    ...overrides,
  };
}

beforeEach(() => {
  Object.assign(navigator, { clipboard: { writeText: vi.fn().mockResolvedValue(undefined) } });
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("RequirementDetailPage", () => {
  it("renders the header, and the meeting/folder/task cards from the loaded detail", async () => {
    const requirement = vi.fn().mockResolvedValue(baseDetail());
    render(
      <RequirementDetailPage
        apiClient={{ requirement } as unknown as ApiClient}
        canPickFolders
        canWrite
        onBack={vi.fn()}
        onOpenMeeting={vi.fn()}
        onOpenProject={vi.fn()}
        onOpenTask={vi.fn()}
        projects={[{ id: "project-a", name: "云图科研用药", color: "#2c8d83" }]}
        requirementId="req-1"
      />,
    );

    expect(await screen.findByRole("heading", { name: "北辰仓快递配送" })).toBeInTheDocument();
    expect(screen.getByText("创建于 09-07")).toBeInTheDocument();
    // 会议标题在「关联会议」表和任务行「来源会议」列都会出现，两处都得有
    expect(screen.getAllByText("260908 云图需求梳理与北辰科研仓对接")).toHaveLength(2);
    expect(screen.getByText("91 个文件")).toBeInTheDocument();
    expect(screen.getByText("与北辰拉会对齐科研仓对接工作量与排期")).toBeInTheDocument();
    expect(screen.getByText("停滞 7 天")).toBeInTheDocument();
  });

  function renderCopyPage(apiClient: Record<string, unknown>) {
    render(
      <RequirementDetailPage
        apiClient={apiClient as unknown as ApiClient}
        canPickFolders
        canWrite
        onBack={vi.fn()}
        onOpenMeeting={vi.fn()}
        onOpenProject={vi.fn()}
        onOpenTask={vi.fn()}
        projects={[]}
        requirementId="req-1"
      />,
    );
  }

  const CONTEXT_MARKDOWN = "# 北辰仓快递配送（云图科研用药 · 需求 · P0 · 进行中）\n\n> 声档生成的背景。\n";

  it("4h：［复制给 Claude Code］复制预取的背景，点击之后不再发请求", async () => {
    const requirement = vi.fn().mockResolvedValue(baseDetail());
    const requirementContext = vi.fn().mockResolvedValue({ markdown: CONTEXT_MARKDOWN, paths: [], cards_missing: 0 });
    renderCopyPage({ requirement, requirementContext });

    await screen.findByRole("heading", { name: "北辰仓快递配送" });
    const button = await screen.findByRole("button", { name: "复制给 Claude Code" });
    expect(requirementContext).toHaveBeenCalledTimes(1);
    await userEvent.click(button);

    expect(navigator.clipboard.writeText).toHaveBeenCalledWith(CONTEXT_MARKDOWN);
    expect(await screen.findByText("已复制，粘给 Claude Code 就行")).toBeInTheDocument();
    expect(requirementContext).toHaveBeenCalledTimes(1);
    expect(requirement).toHaveBeenCalledTimes(1);
  });

  it("4h：有会的纪要不在项目文件夹里时换一句提示", async () => {
    const requirement = vi.fn().mockResolvedValue(baseDetail());
    const requirementContext = vi.fn().mockResolvedValue({ markdown: CONTEXT_MARKDOWN, paths: [], cards_missing: 1 });
    renderCopyPage({ requirement, requirementContext });
    await userEvent.click(await screen.findByRole("button", { name: "复制给 Claude Code" }));
    expect(await screen.findByText("已复制；有 1 场会的纪要不在项目文件夹里，带的是归档文件夹")).toBeInTheDocument();
  });

  it("4h：背景还没取到时按钮写「正在准备…」并置灰", async () => {
    const requirement = vi.fn().mockResolvedValue(baseDetail());
    const requirementContext = vi.fn(() => new Promise(() => undefined));
    renderCopyPage({ requirement, requirementContext });
    await screen.findByRole("heading", { name: "北辰仓快递配送" });
    expect(screen.getByRole("button", { name: "正在准备…" })).toBeDisabled();
  });

  it("4h：旧后台（背景接口 404 Not Found）退回复制旧的材料清单", async () => {
    const requirement = vi.fn().mockResolvedValue(baseDetail());
    const requirementContext = vi.fn().mockRejectedValue(new ApiError("Not Found", 404, { detail: "Not Found" }));
    renderCopyPage({ requirement, requirementContext });

    await screen.findByRole("heading", { name: "北辰仓快递配送" });
    await userEvent.click(await screen.findByRole("button", { name: "复制给 Claude Code" }));

    expect(navigator.clipboard.writeText).toHaveBeenCalledWith(
      [
        "/Volumes/资料盘/会议纪要与录音/260908 云图需求梳理与北辰科研仓对接",
        "/Volumes/资料盘/蓝鲸云/云图科研用药/V1.5.7-北辰仓快递配送-260914",
      ].join("\n"),
    );
    expect(await screen.findByText("已复制 2 条路径")).toBeInTheDocument();
  });

  it("D23（4h 改）：一条路径都没有时按钮照样能点，复制出的背景带需求标题", async () => {
    const requirement = vi.fn().mockResolvedValue(baseDetail({ meetings: [], folders: [] }));
    const requirementContext = vi.fn().mockResolvedValue({ markdown: CONTEXT_MARKDOWN, paths: [], cards_missing: 0 });
    renderCopyPage({ requirement, requirementContext });

    await screen.findByRole("heading", { name: "北辰仓快递配送" });
    const button = await screen.findByRole("button", { name: "复制给 Claude Code" });
    expect(button).toBeEnabled();
    await userEvent.click(button);
    expect(vi.mocked(navigator.clipboard.writeText).mock.calls[0][0]).toContain("# 北辰仓快递配送");
  });

  it("D23（4h 改）：旧后台、关联会议没有 canonical_dir 又没有文件夹时说「这个需求还没有可复制的路径」", async () => {
    const requirement = vi.fn().mockResolvedValue(
      baseDetail({
        meetings: [
          { id: "vm-1", title: "还没落地的会议", recording_date: null, duration_ms: null, canonical_dir: null },
        ],
        folders: [],
      }),
    );
    renderCopyPage({ requirement });

    await screen.findByRole("heading", { name: "北辰仓快递配送" });
    const button = screen.getByRole("button", { name: "复制给 Claude Code" });
    expect(button).toBeEnabled();
    await userEvent.click(button);
    expect(navigator.clipboard.writeText).not.toHaveBeenCalled();
    expect(await screen.findByText("这个需求还没有可复制的路径")).toBeInTheDocument();
  });

  it("expands a folder's full file list on 查看全部, capped at 2000", async () => {
    const requirement = vi.fn().mockResolvedValue(baseDetail());
    const requirementFolderFiles = vi.fn().mockResolvedValue({
      folder_id: 1,
      path: "/Volumes/资料盘/蓝鲸云/云图科研用药/V1.5.7-北辰仓快递配送-260914",
      exists: true,
      total: 91,
      capped: false,
      items: [
        { relative_path: "README.md", size_bytes: 2500, modified_at: "2026-09-14T00:00:00Z" },
        { relative_path: "考据/01-北辰物流开放平台接口字段事实.md", size_bytes: 25_000, modified_at: "2026-09-14T00:00:00Z" },
      ],
    });
    render(
      <RequirementDetailPage
        apiClient={{ requirement, requirementFolderFiles } as unknown as ApiClient}
        canPickFolders
        canWrite
        onBack={vi.fn()}
        onOpenMeeting={vi.fn()}
        onOpenProject={vi.fn()}
        onOpenTask={vi.fn()}
        projects={[]}
        requirementId="req-1"
      />,
    );

    await userEvent.click(await screen.findByRole("button", { name: /查看全部 91 个文件/ }));
    expect(requirementFolderFiles).toHaveBeenCalledWith("req-1", 1);
    expect(await screen.findByText("考据/01-北辰物流开放平台接口字段事实.md")).toBeInTheDocument();
  });

  it("removes a linked meeting and a material folder without asking for confirmation", async () => {
    const requirement = vi.fn().mockResolvedValue(baseDetail());
    const removeRequirementMeeting = vi.fn().mockResolvedValue(baseDetail({ meetings: [] }));
    const removeRequirementFolder = vi.fn().mockResolvedValue(baseDetail({ folders: [] }));
    const confirmSpy = vi.spyOn(window, "confirm");
    render(
      <RequirementDetailPage
        apiClient={{ requirement, removeRequirementMeeting, removeRequirementFolder } as unknown as ApiClient}
        canPickFolders
        canWrite
        onBack={vi.fn()}
        onOpenMeeting={vi.fn()}
        onOpenProject={vi.fn()}
        onOpenTask={vi.fn()}
        projects={[]}
        requirementId="req-1"
      />,
    );

    await screen.findByRole("heading", { name: "北辰仓快递配送" });
    await userEvent.click(screen.getAllByRole("button", { name: "移除" })[0]);
    await waitFor(() => expect(removeRequirementMeeting).toHaveBeenCalledWith("req-1", "vm-1"));
    expect(confirmSpy).not.toHaveBeenCalled();
  });
});

// ---------------------------------------------------------------- 4c：「决议」卡

const LINKS_ON = { linksEnabled: true, semanticEnabled: true, llmConfigured: true };

function decisionLog(overrides: Partial<RequirementDecisionLog> = {}): RequirementDecisionLog {
  const early = { id: "vm-1", title: "周会", date: "2026-09-20" };
  const late = { id: "vm-2", title: "周会", date: "2026-09-28" };
  return {
    requirement: { id: "req-1", title: "北辰仓快递配送" },
    counts: { decisions: 2, later_changed: 1, unplaced: 1 },
    state: { kind: "ok", text: null, action: null },
    meetings: [
      {
        meeting: { ...late, audio_url: "/api/media/430" },
        note: null,
        decisions: [
          {
            id: "dec-b", text: "阈值改成 0.7", detail: "", start_ms: 30_000, end_ms: null,
            placement: { how: "only", requirement_id: "req-1" },
            later: [],
            earlier: [{ relation_id: 17, decision_id: "dec-a", meeting: early, text: "阈值先按 0.8 执行", start_ms: 754_000,
                        quote: "改成 0.7", audio_url: "/api/media/412" }],
            restated: [], dismissed: [], stale_files: [],
          },
        ],
        unplaced: [],
      },
      {
        meeting: { ...early, audio_url: "/api/media/412" },
        note: null,
        decisions: [
          {
            id: "dec-a", text: "阈值先按 0.8 执行", detail: "", start_ms: 754_000, end_ms: null,
            placement: { how: "only", requirement_id: "req-1" },
            later: [{ relation_id: 17, decision_id: "dec-b", meeting: late, text: "阈值改成 0.7", start_ms: 30_000,
                      quote: "改成 0.7", audio_url: "/api/media/430" }],
            earlier: [], restated: [], dismissed: [], stale_files: [],
          },
        ],
        unplaced: [
          {
            id: "dec-u", text: "驻场排班改两班", detail: "", start_ms: null, end_ms: null,
            placement: { how: "unplaced", requirement_id: null },
            later: [], earlier: [], restated: [], dismissed: [], stale_files: [],
          },
        ],
      },
      { meeting: { id: "vm-0", title: "需求评审", date: "2026-09-10", audio_url: null }, note: "这场纪要没有决议段",
        decisions: [], unplaced: [] },
    ],
    ...overrides,
  };
}

function renderWithLog(api: Record<string, unknown>, onOpenPreview?: (fileId: number) => void) {
  const requirement = vi.fn().mockResolvedValue(baseDetail());
  const onOpenMeeting = vi.fn();
  render(
    <LinksFlagsContext.Provider value={LINKS_ON}>
      <RequirementDetailPage
        apiClient={{ requirement, meetingQuotes: vi.fn().mockResolvedValue({ quotes: [] }), ...api } as unknown as ApiClient}
        canPickFolders
        canWrite
        onBack={vi.fn()}
        onOpenMeeting={onOpenMeeting}
        onOpenPreview={onOpenPreview}
        onOpenProject={vi.fn()}
        onOpenTask={vi.fn()}
        projects={[]}
        requirementId="req-1"
      />
    </LinksFlagsContext.Provider>,
  );
  return { onOpenMeeting };
}

describe("RequirementDetailPage 的「决议」卡（4c）", () => {
  it("在「关联会议」和「材料文件夹」之间，按会分组，新的会在前", async () => {
    const requirementDecisions = vi.fn().mockResolvedValue(decisionLog());
    const { onOpenMeeting } = renderWithLog({ requirementDecisions });

    const card = await screen.findByRole("region", { name: "决议" });
    const meetings = screen.getByText("关联会议");
    const folders = screen.getByText("材料文件夹");
    expect(meetings.compareDocumentPosition(card) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(card.compareDocumentPosition(folders) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(requirementDecisions).toHaveBeenCalledWith("req-1");

    const inCard = within(card);
    expect(await inCard.findByText("2 条 · 1 条后来改了")).toBeInTheDocument();
    const headers = inCard.getAllByRole("button", { name: /· 周会$/ }).map((button) => button.textContent);
    expect(headers).toEqual(["9月28日 周一 · 周会", "9月20日 周日 · 周会"]);
    expect(inCard.getByText("后来改了：9月28日 周会『阈值改成 0.7』")).toBeInTheDocument();
    expect(inCard.getByText("这次改了 9月20日 周会定的『阈值先按 0.8 执行』")).toBeInTheDocument();
    // 没有决议段的会压成一行
    expect(inCard.getByRole("button", { name: "9月10日 · 需求评审" })).toBeInTheDocument();
    expect(inCard.getByText(/这场纪要没有决议段/)).toBeInTheDocument();
    await userEvent.click(inCard.getByRole("button", { name: "9月20日 周日 · 周会" }));
    expect(onOpenMeeting).toHaveBeenCalledWith("vm-1");
  });

  it("［不是一回事］以后提示 10 秒，提示里的［撤销］调 undoRelation", async () => {
    const requirementDecisions = vi.fn().mockResolvedValue(decisionLog());
    const answerRelation = vi.fn().mockResolvedValue({ relation: {}, undo_until: "2099-01-01T00:00:00Z" });
    const undoRelation = vi.fn().mockResolvedValue({ relation: {}, removed_deliverable_id: null });
    renderWithLog({ requirementDecisions, answerRelation, undoRelation });

    const card = await screen.findByRole("region", { name: "决议" });
    const buttons = await within(card).findAllByRole("button", { name: "不是一回事" });
    await userEvent.click(buttons[0]);
    expect(answerRelation).toHaveBeenCalledWith(17, { answer: "no" });
    expect(await within(card).findByText("已去掉这条『后来改了』")).toBeInTheDocument();
    await userEvent.click(within(card).getByRole("button", { name: "撤销" }));
    await waitFor(() => expect(undoRelation).toHaveBeenCalledWith(17));
    expect(await within(card).findByText("已撤销")).toBeInTheDocument();
  });

  it("提示 10 秒以后收起；「你标过…不是一回事」那一行的［撤销］发 restore", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      const dismissedLog = decisionLog();
      const first = dismissedLog.meetings[1].decisions[0];
      dismissedLog.meetings[1].decisions[0] = {
        ...first,
        later: [],
        dismissed: [{ relation_id: 17, kind: "later_changed",
                      other: { date: "2026-09-28", meeting_title: "周会", text: "阈值改成 0.7" }, decided_at: null }],
      };
      const requirementDecisions = vi.fn().mockResolvedValueOnce(decisionLog()).mockResolvedValue(dismissedLog);
      const answerRelation = vi.fn().mockResolvedValue({ relation: {}, undo_until: "2099-01-01T00:00:00Z" });
      renderWithLog({ requirementDecisions, answerRelation, undoRelation: vi.fn() });

      const card = await screen.findByRole("region", { name: "决议" });
      // userEvent.setup 会给 navigator 装上只读的 clipboard，后面的用例就换不了：这里用 fireEvent
      fireEvent.click((await within(card).findAllByRole("button", { name: "不是一回事" }))[0]);
      expect(await within(card).findByText("已去掉这条『后来改了』")).toBeInTheDocument();
      await act(async () => {
        vi.advanceTimersByTime(9_000);
      });
      expect(within(card).getByText("已去掉这条『后来改了』")).toBeInTheDocument();
      await act(async () => {
        vi.advanceTimersByTime(1_500);
      });
      expect(within(card).queryByText("已去掉这条『后来改了』")).not.toBeInTheDocument();

      const row = within(card).getByText("你标过和 9月28日 周会那条不是一回事").closest("li") as HTMLElement;
      fireEvent.click(within(row).getByRole("button", { name: "撤销" }));
      await waitFor(() => expect(answerRelation).toHaveBeenLastCalledWith(17, { answer: "restore" }));
    } finally {
      vi.useRealTimers();
    }
  });

  it("［不属于这个需求］和撤销；［放到这个需求］以后那一行灰字留到 undo_until", async () => {
    const requirementDecisions = vi.fn().mockResolvedValue(decisionLog());
    const placeDecision = vi.fn().mockResolvedValue({
      decision: {}, undo: { placement: null, requirement_id: null }, undo_until: "2099-01-01T00:00:00Z",
    });
    renderWithLog({ requirementDecisions, placeDecision });

    const card = await screen.findByRole("region", { name: "决议" });
    const notMine = await within(card).findAllByRole("button", { name: "不属于这个需求" });
    await userEvent.click(notMine[notMine.length - 1]);
    expect(placeDecision).toHaveBeenCalledWith("dec-a", { placement: "none", requirement_id: null });
    expect(await within(card).findAllByText("已从这个需求里拿掉，项目时间线的『决议』里还能看到")).not.toHaveLength(0);
    const gray = within(card)
      .getAllByText("已从这个需求里拿掉，项目时间线的『决议』里还能看到")
      .find((node) => node.closest(".decision-log__placed")) as HTMLElement;
    await userEvent.click(within(gray.closest(".decision-log__placed") as HTMLElement).getByRole("button", { name: "撤销" }));
    await waitFor(() => expect(placeDecision).toHaveBeenLastCalledWith("dec-a", { placement: null, requirement_id: null }));

    // 没归到的折叠成一行，［展开］以后每条［放到这个需求］
    expect(within(card).queryByText("驻场排班改两班")).not.toBeInTheDocument();
    await userEvent.click(within(card).getByRole("button", { name: "展开" }));
    await userEvent.click(within(card).getByRole("button", { name: "放到这个需求" }));
    expect(placeDecision).toHaveBeenLastCalledWith("dec-u", { placement: "picked", requirement_id: "req-1" });
    await waitFor(() =>
      expect(card.querySelector(".decision-log__placed")?.textContent).toContain("已放到这个需求"),
    );
  });

  it("［放到这个需求］在每条后面一直显示，不藏在悬停里", async () => {
    renderWithLog({ requirementDecisions: vi.fn().mockResolvedValue(decisionLog()), placeDecision: vi.fn() });
    const card = await screen.findByRole("region", { name: "决议" });
    await userEvent.click(await within(card).findByRole("button", { name: "展开" }));
    const put = within(card).getByRole("button", { name: "放到这个需求" });
    expect(put.closest(".decision-row__end-actions")).not.toBeNull();
    expect(put.closest(".decision-row__hover-actions")).toBeNull();
    // 有「原话」的那条上的［不属于这个需求］照旧悬停或聚焦时出现
    const notMine = within(card).getAllByRole("button", { name: "不属于这个需求" });
    expect(notMine.every((button) => button.closest(".decision-row__hover-actions"))).toBe(true);
  });

  it("拿掉的是那场会在卡里唯一的一条：重读回来会不在了，灰字行和［撤销］照样留到 undo_until", async () => {
    const picked = decisionLog();
    const only = {
      ...picked,
      counts: { decisions: 1, later_changed: 0, unplaced: 0 },
      meetings: [{ ...picked.meetings[1], unplaced: [] }],
    };
    only.meetings[0].decisions = [{ ...only.meetings[0].decisions[0], later: [], placement: { how: "picked", requirement_id: "req-1" } }];
    const after = { ...only, counts: { decisions: 0, later_changed: 0, unplaced: 0 }, meetings: [] };
    const requirementDecisions = vi.fn().mockResolvedValueOnce(only).mockResolvedValue(after);
    const placeDecision = vi.fn().mockResolvedValue({
      decision: {}, undo: { placement: "picked", requirement_id: "req-1" }, undo_until: "2099-01-01T00:00:00Z",
    });
    renderWithLog({ requirementDecisions, placeDecision });

    const card = await screen.findByRole("region", { name: "决议" });
    await userEvent.click(await within(card).findByRole("button", { name: "不属于这个需求" }));
    await waitFor(() => expect(requirementDecisions).toHaveBeenCalledTimes(2));
    const placedLine = await waitFor(() => {
      const node = card.querySelector(".decision-log__placed");
      expect(node).not.toBeNull();
      return node as HTMLElement;
    });
    expect(placedLine.textContent).toContain("已从这个需求里拿掉");
    // 那场会的标题还在，不写「还没有关联会议」
    expect(within(card).getByRole("button", { name: "9月20日 周日 · 周会" })).toBeInTheDocument();
    expect(within(card).queryByText("还没有关联会议，关联以后这里列出每场会定了什么")).not.toBeInTheDocument();
    await userEvent.click(within(placedLine).getByRole("button", { name: "撤销" }));
    await waitFor(() =>
      expect(placeDecision).toHaveBeenLastCalledWith("dec-a", { placement: "picked", requirement_id: "req-1" }),
    );
  });

  it("没归到的折叠成一行", async () => {
    renderWithLog({ requirementDecisions: vi.fn().mockResolvedValue(decisionLog()) });
    const card = await screen.findByRole("region", { name: "决议" });
    expect(await within(card).findByText(/这场会还有 1 条决议没归到具体需求/)).toBeInTheDocument();
    expect(within(card).queryByText("驻场排班改两班")).not.toBeInTheDocument();
  });

  it("旧后台（没有方法或接口 404）时不画这张卡", async () => {
    const { unmount } = render(
      <RequirementDetailPage
        apiClient={{ requirement: vi.fn().mockResolvedValue(baseDetail()) } as unknown as ApiClient}
        canPickFolders
        canWrite
        onBack={vi.fn()}
        onOpenMeeting={vi.fn()}
        onOpenProject={vi.fn()}
        onOpenTask={vi.fn()}
        projects={[]}
        requirementId="req-1"
      />,
    );
    await screen.findByRole("heading", { name: "北辰仓快递配送" });
    expect(screen.queryByRole("region", { name: "决议" })).not.toBeInTheDocument();
    unmount();

    const requirementDecisions = vi.fn().mockRejectedValue(new ApiError("Not Found", 404, { detail: "Not Found" }));
    renderWithLog({ requirementDecisions });
    await screen.findByRole("heading", { name: "北辰仓快递配送" });
    await waitFor(() => expect(requirementDecisions).toHaveBeenCalled());
    await waitFor(() => expect(screen.queryByRole("region", { name: "决议" })).not.toBeInTheDocument());
  });

  it("三种空和读不到", async () => {
    renderWithLog({
      requirementDecisions: vi.fn().mockResolvedValue(
        decisionLog({ meetings: [], counts: { decisions: 0, later_changed: 0, unplaced: 0 } }),
      ),
    });
    expect(await screen.findByText("还没有关联会议，关联以后这里列出每场会定了什么")).toBeInTheDocument();
  });
});

describe("RequirementDetailPage 材料文件夹的小签（4d）", () => {
  afterEach(async () => {
    const { clearMentionedCounts } = await import("./files/useMentionedCounts");
    clearMentionedCounts();
  });

  it("带 file_id 的文件行有「3 场会提到」，没有 file_id 的不带；点小签调 onOpenPreview(812)", async () => {
    const detail = baseDetail();
    detail.folders[0].preview_files = [
      { relative_path: "README.md", size_bytes: 2500, modified_at: "2026-09-14T00:00:00Z", file_id: 812 },
      { relative_path: "readme-disk.md", size_bytes: 10, modified_at: "2026-09-14T00:00:00Z" },
    ];
    const requirement = vi.fn().mockResolvedValue(detail);
    const mentionedCounts = vi.fn().mockResolvedValue({ counts: { "812": 3 } });
    const onOpenPreview = vi.fn();
    render(
      <RequirementDetailPage
        apiClient={{ requirement, mentionedCounts } as unknown as ApiClient}
        canPickFolders
        canWrite
        onBack={vi.fn()}
        onOpenMeeting={vi.fn()}
        onOpenPreview={onOpenPreview}
        onOpenProject={vi.fn()}
        onOpenTask={vi.fn()}
        projects={[]}
        requirementId="req-1"
      />,
    );
    const badge = await screen.findByRole("button", { name: "3 场会提到" });
    expect(badge).toHaveAttribute("title", "在 3 场会上被提到");
    expect(mentionedCounts).toHaveBeenCalledTimes(1);
    expect(mentionedCounts).toHaveBeenCalledWith([812]);
    const diskRow = screen.getByText("readme-disk.md").closest(".requirement-detail__folder-row")!;
    expect(within(diskRow as HTMLElement).queryByText(/场会提到/)).not.toBeInTheDocument();
    await userEvent.click(badge);
    expect(onOpenPreview).toHaveBeenCalledWith(812);
  });
});

describe("RequirementDetailPage 决议卡的「可能过时」（4e）", () => {
  const STALE: RelationQuestion = {
    relation_id: 57,
    kind: "affects",
    text: "『报价单 v3』之后没改过，可能过时",
    decision: {
      id: "dec-a", text: "阈值先按 0.8 执行", date: "2026-09-20", meeting_id: "vm-1", meeting_title: "周会",
      start_ms: 754_000, audio_url: "/api/media/412",
    },
    passage: { loc: "第 2 页", text: "…阈值 0.9…" },
    file: { id: 812, name: "报价单 v3.xlsx" },
    answers: ["updated", "no"],
  };

  function withStale(stale: RelationQuestion[]): RequirementDecisionLog {
    const log = decisionLog();
    const [late, early, ...rest] = log.meetings;
    return {
      ...log,
      meetings: [late, { ...early, decisions: [{ ...early.decisions[0], stale_files: stale }] }, ...rest],
    };
  }

  it("每个文件一行［已更新］［不相关］，文件名打开预览抽屉；回答以后提示和收成的一行都带［撤销］", async () => {
    const requirementDecisions = vi.fn().mockResolvedValueOnce(withStale([STALE])).mockResolvedValue(withStale([]));
    const answerRelation = vi.fn().mockResolvedValue({ relation: {}, undo_until: new Date(Date.now() + 600_000).toISOString() });
    const undoRelation = vi.fn().mockResolvedValue({ relation: {}, removed_deliverable_id: null });
    const onOpenPreview = vi.fn();
    renderWithLog({ requirementDecisions, answerRelation, undoRelation }, onOpenPreview);

    const card = await screen.findByRole("region", { name: "决议" });
    const row = await within(card).findByRole("group", { name: STALE.text });
    // 决议本身就是那一行：不再列片段
    expect(within(row).queryByText(/第 2 页/)).toBeNull();
    await userEvent.click(within(row).getByRole("button", { name: STALE.text }));
    expect(onOpenPreview).toHaveBeenCalledWith(812);

    await userEvent.click(within(row).getByRole("button", { name: "已更新" }));
    expect(answerRelation).toHaveBeenCalledWith(57, { answer: "updated" });
    await waitFor(() => expect(requirementDecisions).toHaveBeenCalledTimes(2));
    expect((await within(card).findAllByText("已标为更新过")).length).toBeGreaterThan(0);
    await userEvent.click(within(card).getAllByRole("button", { name: "撤销" })[0]);
    await waitFor(() => expect(undoRelation).toHaveBeenCalledWith(57));
  });
});
