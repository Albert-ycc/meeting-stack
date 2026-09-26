import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { ApiClient } from "../api";
import { cardEffectNote, reassignNote } from "../cardCopy";
import type { MeetingCard } from "../types";
import { MeetingCardStatus } from "./MeetingCardStatus";

function card(overrides: Partial<MeetingCard> = {}): MeetingCard {
  return {
    state: "pending",
    reason: "queued",
    category: "waiting",
    path: null,
    synced_at: null,
    error: null,
    ...overrides,
  };
}

function renderStatus(value: MeetingCard, apiClient: Partial<ApiClient> = {}, onOpenProject = vi.fn()) {
  const onCardChange = vi.fn();
  render(
    <MeetingCardStatus
      apiClient={apiClient as ApiClient}
      card={value}
      meetingId="vm-1"
      onCardChange={onCardChange}
      onOpenProject={onOpenProject}
      projectId="project-a"
      projectName="云图AI"
    />,
  );
  return { onCardChange, onOpenProject };
}

describe("会议页卡片状态条", () => {
  it("已写入：显示项目文件夹下的相对路径，按钮复制卡片路径", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText } });
    const path = "/Volumes/资料盘/云图AI/声档会议记录/260926 初审规则沟通.md";
    renderStatus(card({ state: "synced", reason: null, category: "ok", path, synced_at: "2026-09-26T09:02:00Z" }));

    expect(screen.getByRole("status")).toHaveTextContent("项目卡片已写入云图AI/声档会议记录/260926 初审规则沟通.md");
    await userEvent.click(screen.getByRole("button", { name: "复制卡片路径" }));
    expect(writeText).toHaveBeenCalledWith(path);
  });

  it("等待中只说在等什么，不给按钮", () => {
    renderStatus(card({ reason: "waiting_minutes" }));

    expect(screen.getByRole("status")).toHaveTextContent("等待中等纪要生成");
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
  });

  it("项目没挂文件夹时带去项目页挂文件夹", async () => {
    const { onOpenProject } = renderStatus(card({ state: "blocked", reason: "no_root" }));

    expect(screen.getByRole("status")).toHaveTextContent("项目「云图AI」还没挂文件夹");
    await userEvent.click(screen.getByRole("button", { name: "去挂文件夹" }));
    expect(onOpenProject).toHaveBeenCalledWith("project-a");
  });

  it("你改过的卡片：用最新纪要重写，带回新状态", async () => {
    const synced = card({ state: "synced", reason: null, category: "ok", path: "/r/云图AI/声档会议记录/a.md" });
    const meetingCardAction = vi.fn().mockResolvedValue(synced);
    const { onCardChange } = renderStatus(card({ state: "user_edited", reason: null, category: "stopped" }), {
      meetingCardAction,
    });

    expect(screen.getByRole("status")).toHaveTextContent("已停止你改过这张卡的纪要部分，已停止自动更新");
    await userEvent.click(screen.getByRole("button", { name: "用最新纪要重写" }));
    expect(meetingCardAction).toHaveBeenCalledWith("vm-1", "rewrite");
    expect(onCardChange).toHaveBeenCalledWith(synced);
  });

  it("项目暂停写卡片：恢复写入后重读这场会的卡片", async () => {
    const resumeProjectCards = vi.fn().mockResolvedValue({ cards: {}, written: 1 });
    const fresh = card({ state: "synced", reason: null, category: "ok", path: "/r/云图AI/声档会议记录/a.md" });
    const meetingCard = vi.fn().mockResolvedValue(fresh);
    const { onCardChange } = renderStatus(card({ state: "blocked", reason: "paused", category: "stopped" }), {
      resumeProjectCards,
      meetingCard,
    });

    await userEvent.click(screen.getByRole("button", { name: "恢复写入" }));
    expect(resumeProjectCards).toHaveBeenCalledWith("project-a");
    expect(onCardChange).toHaveBeenCalledWith(fresh);
  });

  it("操作失败时把原因写在状态条里", async () => {
    const meetingCardAction = vi.fn().mockRejectedValue(new Error("资料盘没连接"));
    renderStatus(card({ state: "missing", reason: null, category: "stopped" }), { meetingCardAction });

    await userEvent.click(screen.getByRole("button", { name: "重新生成" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("资料盘没连接");
  });
});

describe("改归属提示里的卡片去向", () => {
  it("任务和卡片一起移过去", () => {
    const moved = { action: "moved", from: "云图AI/声档会议记录/a.md", to: "数据中台/声档会议记录/a.md", reason: null } as const;
    expect(reassignNote(3, 0, moved)).toBe("3 条任务和会议卡片一起移过去");
    expect(reassignNote(0, 1, moved)).toBe("会议卡片一起移过去；1 条任务挂在原项目的需求上，留在原处");
  });

  it("标为不归项目时卡片撤下，笔记进回收区", () => {
    expect(
      reassignNote(0, 0, { action: "retired", from: "云图AI/声档会议记录/a.md", to: null, reason: "waiting_project" }),
    ).toBe("会议卡片已从「云图AI」文件夹撤下，你的笔记一并存进了回收区");
  });

  it("新项目没挂文件夹、第一次写卡片", () => {
    expect(cardEffectNote({ action: "waiting", from: null, to: null, reason: "no_root" })).toBe(
      "项目还没挂文件夹，会议卡片先存着",
    );
    expect(cardEffectNote({ action: "written", from: null, to: "数据中台/声档会议记录/a.md", reason: null })).toBe(
      "会议卡片已写进「数据中台」文件夹",
    );
    expect(cardEffectNote({ action: "none", from: null, to: null, reason: null })).toBe("");
  });
});
