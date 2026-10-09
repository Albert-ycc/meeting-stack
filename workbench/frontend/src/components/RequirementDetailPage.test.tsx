import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError, type ApiClient, type RelationQuestion, type RequirementDecisionLog } from "../api";
import { LinksFlagsContext } from "./links/LinksFlagsContext";
import type { RequirementDetail, RequirementSource } from "../types";
import { stubPeaksFetch } from "./pool/peaksFixtures";
import { EXPORT_SOURCE, JD_SOURCE, mergeTargetsPayload, RECEIPT_SOURCE, taskItem } from "./pool/poolFixtures";
import { clearPeaksCache } from "./pool/PosterWaveform";
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
    // 头部和海报一致：项目、等级、进行中在需求名上面；没有来源时不画「出自录音」
    expect(screen.getByRole("button", { name: "云图科研用药" })).toBeInTheDocument();
    expect(screen.getByText("P0")).toHaveClass("requirement-detail__level--p0");
    expect(screen.getByText("进行中", { selector: ".requirement-detail__meta *" })).toHaveClass("requirement-detail__status");
    expect(screen.queryByRole("region", { name: "出自录音" })).not.toBeInTheDocument();
    // 会议标题在「关联会议」表和任务行「来源会议」列都会出现，两处都得有
    expect(screen.getAllByText("260908 云图需求梳理与北辰科研仓对接")).toHaveLength(2);
    expect(screen.getByText("91 个文件")).toBeInTheDocument();
    expect(screen.getByText("与北辰拉会对齐科研仓对接工作量与排期")).toBeInTheDocument();
    expect(screen.getByText("停滞 7 天")).toBeInTheDocument();
  });

  it("从需求 A 换到需求 B：A 的请求晚回来，不把 B 的页面换成 A", async () => {
    let releaseA: () => void = () => undefined;
    const aReady = new Promise<void>((resolve) => {
      releaseA = resolve;
    });
    const requirement = vi.fn().mockImplementation(async (id: string) => {
      if (id === "req-a") {
        await aReady;
        return baseDetail({ id: "req-a", title: "需求 A" });
      }
      return baseDetail({ id: "req-b", title: "需求 B" });
    });
    const props = {
      apiClient: { requirement } as unknown as ApiClient,
      canPickFolders: true,
      canWrite: true,
      onBack: vi.fn(),
      onOpenMeeting: vi.fn(),
      onOpenProject: vi.fn(),
      onOpenTask: vi.fn(),
      projects: [],
    };
    const { rerender } = render(<RequirementDetailPage {...props} requirementId="req-a" />);
    rerender(<RequirementDetailPage {...props} requirementId="req-b" />);
    expect(await screen.findByRole("heading", { name: "需求 B" })).toBeInTheDocument();

    await act(async () => {
      releaseA();
      await aReady;
    });
    expect(screen.getByRole("heading", { name: "需求 B" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "需求 A" })).not.toBeInTheDocument();
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

  it("4h：［复制给 Claude Code］点的当下现取背景：详情页开着时别处改过的说明也带上（第二轮审查建议 5）", async () => {
    const requirement = vi.fn().mockResolvedValue(baseDetail());
    const paths = ["/Volumes/资料盘/会议纪要与录音/260908 云图需求梳理与北辰科研仓对接", "/Volumes/资料盘/蓝鲸云/云图科研用药/V1.5.7-北辰仓快递配送-260914"];
    const changed = `${CONTEXT_MARKDOWN}\n## 说明\n别处刚改过的说明\n`;
    const requirementContext = vi
      .fn()
      .mockResolvedValueOnce({ markdown: CONTEXT_MARKDOWN, paths, cards_missing: 0 })
      .mockResolvedValueOnce({ markdown: changed, paths, cards_missing: 0 });
    renderCopyPage({ requirement, requirementContext });

    await screen.findByRole("heading", { name: "北辰仓快递配送" });
    const button = await screen.findByRole("button", { name: "复制给 Claude Code" });
    expect(requirementContext).toHaveBeenCalledTimes(1);
    await userEvent.click(button);

    // 复制出去的是点的当下取回来的那一份，不是进页面时取的
    await waitFor(() => expect(navigator.clipboard.writeText).toHaveBeenCalledWith(changed));
    // 和海报上的「接下」同一句（R05-5）：N 取背景里带回来的文件路径数
    expect(await screen.findByText("已复制需求背景和 2 个文件路径，去 Claude Code 粘贴")).toBeInTheDocument();
    expect(requirementContext).toHaveBeenCalledTimes(2);
    expect(requirement).toHaveBeenCalledTimes(1);
  });

  it("4h：有会的纪要不在项目文件夹里时，同一句后面多带一句说明", async () => {
    const requirement = vi.fn().mockResolvedValue(baseDetail());
    const requirementContext = vi.fn().mockResolvedValue({ markdown: CONTEXT_MARKDOWN, paths: ["/Volumes/资料盘/a"], cards_missing: 1 });
    renderCopyPage({ requirement, requirementContext });
    await userEvent.click(await screen.findByRole("button", { name: "复制给 Claude Code" }));
    expect(
      await screen.findByText(
        "已复制需求背景和 1 个文件路径，去 Claude Code 粘贴；有 1 场会的纪要不在项目文件夹里，带的是归档文件夹",
      ),
    ).toBeInTheDocument();
  });

  it("4h：没有任何文件路径时不说「0 个文件路径」", async () => {
    const requirement = vi.fn().mockResolvedValue(baseDetail());
    const requirementContext = vi.fn().mockResolvedValue({ markdown: CONTEXT_MARKDOWN, paths: [], cards_missing: 0 });
    renderCopyPage({ requirement, requirementContext });
    await userEvent.click(await screen.findByRole("button", { name: "复制给 Claude Code" }));
    expect(await screen.findByText("已复制需求背景，去 Claude Code 粘贴")).toBeInTheDocument();
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
  // 会的日子不是今年时标题带年份：夹具的会在 2026 年，钉住「今天」，明年跑结果也一样
  const TODAY = new Date("2026-09-30T10:00:00+08:00");
  beforeEach(() => {
    vi.useFakeTimers({ now: TODAY, toFake: ["Date"] });
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("在「关联会议」和「材料文件夹」之间，按会分组，新的会在前", async () => {
    const requirementDecisions = vi.fn().mockResolvedValue(decisionLog());
    const { onOpenMeeting } = renderWithLog({ requirementDecisions });

    const card = await screen.findByRole("region", { name: "决议" });
    const meetings = screen.getByText("关联会议");
    const folders = screen.getByText("材料文件夹");
    expect(meetings.compareDocumentPosition(card) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(card.compareDocumentPosition(folders) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    // 卡片先渲染、取数的 effect 后跑：机器忙时要等一下
    await waitFor(() => expect(requirementDecisions).toHaveBeenCalledWith("req-1"));

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
    vi.useFakeTimers({ shouldAdvanceTime: true, now: TODAY });
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

describe("RequirementDetailPage 修改需求（R04-1）", () => {
  it("给了 onEdit 时［编辑需求］去修改需求的二级页、不再弹窗；从修改页回来提示一次", async () => {
    const onEdit = vi.fn();
    const onFlashShown = vi.fn();
    render(
      <RequirementDetailPage
        apiClient={{ requirement: vi.fn().mockResolvedValue(baseDetail()) } as unknown as ApiClient}
        canPickFolders
        canWrite
        flash="已保存"
        onBack={vi.fn()}
        onEdit={onEdit}
        onFlashShown={onFlashShown}
        onOpenMeeting={vi.fn()}
        onOpenProject={vi.fn()}
        onOpenTask={vi.fn()}
        projects={[]}
        requirementId="req-1"
      />,
    );
    await screen.findByRole("heading", { name: "北辰仓快递配送" });

    expect(screen.getByRole("status")).toHaveTextContent("已保存");
    expect(onFlashShown).toHaveBeenCalledTimes(1);
    await userEvent.click(screen.getByRole("button", { name: "编辑需求" }));
    expect(onEdit).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });
});

describe("RequirementDetailPage 头部和出自录音（R05）", () => {
  // 来源照生产库那场会（会名、录音时间、时长、原话、时间锚，见后端 requirement_pool_world）
  const origin = {
    id: 1,
    kind: "origin" as const,
    meeting_id: "vm-20260916-190150-2eebb406",
    meeting_title: "260916 医米京东科研仓系统对接",
    recording_date: "2026-09-16T19:01:50-07:00",
    duration_ms: 3033387,
    audio_artifact_id: null,
    quote: "就是这个入库单的这个单据，你得需要从你们的一米这个系统里面给我们这个库房推过来。",
    anchor_ms: 825270,
    via_candidate_title: null,
  };
  const merged = {
    ...origin,
    id: 2,
    kind: "merged" as const,
    quote: "强制要求患者除了传教和随行码嗯患者手持身份证跟药盒拍照，然后还要传这个随货通行单，",
    anchor_ms: 1909360,
    via_candidate_title: "京东仓签收凭证",
  };

  function renderDetail(overrides: Partial<RequirementDetail>, onOpenMeeting = vi.fn()) {
    render(
      <RequirementDetailPage
        apiClient={{ requirement: vi.fn().mockResolvedValue(baseDetail(overrides)) } as unknown as ApiClient}
        canPickFolders
        canWrite
        onBack={vi.fn()}
        onOpenMeeting={onOpenMeeting}
        onOpenProject={vi.fn()}
        onOpenTask={vi.fn()}
        projects={[]}
        requirementId="req-1"
      />,
    );
    return onOpenMeeting;
  }

  it("需求名最醒目，座次、项目、等级在上，说明在下；出自录音列出提出的和合并进来的原话", async () => {
    clearPeaksCache();
    stubPeaksFetch({ 77: origin.duration_ms });
    const withAudio = { ...origin, audio_artifact_id: 77 };
    const mergedWithAudio = { ...merged, audio_artifact_id: 77 };
    const onOpenMeeting = renderDetail({
      title: "京东科研仓对接",
      project_name: "医米科研用药",
      project_seat: 1,
      summary: "把京东科研仓当作一个药房接进医米。",
      source: withAudio,
      sources: [withAudio, mergedWithAudio],
    });

    expect(await screen.findByRole("heading", { name: "京东科研仓对接" })).toBeInTheDocument();
    expect(screen.getByText("1", { selector: ".requirement-detail__meta *" })).toHaveClass("requirement-detail__seat");
    expect(screen.getByText("把京东科研仓当作一个药房接进医米。")).toHaveClass("requirement-detail__summary");
    // 头部那张票的封面：会议信息、波形上的时间签、「打开会议」
    const card = within(screen.getByRole("region", { name: "出自录音" }));
    expect(card.getByText("260916 医米京东科研仓系统对接")).toBeInTheDocument();
    // 时间签在峰值取到以后才画（取不到整块波形都不画，见下面的用例）
    expect(await card.findByText("00:13:45 提出")).toBeInTheDocument();
    expect(card.getByText("00:31:49 合并 · 京东仓签收凭证")).toBeInTheDocument();
    // 下面的「来源」：提出的和合并进来的原话
    const quotes = within(screen.getByRole("region", { name: "来源" }));
    expect(quotes.getByText("提出 · 会上原话")).toBeInTheDocument();
    expect(quotes.getByText("合并自候选「京东仓签收凭证」")).toBeInTheDocument();
    expect(quotes.getByText(`「${merged.quote}」`)).toBeInTheDocument();

    await userEvent.click(quotes.getByRole("button", { name: /从 00:31:49 开始放/ }));
    expect(onOpenMeeting).toHaveBeenLastCalledWith("vm-20260916-190150-2eebb406", 1909360);
    await userEvent.click(card.getByRole("button", { name: /打开会议/ }));
    expect(onOpenMeeting).toHaveBeenLastCalledWith("vm-20260916-190150-2eebb406", 825270);
    vi.unstubAllGlobals();
  });

  it("这场会没有录音文件：「出自录音」只有会议信息和原话时间锚，没有波形、时间签、坐标轴", async () => {
    renderDetail({ source: origin, sources: [origin, merged] });

    const card = within(await screen.findByRole("region", { name: "出自录音" }));
    expect(card.getByText("260916 医米京东科研仓系统对接")).toBeInTheDocument();
    expect(card.queryByText("00:00")).not.toBeInTheDocument();
    expect(card.queryByText("00:13:45 提出")).not.toBeInTheDocument();
    expect(screen.getByRole("region", { name: "来源" })).toContainElement(
      screen.getByRole("button", { name: /从 00:13:45 开始放/ }),
    );
  });

  it("票根：待办、会议、材料三个数，点了滚到对应的卡；没有来源时封面是静音线，日子写建的那天", async () => {
    const scrollIntoView = vi.fn();
    Element.prototype.scrollIntoView = scrollIntoView;
    renderDetail({ source: null, sources: [] });

    expect(await screen.findByText("没有来源录音")).toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "出自录音" })).not.toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "来源" })).not.toBeInTheDocument();
    expect(screen.getByText("09-07").parentElement).toHaveTextContent("09-07 新建");

    await userEvent.click(screen.getByRole("button", { name: /^3\s*待办$/ }));
    expect(scrollIntoView.mock.contexts.at(-1)).toHaveTextContent("关联已有任务");
    await userEvent.click(screen.getByRole("button", { name: /^1\s*会议$/ }));
    expect(scrollIntoView.mock.contexts.at(-1)).toHaveTextContent("＋ 关联会议");
    await userEvent.click(screen.getByRole("button", { name: /^1\s*材料$/ }));
    expect(scrollIntoView.mock.contexts.at(-1)).toHaveTextContent("V1.5.7-北辰仓快递配送-260914");
    delete (Element.prototype as Partial<Element>).scrollIntoView;
  });

  it("票根的日子：有来源时写那场会的日子「会上提出」", async () => {
    renderDetail({ source: origin, sources: [origin] });
    expect(await screen.findByText("09-17")).toBeInTheDocument();
    expect(screen.getByText("09-17").parentElement).toHaveTextContent("09-17 会上提出");
  });

  it("已完成、已搁置在需求名后面盖章，不再挂「进行中」", async () => {
    renderDetail({ status: "done" });
    expect(await screen.findByText("已完成")).toHaveClass("requirement-detail__stamp--done");
    expect(screen.queryByText("进行中", { selector: ".requirement-detail__meta *" })).not.toBeInTheDocument();
    cleanup();

    renderDetail({ status: "shelved" });
    expect(await screen.findByText("已搁置")).toHaveClass("requirement-detail__stamp--shelved");
  });
});

// ---------------------------------------------------------------- 需求池改版收尾（260930、261001 版 PRD）

/*
 * 下面几组用例的会、原话、时间锚照生产库逐字稿抄（poolFixtures 和后端 requirement_pool_world 同一份）：
 * 京东科研仓对接那场会有 00:13:45 提出、00:31:49 合并进来的两句；云课堂那场会有 00:09:36 一句。
 */
const JD_ID = JD_SOURCE.meeting_id;
const CVM_ID = EXPORT_SOURCE.meeting_id;
const JD_MEETING = {
  id: JD_ID,
  title: JD_SOURCE.meeting_title,
  recording_date: JD_SOURCE.recording_date,
  duration_ms: JD_SOURCE.duration_ms,
  canonical_dir: "/Volumes/资料盘/会议纪要与录音/260916 医米京东科研仓系统对接",
};
const CVM_MEETING = {
  id: CVM_ID,
  title: EXPORT_SOURCE.meeting_title,
  recording_date: EXPORT_SOURCE.recording_date,
  duration_ms: EXPORT_SOURCE.duration_ms,
  canonical_dir: null,
};
const JD_ORIGIN: RequirementSource = { ...JD_SOURCE };
const JD_MERGED: RequirementSource = { ...RECEIPT_SOURCE, kind: "merged", via_candidate_title: "京东仓签收凭证" };
const CVM_ORIGIN: RequirementSource = { ...EXPORT_SOURCE };

function jdDetail(overrides: Partial<RequirementDetail> = {}): RequirementDetail {
  return baseDetail({
    title: "京东科研仓对接",
    project_name: "医米科研用药",
    priority: "P0",
    meetings: [JD_MEETING],
    folders: [],
    tasks: [],
    source: JD_ORIGIN,
    sources: [JD_ORIGIN, JD_MERGED],
    ...overrides,
  });
}

function renderJd(api: Record<string, unknown>, detail: RequirementDetail = jdDetail()) {
  const requirement = vi.fn().mockResolvedValue(detail);
  render(
    <RequirementDetailPage
      apiClient={{ requirement, ...api } as unknown as ApiClient}
      canPickFolders
      canWrite
      onBack={vi.fn()}
      onOpenMeeting={vi.fn()}
      onOpenProject={vi.fn()}
      onOpenTask={vi.fn()}
      projects={[{ id: "project-a", name: "医米科研用药", color: "#2c8d83" }]}
      requirementId="req-1"
    />,
  );
  return requirement;
}

function meetingRow(title: string) {
  const cell = screen.getByText(title, { selector: ".requirement-detail__meeting-title" });
  return within(cell.closest(".requirement-detail__meeting-row") as HTMLElement);
}

describe("RequirementDetailPage 移除关联会议（审查 M1）", () => {
  it("这场会有带原话的来源：移除前先确认，文案写明一起删掉几句原话、不能恢复；取消不移除，确认才移除", async () => {
    const removeRequirementMeeting = vi.fn().mockResolvedValue(jdDetail({ meetings: [], sources: [] }));
    renderJd({ removeRequirementMeeting });
    await screen.findByRole("heading", { name: "京东科研仓对接" });

    fireEvent.click(meetingRow(JD_MEETING.title).getByRole("button", { name: "移除" }));
    const dialog = await screen.findByRole("alertdialog");
    expect(dialog).toHaveTextContent("移除后，出自这场会的 2 句原话会一起删掉，不能恢复");
    expect(removeRequirementMeeting).not.toHaveBeenCalled();

    fireEvent.click(within(dialog).getByRole("button", { name: "取消" }));
    await waitFor(() => expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument());
    expect(removeRequirementMeeting).not.toHaveBeenCalled();

    fireEvent.click(meetingRow(JD_MEETING.title).getByRole("button", { name: "移除" }));
    fireEvent.click(within(await screen.findByRole("alertdialog")).getByRole("button", { name: "移除" }));
    await waitFor(() => expect(removeRequirementMeeting).toHaveBeenCalledWith("req-1", JD_ID));
  });

  it("只数出自这场会的原话：两场会各有来源时，每场会写各自的句数", async () => {
    const removeRequirementMeeting = vi.fn().mockResolvedValue(jdDetail());
    renderJd(
      { removeRequirementMeeting },
      jdDetail({ meetings: [JD_MEETING, CVM_MEETING], sources: [JD_ORIGIN, JD_MERGED, CVM_ORIGIN] }),
    );
    await screen.findByRole("heading", { name: "京东科研仓对接" });

    fireEvent.click(meetingRow(CVM_MEETING.title).getByRole("button", { name: "移除" }));
    expect(await screen.findByRole("alertdialog")).toHaveTextContent("出自这场会的 1 句原话");
    fireEvent.click(within(screen.getByRole("alertdialog")).getByRole("button", { name: "取消" }));
    await waitFor(() => expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument());

    fireEvent.click(meetingRow(JD_MEETING.title).getByRole("button", { name: "移除" }));
    expect(await screen.findByRole("alertdialog")).toHaveTextContent("出自这场会的 2 句原话");
  });

  it("没有原话的会照旧直接移除，不弹确认：别的会有来源、这场会没有", async () => {
    const removeRequirementMeeting = vi.fn().mockResolvedValue(jdDetail());
    renderJd({ removeRequirementMeeting }, jdDetail({ meetings: [JD_MEETING, CVM_MEETING], sources: [CVM_ORIGIN] }));
    await screen.findByRole("heading", { name: "京东科研仓对接" });

    fireEvent.click(meetingRow(JD_MEETING.title).getByRole("button", { name: "移除" }));
    await waitFor(() => expect(removeRequirementMeeting).toHaveBeenCalledWith("req-1", JD_ID));
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
  });

  it("只关联了会议、没挑原话的来源（原话为空）不算原话：照旧直接移除", async () => {
    const removeRequirementMeeting = vi.fn().mockResolvedValue(jdDetail());
    const bare = { ...JD_ORIGIN, quote: "", anchor_ms: null };
    renderJd({ removeRequirementMeeting }, jdDetail({ source: bare, sources: [bare] }));
    await screen.findByRole("heading", { name: "京东科研仓对接" });

    fireEvent.click(meetingRow(JD_MEETING.title).getByRole("button", { name: "移除" }));
    await waitFor(() => expect(removeRequirementMeeting).toHaveBeenCalledWith("req-1", JD_ID));
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
  });

  it("需求没有来源（手动新建时留空）：直接移除", async () => {
    const removeRequirementMeeting = vi.fn().mockResolvedValue(jdDetail());
    renderJd({ removeRequirementMeeting }, jdDetail({ source: null, sources: [] }));
    await screen.findByRole("heading", { name: "京东科研仓对接" });

    fireEvent.click(meetingRow(JD_MEETING.title).getByRole("button", { name: "移除" }));
    await waitFor(() => expect(removeRequirementMeeting).toHaveBeenCalledWith("req-1", JD_ID));
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
  });
});

describe("RequirementDetailPage 撤销合并（R01-14，S03-b）", () => {
  // 合并后 10 分钟内可撤销；until 是后端算好的截止时刻，页面按本机时间判断，用例里钉住时钟
  const NOW = new Date("2026-10-01T10:00:00+08:00");
  const undoable = (until: Date): RequirementSource => ({
    ...JD_MERGED,
    undo_merge: { kind: "candidate", candidate_id: "candidate-receipt", until: until.toISOString() },
  });

  beforeEach(() => {
    vi.useFakeTimers({ shouldAdvanceTime: true, now: NOW });
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("合并进来的原话在时限内：那一行有「撤销合并」，点了调接口，成功后刷新详情并提示「已撤销合并」", async () => {
    const withUndo = jdDetail({ sources: [JD_ORIGIN, undoable(new Date(NOW.getTime() + 8 * 60_000))] });
    const afterUndo = jdDetail({ sources: [JD_ORIGIN] });
    const undoCandidateMerge = vi.fn().mockResolvedValue({});
    const requirement = vi.fn().mockResolvedValueOnce(withUndo).mockResolvedValue(afterUndo);
    render(
      <RequirementDetailPage
        apiClient={{ requirement, undoCandidateMerge } as unknown as ApiClient}
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

    const card = within(await screen.findByRole("region", { name: "来源" }));
    const button = card.getByRole("button", { name: "撤销合并" });
    expect(button.closest("li")).toHaveTextContent("合并自候选「京东仓签收凭证」");
    fireEvent.click(button);

    await waitFor(() => expect(undoCandidateMerge).toHaveBeenCalledWith("candidate-receipt"));
    expect(await screen.findByText("已撤销合并")).toBeInTheDocument();
    await waitFor(() => expect(requirement).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(screen.queryByRole("button", { name: "撤销合并" })).not.toBeInTheDocument());
  });

  it("撤销失败（比如超过 10 分钟）：红色提示条写后端给的原因，详情不变", async () => {
    const undoCandidateMerge = vi
      .fn()
      .mockRejectedValue(new ApiError("合并超过 10 分钟，不能撤销了", 409, { detail: "合并超过 10 分钟，不能撤销了" }));
    renderJd(
      { undoCandidateMerge },
      jdDetail({ sources: [JD_ORIGIN, undoable(new Date(NOW.getTime() + 60_000))] }),
    );

    fireEvent.click(await screen.findByRole("button", { name: "撤销合并" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("合并超过 10 分钟，不能撤销了");
    expect(screen.queryByText("已撤销合并")).not.toBeInTheDocument();
  });

  it("过了 until 就不显示：页面打开时已经过了，或者开着页面等到了点", async () => {
    renderJd({}, jdDetail({ sources: [JD_ORIGIN, undoable(new Date(NOW.getTime() + 60_000))] }));
    expect(await screen.findByRole("button", { name: "撤销合并" })).toBeInTheDocument();
    // 异步推进：React 的副作用要靠微任务和定时器交替才排得上，同步推进会跳过它们
    await act(async () => {
      await vi.advanceTimersByTimeAsync(61_000);
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(500);
    });
    expect(screen.queryByRole("button", { name: "撤销合并" })).not.toBeInTheDocument();
    cleanup();

    renderJd({}, jdDetail({ sources: [JD_ORIGIN, undoable(new Date(NOW.getTime() - 1_000))] }));
    await screen.findByRole("heading", { name: "京东科研仓对接" });
    expect(screen.queryByRole("button", { name: "撤销合并" })).not.toBeInTheDocument();
  });

  it("没有写权限时不显示「撤销合并」", async () => {
    const requirement = vi.fn().mockResolvedValue(jdDetail({ sources: [JD_ORIGIN, undoable(new Date(NOW.getTime() + 60_000))] }));
    render(
      <RequirementDetailPage
        apiClient={{ requirement } as unknown as ApiClient}
        canPickFolders
        canWrite={false}
        onBack={vi.fn()}
        onOpenMeeting={vi.fn()}
        onOpenProject={vi.fn()}
        onOpenTask={vi.fn()}
        projects={[]}
        requirementId="req-1"
      />,
    );
    await screen.findByRole("heading", { name: "京东科研仓对接" });
    expect(screen.queryByRole("button", { name: "撤销合并" })).not.toBeInTheDocument();
  });
});

describe("RequirementDetailPage 在任务区新建任务（R05-6，S12-c）", () => {
  it("弹窗写「挂在「需求名」下」、挂到需求只读；填任务名、选负责人后创建，任务挂在本需求上，创建后刷新详情", async () => {
    const createTask = vi.fn().mockResolvedValue({});
    const requirements = vi.fn().mockResolvedValue({ items: [], total: 0, limit: 200, offset: 0, counts: { active: 0, done: 0, shelved: 0, all: 0 } });
    const requirement = renderJd({ createTask, requirements });
    await screen.findByRole("heading", { name: "京东科研仓对接" });

    fireEvent.click(screen.getByRole("button", { name: "＋ 新建任务" }));
    const dialog = await screen.findByRole("dialog", { name: "新建任务" });
    expect(dialog).toHaveTextContent("挂在「京东科研仓对接」下");
    const locked = within(dialog).getByRole("group", { name: "挂到需求" });
    expect(locked).toHaveTextContent("京东科研仓对接");
    expect(locked).toHaveTextContent("P0");
    expect(dialog).toHaveTextContent("从需求详情新建，固定挂在这条需求上");
    // 不能改需求，也不能改项目：没有下拉
    expect(within(dialog).queryByRole("button", { name: /所属项目|医米科研用药|未归项目/ })).not.toBeInTheDocument();
    expect(within(dialog).queryByRole("button", { name: /未归需求/ })).not.toBeInTheDocument();

    fireEvent.change(within(dialog).getByRole("textbox"), { target: { value: "把入库单推送接口的字段表发给京东" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "AI" }));
    fireEvent.click(within(dialog).getByRole("button", { name: "创建" }));

    await waitFor(() =>
      expect(createTask).toHaveBeenCalledWith({
        title: "把入库单推送接口的字段表发给京东",
        project_id: "project-a",
        requirement_id: "req-1",
        assignee: "ai",
      }),
    );
    await waitFor(() => expect(requirement).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "新建任务" })).not.toBeInTheDocument());
  });
});

describe("RequirementDetailPage 头部：超长需求名（审查 B6）", () => {
  // 取会上说过的几句决议连起来，凑一条 170 多字的需求名（审查里是 166 字）
  const LONG_TITLE =
    "京东科研仓对接：采购单入库、销售单、订单取消、物流轨迹四类接口必须对上，签收凭证怎么拿还悬着；" +
    "医米系统强制要求患者上传手写签名的随货同行单，与手持身份证及药盒拍照一并作为收货凭证；" +
    "患者收货凭证上传不全将影响其下一次药品申请，单次结算周期内如有差异不单独处理，可平移到下一次结算周期；" +
    "医米侧确认盘点口径与智研保持一致，医米侧不能只接收物流状态。";

  it("需求名 170 多字：按钮在标题行的顶上，不悬在十行标题中间；面包屑「需求池」不收缩、需求名一行截断", async () => {
    expect(LONG_TITLE.length).toBeGreaterThan(166);
    renderJd({}, jdDetail({ title: LONG_TITLE }));
    await screen.findByRole("heading", { name: LONG_TITLE });

    const row = document.querySelector(".requirement-detail__title-row") as HTMLElement;
    expect(getComputedStyle(row).alignItems).toBe("flex-start");

    const crumb = document.querySelector(".requirement-detail__breadcrumb") as HTMLElement;
    const backLink = crumb.querySelector("button") as HTMLElement;
    expect(getComputedStyle(backLink).flexShrink).toBe("0");
    expect(getComputedStyle(backLink).whiteSpace).toBe("nowrap");
    const current = crumb.querySelector(".requirement-detail__crumb-current") as HTMLElement;
    expect(current).toHaveTextContent(LONG_TITLE);
    expect(current).toHaveAttribute("title", LONG_TITLE);
    expect(getComputedStyle(current).textOverflow).toBe("ellipsis");
    expect(getComputedStyle(current).whiteSpace).toBe("nowrap");
    expect(getComputedStyle(current).overflow).toBe("hidden");
  });
});

// ---------------------------------------------------------------- 需求并需求 + 改状态（261009）

describe("RequirementDetailPage 头部改状态、并入（D2、D4、D7～D11）", () => {
  const OPEN = [
    taskItem({ requirement_id: "req-1" }),
    taskItem({ id: "task-fields", title: "整理四类接口的字段对照表", status: "in_progress", requirement_id: "req-1" }),
    taskItem({ id: "task-kickoff", title: "约京东科研仓开对接启动会", status: "done", requirement_id: "req-1" }),
  ];
  const MAIN = baseDetail({
    id: "requirement-receipt",
    title: "京东仓签收凭证",
    project_name: "医米科研用药",
    priority: "P1",
    meetings: [JD_MEETING],
    folders: [],
    tasks: [],
  });

  /** App 的样子：onOpenRequirement 换掉 requirementId，同一个页面实例接着显示另一条 */
  function Harness({ apiClient, opened, canWrite = true }: { apiClient: ApiClient; opened: string[]; canWrite?: boolean }) {
    const [requirementId, setRequirementId] = useState("req-1");
    return (
      <RequirementDetailPage
        apiClient={apiClient}
        canPickFolders
        canWrite={canWrite}
        onBack={vi.fn()}
        onOpenMeeting={vi.fn()}
        onOpenProject={vi.fn()}
        onOpenRequirement={(id) => {
          opened.push(id);
          setRequirementId(id);
        }}
        onOpenTask={vi.fn()}
        projects={[]}
        requirementId={requirementId}
      />
    );
  }

  function renderHarness(api: Record<string, unknown>, canWrite = true) {
    const opened: string[] = [];
    render(<Harness apiClient={api as unknown as ApiClient} canWrite={canWrite} opened={opened} />);
    return opened;
  }

  function actions() {
    return within(document.querySelector(".requirement-detail__actions") as HTMLElement);
  }

  async function menuItems() {
    await userEvent.click(actions().getByRole("button", { name: "更多操作" }));
    return within(screen.getByRole("menu"))
      .getAllByRole("menuitem")
      .map((item) => item.textContent);
  }

  it("进行中：「编辑需求」旁边是［标记完成］，「⋯」里是搁置、并入其他需求…", async () => {
    renderHarness({ requirement: vi.fn().mockResolvedValue(jdDetail()) });
    await screen.findByRole("heading", { name: "京东科研仓对接" });

    const buttons = actions()
      .getAllByRole("button")
      .map((button) => button.textContent);
    expect(buttons).toEqual(["复制给 Claude Code", "编辑需求", "标记完成", "⋯"]);
    expect(await menuItems()).toEqual(["搁置", "并入其他需求…"]);
  });

  it("已完成、已搁置：按钮是［重新打开］，「⋯」里只有并入其他需求…", async () => {
    renderHarness({ requirement: vi.fn().mockResolvedValue(jdDetail({ status: "done" })) });
    await screen.findByRole("heading", { name: "京东科研仓对接" });
    expect(actions().getByRole("button", { name: "重新打开" })).toBeInTheDocument();
    expect(actions().queryByRole("button", { name: "标记完成" })).not.toBeInTheDocument();
    expect(await menuItems()).toEqual(["并入其他需求…"]);
    cleanup();

    renderHarness({ requirement: vi.fn().mockResolvedValue(jdDetail({ status: "shelved" })) });
    await screen.findByRole("heading", { name: "京东科研仓对接" });
    expect(actions().getByRole("button", { name: "重新打开" })).toBeInTheDocument();
    expect(await menuItems()).toEqual(["并入其他需求…"]);
  });

  it("没有写权限时没有这两个按钮", async () => {
    renderHarness({ requirement: vi.fn().mockResolvedValue(jdDetail()) }, false);
    await screen.findByRole("heading", { name: "京东科研仓对接" });
    expect(actions().queryByRole("button", { name: "标记完成" })).not.toBeInTheDocument();
    expect(actions().queryByRole("button", { name: "更多操作" })).not.toBeInTheDocument();
  });

  it("有没做完的待办时标记完成：用详情里的待办清单弹确认，［一起关掉］发 close_open_tasks: true；改完重读，盖上已完成的章", async () => {
    const requirement = vi
      .fn()
      .mockResolvedValueOnce(jdDetail({ tasks: OPEN, open_task_count: 2 }))
      .mockResolvedValue(jdDetail({ status: "done", tasks: OPEN, open_task_count: 0 }));
    const updateRequirement = vi.fn().mockResolvedValue(jdDetail({ status: "done", closed_task_count: 2 }));
    renderHarness({ requirement, updateRequirement });
    await screen.findByRole("heading", { name: "京东科研仓对接" });

    await userEvent.click(actions().getByRole("button", { name: "标记完成" }));
    const dialog = await screen.findByRole("alertdialog", { name: "还有 2 条待办没做完" });
    expect(within(dialog).getAllByRole("listitem").map((row) => row.textContent)).toEqual([
      "跟京东确认签收凭证怎么回传给医米",
      "整理四类接口的字段对照表",
    ]);
    // 待办清单就用页面上已有的，不再多取一次
    expect(requirement).toHaveBeenCalledTimes(1);
    await userEvent.click(within(dialog).getByRole("button", { name: "一起关掉" }));

    expect(updateRequirement).toHaveBeenCalledWith("req-1", { status: "done", close_open_tasks: true });
    expect(await screen.findByText("已完成「京东科研仓对接」，一起关掉 2 条待办")).toBeInTheDocument();
    expect(await screen.findByText("已完成", { selector: ".requirement-detail__stamp" })).toBeInTheDocument();
    expect(actions().getByRole("button", { name: "重新打开" })).toBeInTheDocument();
  });

  it("［待办留着］发 close_open_tasks: false", async () => {
    const requirement = vi.fn().mockResolvedValue(jdDetail({ tasks: OPEN, open_task_count: 2 }));
    const updateRequirement = vi.fn().mockResolvedValue(jdDetail({ status: "shelved" }));
    renderHarness({ requirement, updateRequirement });
    await screen.findByRole("heading", { name: "京东科研仓对接" });

    await userEvent.click(actions().getByRole("button", { name: "更多操作" }));
    await userEvent.click(screen.getByRole("menuitem", { name: "搁置" }));
    const dialog = await screen.findByRole("alertdialog", { name: "还有 2 条待办没做完" });
    await userEvent.click(within(dialog).getByRole("button", { name: "待办留着" }));
    expect(updateRequirement).toHaveBeenCalledWith("req-1", { status: "shelved", close_open_tasks: false });
    expect(await screen.findByText("已搁置「京东科研仓对接」")).toBeInTheDocument();
  });

  it("没有没做完的待办：直接改；［撤销］调 status-undo，重读回到原状态", async () => {
    const requirement = vi
      .fn()
      .mockResolvedValueOnce(jdDetail({ open_task_count: 0 }))
      .mockResolvedValueOnce(jdDetail({ status: "done" }))
      .mockResolvedValue(jdDetail());
    const updateRequirement = vi.fn().mockResolvedValue(jdDetail({ status: "done" }));
    const undoRequirementStatus = vi.fn().mockResolvedValue(jdDetail());
    renderHarness({ requirement, updateRequirement, undoRequirementStatus });
    await screen.findByRole("heading", { name: "京东科研仓对接" });

    await userEvent.click(actions().getByRole("button", { name: "标记完成" }));
    expect(updateRequirement).toHaveBeenCalledWith("req-1", { status: "done" });
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
    const toast = await screen.findByRole("status");
    expect(toast).toHaveTextContent("已完成「京东科研仓对接」");
    await waitFor(() => expect(actions().getByRole("button", { name: "重新打开" })).toBeInTheDocument());

    await userEvent.click(within(screen.getByRole("status")).getByRole("button", { name: "撤销" }));
    expect(undoRequirementStatus).toHaveBeenCalledWith("req-1");
    expect(await screen.findByText("「京东科研仓对接」回到进行中了")).toBeInTheDocument();
    await waitFor(() => expect(actions().getByRole("button", { name: "标记完成" })).toBeInTheDocument());
  });

  it("并入其他需求：成了跳到主需求的详情，提示「已并入「主需求」」带［撤销］；撤销后回到这条", async () => {
    const requirement = vi.fn((id: string) =>
      Promise.resolve(id === "requirement-receipt" ? MAIN : jdDetail()),
    );
    const requirementMergeTargets = vi.fn().mockResolvedValue(mergeTargetsPayload());
    const mergeRequirement = vi.fn().mockResolvedValue({
      ...MAIN,
      merged_from: { id: "req-1", title: "京东科研仓对接", undo_until: "2026-10-09T03:10:00+00:00" },
    });
    const undoRequirementMerge = vi.fn().mockResolvedValue(jdDetail());
    const opened = renderHarness({ requirement, requirementMergeTargets, mergeRequirement, undoRequirementMerge });
    await screen.findByRole("heading", { name: "京东科研仓对接" });

    await userEvent.click(actions().getByRole("button", { name: "更多操作" }));
    await userEvent.click(screen.getByRole("menuitem", { name: "并入其他需求…" }));
    const dialog = await screen.findByRole("dialog", { name: "把「京东科研仓对接」并入" });
    expect(requirementMergeTargets).toHaveBeenCalledWith("req-1", undefined);
    await userEvent.click(await within(dialog).findByRole("radio", { name: /京东仓签收凭证/ }));
    expect(within(dialog).getByText("合并后 P1（取较高）· 10 分钟内可撤销")).toBeInTheDocument();
    await userEvent.click(within(dialog).getByRole("button", { name: "并入" }));

    expect(mergeRequirement).toHaveBeenCalledWith("req-1", "requirement-receipt");
    expect(opened).toEqual(["requirement-receipt"]);
    expect(await screen.findByRole("heading", { name: "京东仓签收凭证" })).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("已并入「京东仓签收凭证」");

    await userEvent.click(within(screen.getByRole("status")).getByRole("button", { name: "撤销" }));
    expect(undoRequirementMerge).toHaveBeenCalledWith("req-1");
    expect(await screen.findByRole("heading", { name: "京东科研仓对接" })).toBeInTheDocument();
    expect(opened).toEqual(["requirement-receipt", "req-1"]);
    expect(screen.getByRole("status")).toHaveTextContent("已撤销合并");
  });
});

describe("RequirementDetailPage 打开被并掉的需求（D8）", () => {
  const mergedAway = () =>
    new ApiError("这条需求已并入「京东仓签收凭证」", 404, {
      detail: "这条需求已并入「京东仓签收凭证」",
      merged_into: { id: "requirement-receipt", title: "京东仓签收凭证" },
    });

  it("404 带着 merged_into：显示「这条需求已并入「X」」和［去看］，不显示需求不存在", async () => {
    const onOpenRequirement = vi.fn();
    const onBack = vi.fn();
    render(
      <RequirementDetailPage
        apiClient={{ requirement: vi.fn().mockRejectedValue(mergedAway()) } as unknown as ApiClient}
        canPickFolders
        canWrite
        onBack={onBack}
        onOpenMeeting={vi.fn()}
        onOpenProject={vi.fn()}
        onOpenRequirement={onOpenRequirement}
        onOpenTask={vi.fn()}
        projects={[]}
        requirementId="req-1"
      />,
    );

    expect(await screen.findByText("这条需求已并入「京东仓签收凭证」")).toBeInTheDocument();
    expect(screen.queryByText(/需求不存在/)).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "去看" }));
    expect(onOpenRequirement).toHaveBeenCalledWith("requirement-receipt");
    await userEvent.click(screen.getByRole("button", { name: "返回需求池" }));
    expect(onBack).toHaveBeenCalled();
  });

  it("页面上开着别的需求时换到被并掉的那条：不把上一条的内容留着冒充", async () => {
    const requirement = vi.fn((id: string) => (id === "req-1" ? Promise.resolve(jdDetail()) : Promise.reject(mergedAway())));
    const props = {
      apiClient: { requirement } as unknown as ApiClient,
      canPickFolders: true,
      canWrite: true,
      onBack: vi.fn(),
      onOpenMeeting: vi.fn(),
      onOpenProject: vi.fn(),
      onOpenRequirement: vi.fn(),
      onOpenTask: vi.fn(),
      projects: [],
    };
    const { rerender } = render(<RequirementDetailPage {...props} requirementId="req-1" />);
    await screen.findByRole("heading", { name: "京东科研仓对接" });

    rerender(<RequirementDetailPage {...props} requirementId="req-gone" />);
    expect(await screen.findByText("这条需求已并入「京东仓签收凭证」")).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "京东科研仓对接" })).not.toBeInTheDocument();
  });

  it("别的 404（没有 merged_into）照旧是「需求不存在或已删除」", async () => {
    render(
      <RequirementDetailPage
        apiClient={{ requirement: vi.fn().mockRejectedValue(new ApiError("需求不存在", 404, { detail: "需求不存在" })) } as unknown as ApiClient}
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
    expect(await screen.findByText(/需求不存在或已删除/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "去看" })).not.toBeInTheDocument();
  });
});

describe("RequirementDetailPage 常驻的撤销入口（F1）", () => {
  const NOW = new Date("2026-10-09T12:00:00+08:00");
  const inEight = new Date(NOW.getTime() + 8 * 60_000).toISOString();

  beforeEach(() => {
    vi.useFakeTimers({ shouldAdvanceTime: true, now: NOW });
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  function renderDetail(api: Record<string, unknown>, extra: Partial<Parameters<typeof RequirementDetailPage>[0]> = {}) {
    const onOpenRequirement = vi.fn();
    render(
      <RequirementDetailPage
        apiClient={api as unknown as ApiClient}
        canPickFolders
        canWrite
        onBack={vi.fn()}
        onOpenMeeting={vi.fn()}
        onOpenProject={vi.fn()}
        onOpenRequirement={onOpenRequirement}
        onOpenTask={vi.fn()}
        projects={[]}
        requirementId="req-1"
        {...extra}
      />,
    );
    return onOpenRequirement;
  }

  const header = () => within(document.querySelector(".requirement-detail__actions") as HTMLElement);

  it.each([
    ["done", "撤销标记完成", "重新打开"],
    ["shelved", "撤销搁置", "重新打开"],
    ["active", "撤销重新打开", "标记完成"],
  ] as const)("%s、status_undo_until 没过：状态按钮旁有「%s」", async (status, label, statusButton) => {
    renderDetail({ requirement: vi.fn().mockResolvedValue(jdDetail({ status, status_undo_until: inEight })) });
    await screen.findByRole("heading", { name: "京东科研仓对接" });
    const undo = header().getByRole("button", { name: label });
    expect(undo).toHaveClass("requirement-detail__status-undo");
    expect(undo.previousElementSibling).toHaveTextContent(statusButton);
  });

  it("点了调 status-undo，提示回到哪个状态，重读后撤销入口跟着没了", async () => {
    const requirement = vi
      .fn()
      .mockResolvedValueOnce(jdDetail({ status: "done", status_undo_until: inEight }))
      .mockResolvedValue(jdDetail({ status: "active", status_undo_until: null }));
    const undoRequirementStatus = vi.fn().mockResolvedValue(jdDetail({ status: "active" }));
    renderDetail({ requirement, undoRequirementStatus });
    await screen.findByRole("heading", { name: "京东科研仓对接" });

    await userEvent.click(header().getByRole("button", { name: "撤销标记完成" }));
    expect(undoRequirementStatus).toHaveBeenCalledWith("req-1");
    expect(await screen.findByText("「京东科研仓对接」回到进行中了")).toBeInTheDocument();
    await waitFor(() => expect(header().getByRole("button", { name: "标记完成" })).toBeInTheDocument());
    expect(header().queryByRole("button", { name: /^撤销/ })).not.toBeInTheDocument();
  });

  it("到点自己消失，不用刷新页面；没有 status_undo_until、已经过了、没有写权限都不显示", async () => {
    renderDetail({ requirement: vi.fn().mockResolvedValue(jdDetail({ status: "done", status_undo_until: inEight })) });
    await screen.findByRole("heading", { name: "京东科研仓对接" });
    expect(header().getByRole("button", { name: "撤销标记完成" })).toBeInTheDocument();
    act(() => {
      vi.advanceTimersByTime(8 * 60_000 + 1_000);
    });
    expect(header().queryByRole("button", { name: "撤销标记完成" })).not.toBeInTheDocument();
    cleanup();

    renderDetail({ requirement: vi.fn().mockResolvedValue(jdDetail({ status: "done", status_undo_until: null })) });
    await screen.findByRole("heading", { name: "京东科研仓对接" });
    expect(header().queryByRole("button", { name: /^撤销/ })).not.toBeInTheDocument();
    cleanup();

    const past = new Date(NOW.getTime() - 60_000).toISOString();
    renderDetail({ requirement: vi.fn().mockResolvedValue(jdDetail({ status: "done", status_undo_until: past })) });
    await screen.findByRole("heading", { name: "京东科研仓对接" });
    expect(header().queryByRole("button", { name: /^撤销/ })).not.toBeInTheDocument();
    cleanup();

    renderDetail({ requirement: vi.fn().mockResolvedValue(jdDetail({ status: "done", status_undo_until: inEight })) }, { canWrite: false });
    await screen.findByRole("heading", { name: "京东科研仓对接" });
    expect(screen.queryByRole("button", { name: "撤销标记完成" })).not.toBeInTheDocument();
  });

  it("来源卡上需求并进来的原话：［撤销合并］调需求的 unmerge，提示后跳到恢复出来的那条", async () => {
    const mark = { kind: "requirement" as const, requirement_id: "requirement-old", title: "京东仓签收凭证（旧）", until: inEight };
    const merged: RequirementSource = { ...JD_MERGED, via_candidate_title: "京东仓签收凭证（旧）", undo_merge: mark };
    const undoRequirementMerge = vi.fn().mockResolvedValue(jdDetail({ id: "requirement-old", title: "京东仓签收凭证（旧）" }));
    const undoCandidateMerge = vi.fn();
    const onOpenRequirement = renderDetail({
      requirement: vi.fn().mockResolvedValue(jdDetail({ sources: [JD_ORIGIN, merged] })),
      undoRequirementMerge,
      undoCandidateMerge,
    });

    const card = within(await screen.findByRole("region", { name: "来源" }));
    const button = card.getByRole("button", { name: "撤销合并" });
    expect(button.closest("li")).toHaveTextContent("合并自「京东仓签收凭证（旧）」");
    await userEvent.click(button);

    expect(undoRequirementMerge).toHaveBeenCalledWith("requirement-old");
    expect(undoCandidateMerge).not.toHaveBeenCalled();
    expect(await screen.findByText("已撤销合并")).toBeInTheDocument();
    expect(onOpenRequirement).toHaveBeenCalledWith("requirement-old");
  });

  it("来源卡上候选并进来的原话：照旧调候选的 unmerge，留在这条需求上", async () => {
    const mark = { kind: "candidate" as const, candidate_id: "candidate-receipt", until: inEight };
    const merged: RequirementSource = { ...JD_MERGED, undo_merge: mark };
    const undoCandidateMerge = vi.fn().mockResolvedValue({});
    const undoRequirementMerge = vi.fn();
    const onOpenRequirement = renderDetail({
      requirement: vi.fn().mockResolvedValue(jdDetail({ sources: [JD_ORIGIN, merged] })),
      undoRequirementMerge,
      undoCandidateMerge,
    });

    const card = within(await screen.findByRole("region", { name: "来源" }));
    expect(card.getByRole("button", { name: "撤销合并" }).closest("li")).toHaveTextContent("合并自候选「京东仓签收凭证」");
    await userEvent.click(card.getByRole("button", { name: "撤销合并" }));
    expect(undoCandidateMerge).toHaveBeenCalledWith("candidate-receipt");
    expect(undoRequirementMerge).not.toHaveBeenCalled();
    expect(onOpenRequirement).not.toHaveBeenCalled();
  });

  it("需求合并撤销被拒（过了时限）：红色提示后端的原因，不跳走", async () => {
    const mark = { kind: "requirement" as const, requirement_id: "requirement-old", title: "京东仓签收凭证（旧）", until: inEight };
    const merged: RequirementSource = { ...JD_MERGED, undo_merge: mark };
    const undoRequirementMerge = vi
      .fn()
      .mockRejectedValue(new ApiError("合并超过 10 分钟，不能撤销了", 409, { detail: "合并超过 10 分钟，不能撤销了" }));
    const onOpenRequirement = renderDetail({
      requirement: vi.fn().mockResolvedValue(jdDetail({ sources: [JD_ORIGIN, merged] })),
      undoRequirementMerge,
    });

    const card = within(await screen.findByRole("region", { name: "来源" }));
    await userEvent.click(card.getByRole("button", { name: "撤销合并" }));
    await waitFor(() => expect(document.querySelector(".app-toast--error")).toHaveTextContent("合并超过 10 分钟，不能撤销了"));
    expect(onOpenRequirement).not.toHaveBeenCalled();
  });
});

describe("RequirementDetailPage 旧标签页里改已经并走的需求（F3）", () => {
  it("点标记完成被拒：提示后端的原因，重读后页面换成「已并入」", async () => {
    const gone = new ApiError("这条需求已并入「京东仓签收凭证」", 404, {
      detail: "这条需求已并入「京东仓签收凭证」",
      merged_into: { id: "requirement-receipt", title: "京东仓签收凭证" },
    });
    const requirement = vi.fn().mockResolvedValueOnce(jdDetail()).mockRejectedValue(gone);
    const updateRequirement = vi.fn().mockRejectedValue(gone);
    render(
      <RequirementDetailPage
        apiClient={{ requirement, updateRequirement } as unknown as ApiClient}
        canPickFolders
        canWrite
        onBack={vi.fn()}
        onOpenMeeting={vi.fn()}
        onOpenProject={vi.fn()}
        onOpenRequirement={vi.fn()}
        onOpenTask={vi.fn()}
        projects={[]}
        requirementId="req-1"
      />,
    );
    await screen.findByRole("heading", { name: "京东科研仓对接" });

    await userEvent.click(screen.getByRole("button", { name: "标记完成" }));
    await waitFor(() => expect(document.querySelector(".app-toast--error")).toHaveTextContent("这条需求已并入「京东仓签收凭证」"));
    expect(await screen.findByText("这条需求已并入「京东仓签收凭证」", { selector: ".requirement-detail__state" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "去看" })).toBeInTheDocument();
  });
});
