import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { Job } from "../types";
import { JobsPage } from "./JobsPage";

const job: Job = {
  id: "job-1",
  meeting_id: "vm-1",
  state: "completed_unreviewed",
  whisper_status: "failed",
  index_status: "ready",
  substates: {
    whisper: { status: "failed", error: "Whisper 显存不足", attempt: 1 },
    index: { status: "ready", attempt: 1 },
    qwen: { status: "queued", attempt: 1 },
  },
  created_at: "2026-07-10T00:00:00Z",
  updated_at: "2026-07-10T00:01:00Z",
};

describe("JobsPage non-blocking substates", () => {
  it("shows Whisper and index separately without turning the main chain into a failure", async () => {
    const onRetrySubstate = vi.fn().mockResolvedValue(undefined);
    render(
      <JobsPage
        available
        jobs={[job]}
        onCancel={vi.fn()}
        onRetry={vi.fn()}
        onRetrySubstate={onRetrySubstate}
        onStopAfterStage={vi.fn()}
        onUpload={vi.fn()}
        state="ready"
      />,
    );

    expect(screen.getAllByText("已完成").length).toBeGreaterThan(0);
    expect(screen.getByText("Whisper 对照稿")).toBeInTheDocument();
    expect(screen.getByText("检索索引")).toBeInTheDocument();
    expect(screen.getByText("离线 Qwen 影子稿")).toBeInTheDocument();
    expect(screen.getByText("已排队")).toBeInTheDocument();
    expect(screen.getByText("Whisper 显存不足")).toBeInTheDocument();
    expect(screen.queryByText("transcribing：")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "重试检索索引" })).not.toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "重试 Whisper 对照稿" }));
    expect(onRetrySubstate).toHaveBeenCalledWith("job-1", "whisper");
  });

  it("passes normalized optional hotwords through upload and transcription retry", async () => {
    const onUpload = vi.fn().mockResolvedValue("已入队");
    const onRetry = vi.fn().mockResolvedValue(undefined);
    const retryable = { ...job, state: "failed" as const, failure_stage: "transcribing" };
    render(
      <JobsPage
        available
        jobs={[retryable]}
        onCancel={vi.fn()}
        onRetry={onRetry}
        onRetrySubstate={vi.fn()}
        onStopAfterStage={vi.fn()}
        onUpload={onUpload}
        state="ready"
      />,
    );

    await userEvent.type(screen.getByLabelText("手工导入本场热词"), " ＡＣＭＥ, acme\n云图");
    await userEvent.upload(
      screen.getByLabelText("选择录音文件"),
      new File(["audio"], "meeting.m4a", { type: "audio/mp4" }),
    );
    expect(onUpload).toHaveBeenCalledWith(expect.any(File), ["ACME", "云图"]);
    expect(screen.getByLabelText("手工导入本场热词")).toHaveValue("");
    await userEvent.upload(
      screen.getByLabelText("选择录音文件"),
      new File(["second"], "second.m4a", { type: "audio/mp4" }),
    );
    expect(onUpload).toHaveBeenLastCalledWith(expect.any(File), []);

    await userEvent.type(screen.getByLabelText("job-1 重试热词"), "MDT，mdt\n术语");
    await userEvent.click(screen.getByRole("button", { name: "重新转写" }));
    expect(onRetry).toHaveBeenCalledWith("job-1", "transcribing", ["MDT", "术语"]);
  });

  it("retains upload hotwords after failure and clears them only after a successful retry", async () => {
    const onUpload = vi.fn().mockRejectedValueOnce(new Error("上传失败")).mockResolvedValueOnce("已入队");
    render(<JobsPage available jobs={[]} onCancel={vi.fn()} onRetry={vi.fn()} onRetrySubstate={vi.fn()} onStopAfterStage={vi.fn()} onUpload={onUpload} state="empty" />);
    const hotwords = screen.getByLabelText("手工导入本场热词");
    const picker = screen.getByLabelText("选择录音文件");
    await userEvent.type(hotwords, "ACME，云图");
    await userEvent.upload(picker, new File(["first"], "first.m4a", { type: "audio/mp4" }));
    expect(await screen.findByRole("status")).toHaveTextContent("上传失败");
    expect(hotwords).toHaveValue("ACME，云图");
    await userEvent.upload(picker, new File(["retry"], "retry.m4a", { type: "audio/mp4" }));
    expect(onUpload).toHaveBeenLastCalledWith(expect.any(File), ["ACME", "云图"]);
    expect(hotwords).toHaveValue("");
  });

  it("blocks a local hotword list above twenty terms before uploading", async () => {
    const onUpload = vi.fn();
    render(
      <JobsPage
        available
        jobs={[]}
        onCancel={vi.fn()}
        onRetry={vi.fn()}
        onRetrySubstate={vi.fn()}
        onStopAfterStage={vi.fn()}
        onUpload={onUpload}
        state="empty"
      />,
    );
    await userEvent.type(
      screen.getByLabelText("手工导入本场热词"),
      Array.from({ length: 21 }, (_, index) => `词${index}`).join(","),
    );
    expect(screen.getByText("本场热词最多 20 个")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "＋ 手工导入录音" })).toBeDisabled();
  });

  it("limits the file picker to the three supported recording formats", () => {
    render(
      <JobsPage
        available
        jobs={[]}
        onCancel={vi.fn()}
        onRetry={vi.fn()}
        onRetrySubstate={vi.fn()}
        onStopAfterStage={vi.fn()}
        onUpload={vi.fn()}
        state="empty"
      />,
    );

    expect(screen.getByLabelText("选择录音文件")).toHaveAttribute(
      "accept",
      ".m4a,.mp3,.wav",
    );
  });

  it("does not offer stop-after-stage for any terminal job", () => {
    render(
      <JobsPage
        available
        jobs={[job]}
        onCancel={vi.fn()}
        onRetry={vi.fn()}
        onRetrySubstate={vi.fn()}
        onStopAfterStage={vi.fn()}
        onUpload={vi.fn()}
        state="ready"
      />,
    );

    expect(
      screen.queryByRole("button", { name: "当前阶段结束后停止" }),
    ).not.toBeInTheDocument();
  });

  it("keeps the last jobs visible and marks them stale when refresh fails", () => {
    render(
      <JobsPage
        available
        jobs={[job]}
        message="任务台账暂时不可用"
        onCancel={vi.fn()}
        onRetry={vi.fn()}
        onRetrySubstate={vi.fn()}
        onStopAfterStage={vi.fn()}
        onUpload={vi.fn()}
        stale
        state="error"
      />,
    );

    expect(screen.getByText("vm-1")).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("显示上次成功读取的任务");
  });

  it("shows an honest error when no successful job snapshot exists", () => {
    render(
      <JobsPage
        available
        jobs={[]}
        message="任务台账暂时不可用"
        onCancel={vi.fn()}
        onRetry={vi.fn()}
        onRetrySubstate={vi.fn()}
        onStopAfterStage={vi.fn()}
        onUpload={vi.fn()}
        state="error"
      />,
    );

    expect(screen.getByRole("alert")).toHaveTextContent("任务台账暂时不可用");
    expect(screen.queryByText(/当前没有任务/)).not.toBeInTheDocument();
  });
});
