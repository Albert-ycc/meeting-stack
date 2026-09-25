import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { LibraryPage } from "./LibraryPage";
import type { AttentionPayload } from "../types";

const ATTENTION: AttentionPayload = {
  jobs: [
    {
      job_id: "job-transcribe",
      status: "failed",
      stage: "transcribing",
      kind: "transcription",
      summary: "转写没有跑完",
      next_step: "到「转写录音」页对这条点「重新转写」；确认是坏录音就归档",
      created_at: "2026-08-01T04:07:19Z",
      updated_at: "2026-08-06T15:54:52Z",
    },
    {
      job_id: "job-minutes",
      status: "failed",
      stage: "codex_callback",
      kind: "minutes",
      summary: "逐字稿已经好了，纪要没生成出来",
      next_step: "到「转写录音」页对这条点「重新生成纪要」",
      meeting_id: "vm-20260903",
      meeting_title: "远山患者平台原型评审",
      created_at: "2026-09-03T09:47:57Z",
      updated_at: "2026-09-03T13:30:32Z",
    },
  ],
  jobs_available: true,
  acknowledged_count: 0,
  quarantined: [
    {
      directory: "/archive/260903 远山患者平台原型评审",
      name: "260903 远山患者平台原型评审",
      summary: "会议文件夹没能导入",
      reason: "受管任务状态与预期不符：failed",
      next_step: "对应的转写任务没有正常收尾，处理完「转写录音」里的那条后会自动导入",
    },
  ],
};

function renderLibrary(props: Partial<Parameters<typeof LibraryPage>[0]> = {}) {
  const onOpen = vi.fn();
  render(
    <LibraryPage
      filters={{}}
      limit={50}
      meetings={[]}
      offset={0}
      onFilter={vi.fn()}
      onOpen={onOpen}
      onPageChange={vi.fn()}
      projects={[]}
      state="empty"
      tags={[]}
      total={0}
      attention={ATTENTION}
      {...props}
    />,
  );
  return { onOpen };
}

describe("资料库「需要处理」", () => {
  it("失败任务和隔离目录都写清原因与下一步", () => {
    renderLibrary();

    const panel = screen.getByRole("region", { name: "需要处理" });
    expect(panel).toHaveTextContent("3 条录音没能正常入库");
    expect(panel).toHaveTextContent("转写没有跑完");
    expect(panel).toHaveTextContent("下一步：到「转写录音」页对这条点「重新转写」");
    expect(panel).toHaveTextContent("260903 远山患者平台原型评审");
    expect(panel).toHaveTextContent("会议文件夹没能导入：受管任务状态与预期不符：failed");
  });

  it("桌面端可以把一条失败任务确认归档", async () => {
    const onAcknowledgeJob = vi.fn().mockResolvedValue(undefined);
    renderLibrary({ onAcknowledgeJob });

    await userEvent.click(screen.getAllByRole("button", { name: "知道了，归档" })[0]);

    expect(onAcknowledgeJob).toHaveBeenCalledWith("job-transcribe");
  });

  it("关联到会议的失败任务可以直接打开会议", async () => {
    const { onOpen } = renderLibrary();

    await userEvent.click(screen.getByRole("button", { name: "打开会议" }));

    expect(onOpen).toHaveBeenCalledWith("vm-20260903");
  });

  it("移动端只读：不给归档和去处理按钮", () => {
    renderLibrary({ onAcknowledgeJob: undefined, onOpenJobs: undefined });

    expect(screen.queryByRole("button", { name: "知道了，归档" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "去处理" })).not.toBeInTheDocument();
  });

  it("筛「失败」没有会议记录时指向上方清单，而不是说什么都没有", () => {
    renderLibrary({ filters: { status: "failed" } });

    expect(
      screen.getByText("没有处理失败的会议记录；没能入库的录音列在上方「需要处理」里。"),
    ).toBeInTheDocument();
  });

  it("没有需要处理的录音时不渲染这一块", () => {
    renderLibrary({ attention: { jobs: [], jobs_available: true, acknowledged_count: 2, quarantined: [] } });

    expect(screen.queryByRole("region", { name: "需要处理" })).not.toBeInTheDocument();
  });
});
