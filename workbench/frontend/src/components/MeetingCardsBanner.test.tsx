import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { ApiClient } from "../api";
import type { CardsBanner, ProjectCardsSummary } from "../types";
import { FolderSuggestionBanner } from "./FolderSuggestionBanner";
import { MeetingCardsBanner } from "./MeetingCardsBanner";
import { CLAUDE_CODE_HINT, ProjectCardsRow } from "./ProjectCardsRow";

const BACKFILL: CardsBanner = {
  backfill: {
    meetings: 186,
    projects: 12,
    ai_attributed: 41,
    no_folder_projects: 3,
    no_folder_meetings: 9,
    top: [
      { project_id: "p1", project_name: "云图AI", count: 40, path: "/Volumes/资料盘/云图AI/声档会议记录", paused: false },
      { project_id: "p2", project_name: "北辰", count: 9, path: null, paused: false },
    ],
  },
  notices: [],
};

function client(banner: CardsBanner, overrides: Partial<ApiClient> = {}) {
  return {
    cardsBanner: vi.fn().mockResolvedValue(banner),
    answerCardsBackfill: vi.fn().mockResolvedValue({ answer: "yes" }),
    retireAllCards: vi.fn().mockResolvedValue({ retired: 5, kept: [] }),
    dismissCardsNotice: vi.fn().mockResolvedValue({ ok: true }),
    pauseProjectCards: vi.fn().mockResolvedValue({ retired: 1 }),
    revealCards: vi.fn().mockResolvedValue({ path: "/x" }),
    ...overrides,
  } as unknown as ApiClient;
}

describe("工作台会议卡片横幅", () => {
  it("问要不要补写历史会议，写入后给［全部撤下］", async () => {
    const apiClient = client(BACKFILL);
    render(<MeetingCardsBanner apiClient={apiClient} />);

    expect(
      await screen.findByText(/可以把已归项目的 186 场会写成卡片，放进 12 个项目文件夹.*其中 41 场的项目是 AI 自动归的.*另有 3 个项目还没挂文件夹/),
    ).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "看看写到哪" }));
    expect(screen.getByText("/Volumes/资料盘/云图AI/声档会议记录/")).toBeInTheDocument();
    expect(screen.getByText("还没挂文件夹，先存着")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "写入" }));
    expect(apiClient.answerCardsBackfill).toHaveBeenCalledWith("yes");
    expect(await screen.findByText(/接下来的扫描会把 186 场会写成卡片/)).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "全部撤下" }));
    expect(apiClient.retireAllCards).toHaveBeenCalled();
    expect(await screen.findByText("已撤下 5 张会议卡片，会议卡片已关闭")).toBeInTheDocument();
  });

  it("［稍后］只收起横幅", async () => {
    const apiClient = client(BACKFILL);
    render(<MeetingCardsBanner apiClient={apiClient} />);

    await userEvent.click(await screen.findByRole("button", { name: "稍后" }));
    expect(apiClient.answerCardsBackfill).toHaveBeenCalledWith("later");
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("第一张卡片先报，排在补写之前；［这个项目不要写］暂停这个项目", async () => {
    const apiClient = client({
      ...BACKFILL,
      notices: [{ project_id: "p1", project_name: "云图AI", path: "/Volumes/资料盘/云图AI/声档会议记录", at: "" }],
    });
    render(<MeetingCardsBanner apiClient={apiClient} />);

    expect(await screen.findByText("已在 云图AI/声档会议记录/ 写入第一张会议卡片，给你和 Claude Code 看")).toBeInTheDocument();
    expect(screen.queryByText(/可以把已归项目/)).not.toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "这个项目不要写" }));
    expect(apiClient.pauseProjectCards).toHaveBeenCalledWith("p1");
    // 报完才轮到补写
    expect(await screen.findByText(/可以把已归项目的 186 场会/)).toBeInTheDocument();
  });

  it("同名文件夹横幅读完之后才告诉工作台它在不在", async () => {
    const onActiveChange = vi.fn();
    const apiClient = { coldStartFolders: vi.fn().mockResolvedValue({ items: [], snoozed_until: null }) } as unknown as ApiClient;
    render(<FolderSuggestionBanner apiClient={apiClient} onActiveChange={onActiveChange} />);

    await vi.waitFor(() => expect(onActiveChange).toHaveBeenCalledWith(false));
    expect(onActiveChange).not.toHaveBeenCalledWith(true);
  });
});

function summary(overrides: Partial<ProjectCardsSummary> = {}): ProjectCardsSummary {
  return {
    root: "/Volumes/资料盘/云图AI/声档会议记录",
    index_path: "/Volumes/资料盘/云图AI/声档会议记录/00 索引.md",
    written: 42,
    edited: 0,
    missing: 0,
    waiting: 3,
    waiting_reason: "root_offline",
    paused: false,
    history: 0,
    ...overrides,
  };
}

describe("项目页会议卡片汇总", () => {
  it("写在哪、写了几张、几张在等，外加给 Claude Code 的一句话", async () => {
    const onCopy = vi.fn();
    render(
      <ProjectCardsRow
        apiClient={{} as ApiClient}
        canPickFolders
        canWrite
        cards={summary()}
        onChanged={vi.fn()}
        onCopy={onCopy}
        projectId="p1"
      />,
    );

    expect(
      screen.getByText("会议卡片写在 /Volumes/资料盘/云图AI/声档会议记录/ · 已写 42 张 · 3 张等待（资料盘未连接）"),
    ).toBeInTheDocument();
    expect(screen.getByText(CLAUDE_CODE_HINT)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "复制" }));
    expect(onCopy).toHaveBeenCalledWith(CLAUDE_CODE_HINT, "已复制");
  });

  it("暂停了可以恢复写入，上线前的会可以补写", async () => {
    const resumeProjectCards = vi.fn().mockResolvedValue({ cards: summary(), written: 2 });
    const answerCardsBackfill = vi.fn().mockResolvedValue({ answer: "yes" });
    const onChanged = vi.fn();
    render(
      <ProjectCardsRow
        apiClient={{ resumeProjectCards, answerCardsBackfill } as unknown as ApiClient}
        canPickFolders
        canWrite
        cards={summary({ waiting_reason: "paused", paused: true, written: 0, history: 7 })}
        onChanged={onChanged}
        onCopy={vi.fn()}
        projectId="p1"
      />,
    );

    await userEvent.click(screen.getByRole("button", { name: "恢复写入" }));
    expect(resumeProjectCards).toHaveBeenCalledWith("p1");
    expect(await screen.findByText("已恢复，补写了 2 张会议卡片")).toBeInTheDocument();
    expect(onChanged).toHaveBeenCalled();

    expect(screen.getByText("另有 7 场上线前的会没写卡片")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "补写历史卡片" }));
    expect(answerCardsBackfill).toHaveBeenCalledWith("yes");
  });

  it("不在 Mac 上打不开访达时退回复制文件夹路径", async () => {
    const onCopy = vi.fn();
    const revealCards = vi.fn().mockRejectedValue(new Error("只能在运行声档的 Mac 上打开文件夹"));
    render(
      <ProjectCardsRow
        apiClient={{ revealCards } as unknown as ApiClient}
        canPickFolders
        canWrite
        cards={summary({ waiting_reason: null, waiting: 0 })}
        onChanged={vi.fn()}
        onCopy={onCopy}
        projectId="p1"
      />,
    );

    await userEvent.click(screen.getByRole("button", { name: "打开文件夹" }));
    expect(onCopy).toHaveBeenCalledWith("/Volumes/资料盘/云图AI/声档会议记录", "已复制卡片文件夹路径");
  });
});
