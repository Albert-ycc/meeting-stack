import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";

import { ApiError, type ApiClient } from "../api";
import type { MeetingAttribution, MeetingDetail, Segment } from "../types";
import { MeetingDetailPage } from "./MeetingDetailPage";

const mainSegment: Segment = {
  id: "seg-main",
  ordinal: 0,
  start_ms: 0,
  end_ms: 5_000,
  speaker_name: "甲",
  text: "这是 FunASR 当前工作稿",
};

function meeting(withReference = true): MeetingDetail {
  return {
    id: "vm-1",
    title: "对照测试会议",
    status: "completed_unreviewed",
    tags: [],
    artifacts: [],
    segments: [mainSegment],
    speakers: [],
    events: [],
    current_transcript_version_id: "tv-main",
    transcript_versions: [
      {
        id: "tv-main",
        meeting_id: "vm-1",
        version_no: 4,
        kind: "funasr",
        published: 0,
        created_at: "2026-07-10T00:00:00Z",
      },
      ...(withReference
        ? [
            {
              id: "tv-whisper-old",
              meeting_id: "vm-1",
              version_no: 2,
              kind: "whisper_reference",
              published: 0,
              created_at: "2026-07-09T00:00:00Z",
            },
            {
              id: "tv-whisper-new",
              meeting_id: "vm-1",
              version_no: 3,
              kind: "whisper_reference",
              published: 0,
              created_at: "2026-07-10T00:00:00Z",
            },
          ]
        : []),
    ],
    minutes_versions: [],
  };
}

function client(readVersion: ApiClient["transcriptVersionSegments"]): ApiClient {
  return { transcriptVersionSegments: readVersion } as unknown as ApiClient;
}

describe("MeetingDetailPage Whisper comparison", () => {
  it("loads the comparison contract for risk review and keeps the candidate tied to its version", async () => {
    const transcriptComparison = vi.fn().mockResolvedValue({
      primary_version_id: "tv-main",
      candidate_version_id: "tv-whisper-new",
      candidate_kind: "whisper_reference",
      items: [{
        primary_segment_id: "seg-main",
        candidate_segment_ids: ["seg-ref"],
        start_ms: 0,
        end_ms: 5_000,
        primary_text: "金额 125 万元 CRM",
        candidate_text: "金额 120 万元 SCRM",
        risk_kinds: ["number", "latin_term"],
        similarity: 0.8,
      }],
    });
    render(
      <MeetingDetailPage
        apiClient={{ transcriptComparison, asrGoldSamples: vi.fn().mockResolvedValue({ items: [] }) } as unknown as ApiClient}
        initialSeekMs={0}
        isMobile={false}
        meeting={meeting()}
        onBack={vi.fn()}
        onReload={vi.fn().mockResolvedValue(undefined)}
        projects={[]}
        tags={[]}
      />,
    );
    await userEvent.click(screen.getByRole("button", { name: "Whisper 对照" }));
    expect(transcriptComparison).toHaveBeenCalledWith("vm-1", "tv-whisper-new");
    expect(await screen.findByText("数字差异")).toBeInTheDocument();
    expect(screen.getByText("英文术语差异")).toBeInTheDocument();
  });

  it("updates a gold reference without erasing its existing annotations", async () => {
    const saved = {
      id: "gold-1", meeting_id: "vm-1", segment_id: "seg-main", start_ms: 0, end_ms: 5_000,
      reference: "旧金标", entities: ["ACME"], numbers: ["125"], tags: ["术语"],
      created_at: "2026-07-14T00:00:00Z", updated_at: "2026-07-14T00:00:00Z",
    };
    const saveAsrGoldSample = vi.fn().mockImplementation(async (_meetingId, payload) => ({ ...saved, ...payload }));
    const apiClient = {
      transcriptComparison: vi.fn().mockResolvedValue({
        primary_version_id: "tv-main", candidate_version_id: "tv-whisper-new", candidate_kind: "whisper_reference",
        items: [{ primary_segment_id: "seg-main", candidate_segment_ids: ["ref"], start_ms: 0, end_ms: 5_000, primary_text: mainSegment.text, candidate_text: "候选", risk_kinds: ["text"], similarity: 0.5 }],
      }),
      asrGoldSamples: vi.fn().mockResolvedValue({ items: [saved] }),
      saveAsrGoldSample,
    } as unknown as ApiClient;
    render(<MeetingDetailPage apiClient={apiClient} initialSeekMs={0} isMobile={false} meeting={meeting()} onBack={vi.fn()} onReload={vi.fn()} projects={[]} tags={[]} />);
    await userEvent.click(screen.getByRole("button", { name: "Whisper 对照" }));
    await userEvent.click(await screen.findByRole("button", { name: "更新金标" }));
    const editor = screen.getByLabelText("seg-main 金标文本");
    await userEvent.clear(editor);
    await userEvent.type(editor, "新金标");
    await userEvent.click(screen.getByRole("button", { name: "确认保存金标" }));
    expect(saveAsrGoldSample).toHaveBeenCalledWith("vm-1", {
      segment_id: "seg-main", reference: "新金标", entities: ["ACME"], numbers: ["125"], tags: ["术语"],
    });
  });

  it("shows queued, ready and failed Qwen states honestly and wires request/retry", async () => {
    const requestQwenShadow = vi.fn().mockResolvedValue({ state: "queued" });
    const retryQwenShadow = vi.fn().mockResolvedValue({ state: "queued" });
    const onReload = vi.fn().mockResolvedValue(undefined);
    const baseClient = {
      transcriptVersionSegments: vi.fn(),
      asrGoldSamples: vi.fn().mockResolvedValue({ items: [] }),
      requestQwenShadow,
      retryQwenShadow,
    } as unknown as ApiClient;
    const { rerender } = render(
      <MeetingDetailPage apiClient={baseClient} initialSeekMs={0} isMobile={false} meeting={meeting(false)} onBack={vi.fn()} onReload={onReload} projects={[]} tags={[]} />,
    );
    await userEvent.click(screen.getByRole("button", { name: "生成 Qwen 影子稿" }));
    expect(requestQwenShadow).toHaveBeenCalledWith("vm-1");

    const queued = { ...meeting(false), asr_shadow_runs: [{ id: "q-queued", meeting_id: "vm-1", engine: "qwen", model: "0.6B", state: "queued" as const, created_at: "2026-07-14T00:00:00Z", updated_at: "2026-07-14T00:00:00Z" }] };
    rerender(<MeetingDetailPage apiClient={baseClient} initialSeekMs={0} isMobile={false} meeting={queued} onBack={vi.fn()} onReload={onReload} projects={[]} tags={[]} />);
    expect(screen.getByText("Qwen 已排队")).toBeInTheDocument();

    const failed = { ...meeting(false), asr_shadow_runs: [{ ...queued.asr_shadow_runs[0], id: "q-failed", state: "failed" as const, error: "模型运行失败" }] };
    rerender(<MeetingDetailPage apiClient={baseClient} initialSeekMs={0} isMobile={false} meeting={failed} onBack={vi.fn()} onReload={onReload} projects={[]} tags={[]} />);
    expect(screen.getByText("模型运行失败")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "重试 Qwen 影子稿" }));
    expect(retryQwenShadow).toHaveBeenCalledWith("vm-1", "q-failed");

    const ready = {
      ...meeting(false),
      transcript_versions: [...meeting(false).transcript_versions, { id: "tv-qwen", meeting_id: "vm-1", version_no: 5, kind: "qwen_reference", published: 0, created_at: "2026-07-14T00:00:00Z" }],
      asr_shadow_runs: [{ ...queued.asr_shadow_runs[0], id: "q-ready", state: "ready" as const, transcript_version_id: "tv-qwen" }],
    };
    rerender(<MeetingDetailPage apiClient={baseClient} initialSeekMs={0} isMobile={false} meeting={ready} onBack={vi.fn()} onReload={onReload} projects={[]} tags={[]} />);
    expect(screen.getByText("Qwen 已就绪")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Qwen 影子" })).toBeInTheDocument();
  });

  it("uses the returned Qwen state instead of claiming every request was queued", async () => {
    const requestQwenShadow = vi.fn().mockResolvedValue({
      id: "q-unavailable", meeting_id: "vm-1", engine: "qwen", model: "0.6B", state: "unavailable",
      error: "本机运行时不可用", created_at: "2026-07-14T00:00:00Z", updated_at: "2026-07-14T00:00:00Z",
    });
    render(<MeetingDetailPage apiClient={{ transcriptVersionSegments: vi.fn(), asrGoldSamples: vi.fn().mockResolvedValue({ items: [] }), requestQwenShadow } as unknown as ApiClient} initialSeekMs={0} isMobile={false} meeting={meeting(false)} onBack={vi.fn()} onReload={vi.fn()} projects={[]} tags={[]} />);
    await userEvent.click(screen.getByRole("button", { name: "生成 Qwen 影子稿" }));
    expect(await screen.findByText("Qwen 当前不可用")).toBeInTheDocument();
    expect(screen.queryByText("Qwen 影子稿已排队")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "重试 Qwen 影子稿" })).toBeInTheDocument();
  });

  it("polls only the Qwen snapshot and preserves local editor and gold state", async () => {
    vi.useFakeTimers();
    try {
      const queued = { ...meeting(false), asr_shadow_runs: [{ id: "q-poll", meeting_id: "vm-1", engine: "qwen", model: "0.6B", state: "queued" as const, created_at: "2026-07-14T00:00:00Z", updated_at: "2026-07-14T00:00:00Z" }] };
      const ready = { ...queued, transcript_versions: [...queued.transcript_versions, { id: "tv-qwen-poll", meeting_id: "vm-1", version_no: 5, kind: "qwen_reference", published: 0, created_at: "2026-07-14T00:01:00Z" }], asr_shadow_runs: [{ ...queued.asr_shadow_runs[0], state: "ready" as const, transcript_version_id: "tv-qwen-poll" }] };
      const meetingRead = vi.fn().mockResolvedValue(ready);
      const asrGoldSamples = vi.fn().mockResolvedValue({ items: [] });
      const onReload = vi.fn();
      render(<MeetingDetailPage apiClient={{ transcriptVersionSegments: vi.fn(), meeting: meetingRead, asrGoldSamples } as unknown as ApiClient} initialSeekMs={0} isMobile={false} meeting={queued} onBack={vi.fn()} onReload={onReload} projects={[]} tags={[]} />);
      fireEvent.click(screen.getByRole("tab", { name: /会议纪要/ }));
      fireEvent.click(screen.getByRole("button", { name: "编辑纪要" }));
      fireEvent.change(screen.getByRole("textbox", { name: "会议纪要编辑器" }), { target: { value: "本地未保存纪要" } });
      await act(async () => { await vi.advanceTimersByTimeAsync(5_000); });
      expect(meetingRead).toHaveBeenCalledWith("vm-1");
      expect(screen.getByDisplayValue("本地未保存纪要")).toBeInTheDocument();
      expect(asrGoldSamples).toHaveBeenCalledTimes(1);
      expect(onReload).not.toHaveBeenCalled();
    } finally {
      vi.useRealTimers();
    }
  });

  it("keeps the active Qwen candidate and gold editor stable while retaining only the newest out-of-order snapshot", async () => {
    const oldVersion = { id: "tv-qwen-old", meeting_id: "vm-1", version_no: 5, kind: "qwen_reference", published: 0, created_at: "2026-07-14T00:00:00Z" };
      const queued = {
        ...meeting(false),
        transcript_versions: [...meeting(false).transcript_versions, oldVersion],
        asr_shadow_runs: [
          { id: "q-new", meeting_id: "vm-1", engine: "qwen", model: "0.6B", state: "queued" as const, created_at: "2026-07-14T00:02:00Z", updated_at: "2026-07-14T00:02:00Z" },
          { id: "q-old", meeting_id: "vm-1", engine: "qwen", model: "0.6B", state: "ready" as const, transcript_version_id: oldVersion.id, created_at: "2026-07-14T00:00:00Z", updated_at: "2026-07-14T00:01:00Z" },
        ],
      };
      const makeReady = (suffix: string, versionNo: number) => ({
        ...queued,
        transcript_versions: [...queued.transcript_versions, { ...oldVersion, id: `tv-qwen-${suffix}`, version_no: versionNo, created_at: `2026-07-14T00:0${versionNo}:00Z` }],
        asr_shadow_runs: [{ ...queued.asr_shadow_runs[0], state: "ready" as const, transcript_version_id: `tv-qwen-${suffix}`, updated_at: `2026-07-14T00:0${versionNo}:00Z` }],
      });
      let resolveOlder!: (value: MeetingDetail) => void;
      let resolveNewest!: (value: MeetingDetail) => void;
      const older = new Promise<MeetingDetail>((resolve) => { resolveOlder = resolve; });
      const newest = new Promise<MeetingDetail>((resolve) => { resolveNewest = resolve; });
      const meetingRead = vi.fn().mockReturnValueOnce(older).mockReturnValueOnce(newest);
      const transcriptComparison = vi.fn().mockImplementation(async (_meetingId: string, candidateId: string) => ({
        primary_version_id: "tv-main",
        candidate_version_id: candidateId,
        candidate_kind: "qwen_reference",
        items: [{ primary_segment_id: "seg-main", candidate_segment_ids: [candidateId], start_ms: 0, end_ms: 5_000, primary_text: mainSegment.text, candidate_text: candidateId === oldVersion.id ? "旧候选文本" : candidateId === "tv-qwen-newest" ? "最新候选文本" : "迟到候选文本", risk_kinds: [], similarity: 1 }],
      }));
      const onDirtyChange = vi.fn();
      render(
        <MeetingDetailPage
          apiClient={{ meeting: meetingRead, transcriptComparison, asrGoldSamples: vi.fn().mockResolvedValue({ items: [] }), saveAsrGoldSample: vi.fn().mockRejectedValue(new Error("版本冲突")) } as unknown as ApiClient}
          initialSeekMs={0}
          isMobile={false}
          meeting={queued}
          onBack={vi.fn()}
          onDirtyChange={onDirtyChange}
          onReload={vi.fn()}
          projects={[]}
          tags={[]}
        />,
      );
      fireEvent.click(screen.getByRole("button", { name: "Qwen 影子" }));
      expect(await screen.findByText("旧候选文本")).toBeInTheDocument();
      fireEvent.click(screen.getByRole("button", { name: "标为金标" }));
      const editor = screen.getByLabelText("seg-main 金标文本") as HTMLTextAreaElement;
      fireEvent.change(editor, { target: { value: "正在人工修订的金标" } });
      editor.setSelectionRange(3, 3);
      expect(onDirtyChange).toHaveBeenLastCalledWith(true);

      await act(async () => {
        fireEvent(document, new Event("visibilitychange"));
        fireEvent(document, new Event("visibilitychange"));
        await Promise.resolve();
      });
      expect(meetingRead).toHaveBeenCalledTimes(2);
      await act(async () => { resolveNewest(makeReady("newest", 7)); await Promise.resolve(); });
      await act(async () => { resolveOlder(makeReady("late", 6)); await Promise.resolve(); });

      expect(screen.getByDisplayValue("正在人工修订的金标")).toBe(editor);
      expect(editor.selectionStart).toBe(3);
      expect(screen.getByText("旧候选文本")).toBeInTheDocument();
      expect(transcriptComparison).toHaveBeenCalledTimes(1);
      fireEvent.click(screen.getByRole("button", { name: "确认保存金标" }));
      expect(await screen.findByText("版本冲突")).toBeInTheDocument();
      expect(screen.getByText("旧候选文本")).toBeInTheDocument();
      expect(onDirtyChange).toHaveBeenLastCalledWith(true);

      await act(async () => {
        fireEvent.click(screen.getByRole("button", { name: "取消" }));
        await Promise.resolve();
      });
      expect(screen.getByText("最新候选文本")).toBeInTheDocument();
      expect(transcriptComparison).toHaveBeenLastCalledWith("vm-1", "tv-qwen-newest");
      expect(transcriptComparison).not.toHaveBeenCalledWith("vm-1", "tv-qwen-late");
      expect(onDirtyChange).toHaveBeenLastCalledWith(false);
  });

  it("applies a pending Qwen snapshot after a gold save succeeds", async () => {
    const oldVersion = { id: "tv-qwen-save-old", meeting_id: "vm-1", version_no: 5, kind: "qwen_reference", published: 0, created_at: "2026-07-14T00:00:00Z" };
    const queued = {
      ...meeting(false),
      transcript_versions: [...meeting(false).transcript_versions, oldVersion],
      asr_shadow_runs: [{ id: "q-save", meeting_id: "vm-1", engine: "qwen", model: "0.6B", state: "queued" as const, created_at: "2026-07-14T00:02:00Z", updated_at: "2026-07-14T00:02:00Z" }],
    };
    const newVersion = { ...oldVersion, id: "tv-qwen-save-new", version_no: 6 };
    const ready = { ...queued, transcript_versions: [...queued.transcript_versions, newVersion], asr_shadow_runs: [{ ...queued.asr_shadow_runs[0], state: "ready" as const, transcript_version_id: newVersion.id }] };
    const transcriptComparison = vi.fn().mockImplementation(async (_meetingId: string, candidateId: string) => ({
      primary_version_id: "tv-main",
      candidate_version_id: candidateId,
      candidate_kind: "qwen_reference",
      items: [{ primary_segment_id: "seg-main", candidate_segment_ids: [candidateId], start_ms: 0, end_ms: 5_000, primary_text: mainSegment.text, candidate_text: candidateId === oldVersion.id ? "保存前候选" : "保存后候选", risk_kinds: [], similarity: 1 }],
    }));
    const saveAsrGoldSample = vi.fn().mockResolvedValue({
      id: "gold-save", meeting_id: "vm-1", segment_id: "seg-main", start_ms: 0, end_ms: 5_000,
      reference: "已保存金标", entities: [], numbers: [], tags: [], created_at: "2026-07-14T00:00:00Z", updated_at: "2026-07-14T00:00:00Z",
    });
    render(<MeetingDetailPage apiClient={{ meeting: vi.fn().mockResolvedValue(ready), transcriptComparison, asrGoldSamples: vi.fn().mockResolvedValue({ items: [] }), saveAsrGoldSample } as unknown as ApiClient} initialSeekMs={0} isMobile={false} meeting={queued} onBack={vi.fn()} onReload={vi.fn()} projects={[]} tags={[]} />);
    await userEvent.click(screen.getByRole("button", { name: "Qwen 影子" }));
    expect(await screen.findByText("保存前候选")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "标为金标" }));
    fireEvent.change(screen.getByLabelText("seg-main 金标文本"), { target: { value: "已保存金标" } });
    await act(async () => {
      fireEvent(document, new Event("visibilitychange"));
      await Promise.resolve();
    });
    expect(screen.getByText("保存前候选")).toBeInTheDocument();
    expect(transcriptComparison).toHaveBeenCalledTimes(1);
    await userEvent.click(screen.getByRole("button", { name: "确认保存金标" }));
    expect(await screen.findByText("保存后候选")).toBeInTheDocument();
    expect(saveAsrGoldSample).toHaveBeenCalledTimes(1);
    expect(transcriptComparison).toHaveBeenLastCalledWith("vm-1", newVersion.id);
  });

  it("keeps a failed gold ledger closed and retries it explicitly", async () => {
    const asrGoldSamples = vi.fn().mockRejectedValueOnce(new Error("金标库暂不可用")).mockResolvedValueOnce({ items: [] });
    const saveAsrGoldSample = vi.fn();
    const apiClient = {
      transcriptComparison: vi.fn().mockResolvedValue({ primary_version_id: "tv-main", candidate_version_id: "tv-whisper-new", candidate_kind: "whisper_reference", items: [{ primary_segment_id: "seg-main", candidate_segment_ids: ["r"], start_ms: 0, end_ms: 5_000, primary_text: mainSegment.text, candidate_text: "候选", risk_kinds: [], similarity: 1 }] }),
      asrGoldSamples,
      saveAsrGoldSample,
    } as unknown as ApiClient;
    render(<MeetingDetailPage apiClient={apiClient} initialSeekMs={0} isMobile={false} meeting={meeting()} onBack={vi.fn()} onReload={vi.fn()} projects={[]} tags={[]} />);
    await userEvent.click(screen.getByRole("button", { name: "Whisper 对照" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("金标读取失败");
    expect(screen.getByRole("button", { name: "标为金标" })).toBeDisabled();
    await userEvent.click(screen.getByRole("button", { name: "重试读取金标" }));
    expect(await screen.findByRole("button", { name: "标为金标" })).toBeEnabled();
    expect(saveAsrGoldSample).not.toHaveBeenCalled();
  });

  it("does not expose or call Qwen or gold writes on mobile", () => {
    const apiClient = {
      transcriptVersionSegments: vi.fn(),
      requestQwenShadow: vi.fn(),
      retryQwenShadow: vi.fn(),
      saveAsrGoldSample: vi.fn(),
    } as unknown as ApiClient;
    render(<MeetingDetailPage apiClient={apiClient} initialSeekMs={0} isMobile meeting={meeting()} onBack={vi.fn()} onReload={vi.fn()} projects={[]} tags={[]} />);
    expect(screen.queryByRole("button", { name: "生成 Qwen 影子稿" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "标为金标" })).not.toBeInTheDocument();
    expect(apiClient.requestQwenShadow).not.toHaveBeenCalled();
    expect(apiClient.saveAsrGoldSample).not.toHaveBeenCalled();
  });

  it("passes normalized meeting hotwords to retranscription", async () => {
    const retranscribe = vi.fn().mockResolvedValue({ status: "queued" });
    render(<MeetingDetailPage apiClient={{ transcriptVersionSegments: vi.fn(), retranscribe, asrGoldSamples: vi.fn().mockResolvedValue({ items: [] }) } as unknown as ApiClient} initialSeekMs={0} isMobile={false} meeting={meeting(false)} onBack={vi.fn()} onReload={vi.fn().mockResolvedValue(undefined)} projects={[]} tags={[]} />);
    await userEvent.type(screen.getByLabelText("重新转写本场热词"), " ＡＣＭＥ, acme\n云图");
    await userEvent.click(screen.getByRole("button", { name: "重新转写" }));
    expect(retranscribe).toHaveBeenCalledWith("vm-1", ["ACME", "云图"]);
    expect(screen.getByLabelText("重新转写本场热词")).toHaveValue("");
  });

  it("renders minutes evidence and maps 404/409 to honest states", async () => {
    const evidence = {
      coverage: { total_items: 1, included_items: 1, omitted_items: 0 },
      topics: [{ topic_id: "T01", title: "议题", start_sec: 0, end_sec: 20, items: [{ item_id: "D01", kind: "decision" as const, text: "决定", status: "included" as const, source_start_sec: 12, source_end_sec: 14, minutes_anchor: "[00:00:12]" }] }],
      anchors: [{ item_id: "D01", minutes_anchor: "[00:00:12]", source_start_sec: 12, source_end_sec: 14 }],
    };
    const minutesEvidence = vi.fn().mockResolvedValueOnce(evidence).mockRejectedValueOnce(new ApiError("该会议暂无可验证证据", 404)).mockRejectedValueOnce(new ApiError("来源版本不一致", 409));
    const apiClient = { transcriptVersionSegments: vi.fn(), minutesEvidence, asrGoldSamples: vi.fn().mockResolvedValue({ items: [] }) } as unknown as ApiClient;
    const { unmount } = render(<MeetingDetailPage apiClient={apiClient} initialSeekMs={0} isMobile={false} meeting={meeting(false)} onBack={vi.fn()} onReload={vi.fn()} projects={[]} tags={[]} />);
    await userEvent.click(screen.getByRole("tab", { name: /会议纪要/ }));
    expect(await screen.findByText("1 条证据")).toBeInTheDocument();
    unmount();

    const second = render(<MeetingDetailPage apiClient={apiClient} initialSeekMs={0} isMobile={false} meeting={meeting(false)} onBack={vi.fn()} onReload={vi.fn()} projects={[]} tags={[]} />);
    await userEvent.click(screen.getByRole("tab", { name: /会议纪要/ }));
    expect(await screen.findByText("该会议暂无可验证证据")).toBeInTheDocument();
    second.unmount();

    render(<MeetingDetailPage apiClient={apiClient} initialSeekMs={0} isMobile={false} meeting={meeting(false)} onBack={vi.fn()} onReload={vi.fn()} projects={[]} tags={[]} />);
    await userEvent.click(screen.getByRole("tab", { name: /会议纪要/ }));
    expect(await screen.findByText("证据暂不可验证")).toBeInTheDocument();
    expect(screen.getByText("来源版本不一致")).toBeInTheDocument();
  });

  it("keys minutes evidence by the current minutes version and rejects a late old response", async () => {
    const evidenceFor = (text: string) => ({
      coverage: { total_items: 1, included_items: 1, omitted_items: 0 },
      topics: [{ topic_id: "T01", title: text, start_sec: 0, end_sec: 10, items: [{ item_id: text, kind: "decision" as const, text, status: "included" as const, source_start_sec: 1, source_end_sec: 2 }] }],
      anchors: [],
    });
    let resolveOld!: (value: ReturnType<typeof evidenceFor>) => void;
    let resolveNew!: (value: ReturnType<typeof evidenceFor>) => void;
    const oldRequest = new Promise<ReturnType<typeof evidenceFor>>((resolve) => { resolveOld = resolve; });
    const newRequest = new Promise<ReturnType<typeof evidenceFor>>((resolve) => { resolveNew = resolve; });
    const minutesEvidence = vi.fn().mockReturnValueOnce(oldRequest).mockReturnValueOnce(newRequest);
    const mv1 = { id: "mv-1", meeting_id: "vm-1", version_no: 1, kind: "generated", markdown: "正文 M1", published: 0, created_at: "2026-07-14T00:00:00Z" };
    const mv2 = { id: "mv-2", meeting_id: "vm-1", version_no: 2, kind: "generated", markdown: "正文 M2", published: 0, created_at: "2026-07-14T00:01:00Z" };
    const first = { ...meeting(false), current_minutes_version_id: mv1.id, minutes_versions: [mv1] };
    const second = { ...meeting(false), current_minutes_version_id: mv2.id, minutes_versions: [mv1, mv2] };
    const apiClient = { transcriptVersionSegments: vi.fn(), minutesEvidence, asrGoldSamples: vi.fn().mockResolvedValue({ items: [] }) } as unknown as ApiClient;
    const props = { apiClient, initialSeekMs: 0, isMobile: false, onBack: vi.fn(), onReload: vi.fn(), projects: [], tags: [] };
    const { rerender } = render(<MeetingDetailPage {...props} meeting={first} />);
    await userEvent.click(screen.getByRole("tab", { name: /会议纪要/ }));
    expect(screen.getByText("正文 M1")).toBeInTheDocument();
    rerender(<MeetingDetailPage {...props} meeting={second} />);
    expect(screen.getByText("正文 M2")).toBeInTheDocument();
    expect(screen.getByText("正在核验纪要证据…")).toBeInTheDocument();
    expect(screen.queryByText("证据 E1")).not.toBeInTheDocument();
    await act(async () => { resolveOld(evidenceFor("证据 E1")); await Promise.resolve(); });
    expect(screen.queryByText("证据 E1")).not.toBeInTheDocument();
    expect(screen.getByText("正在核验纪要证据…")).toBeInTheDocument();
    await act(async () => { resolveNew(evidenceFor("证据 E2")); await Promise.resolve(); });
    expect(screen.getAllByText("证据 E2").length).toBeGreaterThan(0);
    expect(screen.queryByText("证据 E1")).not.toBeInTheDocument();
    expect(minutesEvidence).toHaveBeenCalledTimes(2);
  });
  it("hides empty speaker naming controls when no speaker turns were imported", () => {
    render(
      <MeetingDetailPage
        apiClient={client(vi.fn())}
        initialSeekMs={0}
        isMobile={false}
        meeting={meeting()}
        onBack={vi.fn()}
        onReload={vi.fn().mockResolvedValue(undefined)}
        projects={[]}
        tags={[]}
      />,
    );

    expect(screen.queryByRole("heading", { name: "标记说话人姓名" })).not.toBeInTheDocument();
    expect(screen.queryByPlaceholderText("新的显示名称")).not.toBeInTheDocument();
  });

  it("explains that imported speaker turns are not cross-meeting voiceprints", () => {
    const withSpeaker = meeting();
    withSpeaker.speakers = [
      { id: "speaker-0", meeting_id: "vm-1", label: "SPEAKER_00", display_name: null },
    ];
    render(
      <MeetingDetailPage
        apiClient={client(vi.fn())}
        initialSeekMs={0}
        isMobile={false}
        meeting={withSpeaker}
        onBack={vi.fn()}
        onReload={vi.fn().mockResolvedValue(undefined)}
        projects={[]}
        tags={[]}
      />,
    );

    expect(screen.getByRole("heading", { name: "标记说话人姓名" })).toBeInTheDocument();
    expect(
      screen.getByText("这里只标记同一场录音内的说话人，不会跨会议识别具体身份。"),
    ).toBeInTheDocument();
  });

  it("loads the latest whisper_reference version and switches back to current FunASR segments", async () => {
    let resolveRequest!: (value: Awaited<ReturnType<ApiClient["transcriptVersionSegments"]>>) => void;
    const request = vi.fn(
      () =>
        new Promise<Awaited<ReturnType<ApiClient["transcriptVersionSegments"]>>>((resolve) => {
          resolveRequest = resolve;
        }),
    );
    render(
      <MeetingDetailPage
        apiClient={client(request)}
        initialSeekMs={0}
        isMobile={false}
        meeting={meeting()}
        onBack={vi.fn()}
        onReload={vi.fn().mockResolvedValue(undefined)}
        projects={[]}
        tags={[]}
      />,
    );

    await userEvent.click(screen.getByRole("button", { name: "Whisper 对照" }));
    expect(request).toHaveBeenCalledWith("vm-1", "tv-whisper-new");
    expect(screen.getByText("正在读取 Whisper 对照稿…")).toBeInTheDocument();

    await act(async () => {
      resolveRequest({
        version: meeting().transcript_versions[2],
        items: [{ ...mainSegment, id: "seg-whisper", text: "这是 Whisper 对照内容" }],
      });
    });
    expect(await screen.findByText("这是 Whisper 对照内容")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "FunASR 主稿" }));
    expect(screen.getByText("这是 FunASR 当前工作稿")).toBeInTheDocument();
  });

  it("shows an honest missing state when no whisper_reference version exists", async () => {
    const request = vi.fn();
    render(
      <MeetingDetailPage
        apiClient={client(request)}
        initialSeekMs={0}
        isMobile={false}
        meeting={meeting(false)}
        onBack={vi.fn()}
        onReload={vi.fn().mockResolvedValue(undefined)}
        projects={[]}
        tags={[]}
      />,
    );

    await userEvent.click(screen.getByRole("button", { name: "Whisper 对照" }));
    expect(screen.getByText("暂无 Whisper 对照稿")).toBeInTheDocument();
    expect(request).not.toHaveBeenCalled();
  });

  it("shows the backend error instead of falling back to the main transcript", async () => {
    const request = vi.fn().mockRejectedValue(new Error("对照稿版本读取失败"));
    render(
      <MeetingDetailPage
        apiClient={client(request)}
        initialSeekMs={0}
        isMobile={false}
        meeting={meeting()}
        onBack={vi.fn()}
        onReload={vi.fn().mockResolvedValue(undefined)}
        projects={[]}
        tags={[]}
      />,
    );

    await userEvent.click(screen.getByRole("button", { name: "Whisper 对照" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("对照稿版本读取失败");
    expect(screen.queryByText("这是 FunASR 当前工作稿")).not.toBeInTheDocument();
  });

  it("saves a desktop meeting project and multiple tags", async () => {
    const updateMeeting = vi.fn().mockResolvedValue(meeting());
    const apiClient = {
      transcriptVersionSegments: vi.fn(),
      updateMeeting,
    } as unknown as ApiClient;
    render(
      <MeetingDetailPage
        apiClient={apiClient}
        initialSeekMs={0}
        isMobile={false}
        meeting={{ ...meeting(false), project_id: "project-a", tags: [{ id: "tag-a", name: "既有", color: "#376f68" }] }}
        onBack={vi.fn()}
        onReload={vi.fn().mockResolvedValue(undefined)}
        projects={[
          { id: "project-a", name: "原项目", color: "#376f68" },
          { id: "project-b", name: "新项目", color: "#f0783b" },
        ]}
        tags={[
          { id: "tag-a", name: "既有", color: "#376f68" },
          { id: "tag-b", name: "待跟进", color: "#f0783b" },
        ]}
      />,
    );

    await userEvent.selectOptions(screen.getByLabelText("主项目"), "project-b");
    await userEvent.click(screen.getByRole("checkbox", { name: "待跟进" }));
    await userEvent.click(screen.getByRole("button", { name: "保存归档归属" }));

    expect(updateMeeting).toHaveBeenCalledWith("vm-1", {
      project_id: "project-b",
      tag_ids: ["tag-a", "tag-b"],
    });
  });

  it("tells how many tasks moved with the meeting", async () => {
    const updateMeeting = vi.fn().mockResolvedValue({
      ...meeting(false),
      effects: {
        tasks_moved: 3,
        tasks_left: [{ id: "t9", title: "改登录", requirement_id: "r1", requirement_title: "登录改版" }],
        undo_until: "2026-09-26T10:10:00Z",
      },
    });
    render(
      <MeetingDetailPage
        apiClient={{ transcriptVersionSegments: vi.fn(), updateMeeting } as unknown as ApiClient}
        initialSeekMs={0}
        isMobile={false}
        meeting={meeting(false)}
        onBack={vi.fn()}
        onReload={vi.fn().mockResolvedValue(undefined)}
        projects={[{ id: "project-a", name: "项目甲", color: "#376f68" }]}
        tags={[]}
      />,
    );

    await userEvent.selectOptions(screen.getByLabelText("主项目"), "project-a");
    await userEvent.click(screen.getByRole("button", { name: "保存归档归属" }));

    expect(
      await screen.findByText(
        "会议归档归属已保存；3 条待确认/过期的任务一起移过去；1 条任务挂在原项目的需求上，留在原处",
      ),
    ).toBeInTheDocument();
  });

  it("tells that the meeting card moved too and shows the card status under the title", async () => {
    const updateMeeting = vi.fn().mockResolvedValue({
      ...meeting(false),
      effects: {
        tasks_moved: 2,
        tasks_left: [],
        card: { action: "moved", from: "云图AI/声档会议记录/a.md", to: "项目甲/声档会议记录/a.md", reason: null },
        undo_until: "2026-09-26T10:10:00Z",
      },
    });
    render(
      <MeetingDetailPage
        apiClient={{ transcriptVersionSegments: vi.fn(), updateMeeting } as unknown as ApiClient}
        initialSeekMs={0}
        isMobile={false}
        meeting={{
          ...meeting(false),
          canonical_dir: "/archive/260926 对照测试会议",
          card: { state: "blocked", reason: "no_root", category: "waiting", path: null, synced_at: null, error: null },
        }}
        onBack={vi.fn()}
        onReload={vi.fn().mockResolvedValue(undefined)}
        projects={[{ id: "project-a", name: "项目甲", color: "#376f68" }]}
        tags={[]}
      />,
    );

    expect(screen.getByRole("button", { name: "复制归档文件夹路径" })).toBeInTheDocument();
    expect(screen.getByText("项目还没挂文件夹")).toBeInTheDocument();
    await userEvent.selectOptions(screen.getByLabelText("主项目"), "project-a");
    await userEvent.click(screen.getByRole("button", { name: "保存归档归属" }));

    expect(await screen.findByText("会议归档归属已保存；2 条待确认/过期的任务和会议卡片一起移过去")).toBeInTheDocument();
  });

  it("saving only tags leaves the project out of the request", async () => {
    const updateMeeting = vi.fn().mockResolvedValue(meeting());
    const apiClient = { transcriptVersionSegments: vi.fn(), updateMeeting } as unknown as ApiClient;
    render(
      <MeetingDetailPage
        apiClient={apiClient}
        initialSeekMs={0}
        isMobile={false}
        meeting={meeting(false)}
        onBack={vi.fn()}
        onReload={vi.fn().mockResolvedValue(undefined)}
        projects={[{ id: "project-a", name: "项目甲", color: "#376f68" }]}
        tags={[{ id: "tag-b", name: "待跟进", color: "#f0783b" }]}
      />,
    );

    await userEvent.click(screen.getByRole("checkbox", { name: "待跟进" }));
    await userEvent.click(screen.getByRole("button", { name: "保存归档归属" }));

    expect(updateMeeting).toHaveBeenCalledWith("vm-1", { tag_ids: ["tag-b"] });
  });

  it("without a project the requirement picker searches across projects and adopts the requirement's project", async () => {
    const requirements = vi.fn().mockResolvedValue({
      items: [
        { id: "req-1", title: "登录改版", priority: "P1", status: "active", project_id: "project-a" },
      ],
      total: 1,
    });
    const apiClient = { transcriptVersionSegments: vi.fn(), requirements } as unknown as ApiClient;
    render(
      <MeetingDetailPage
        apiClient={apiClient}
        initialSeekMs={0}
        isMobile={false}
        meeting={meeting(false)}
        onBack={vi.fn()}
        onReload={vi.fn().mockResolvedValue(undefined)}
        projects={[{ id: "project-a", name: "项目甲", color: "#376f68" }]}
        tags={[]}
      />,
    );

    await userEvent.click(screen.getByRole("button", { name: "＋ 关联需求" }));
    expect(requirements).toHaveBeenCalledWith({ project_id: undefined, status: "active", limit: 200 });
    expect(await screen.findByText("项目甲", { selector: "em" })).toBeInTheDocument();
    await userEvent.click(screen.getByRole("checkbox"));
    await userEvent.click(screen.getByRole("button", { name: "确定" }));

    expect(screen.getByLabelText("主项目")).toHaveValue("project-a");
  });

  it("links requirements through the picker and saves them together with the classification", async () => {
    const requirements = vi.fn().mockResolvedValue({
      items: [
        {
          id: "req-1",
          project_id: "project-a",
          project_name: "项目甲",
          project_color: "#376f68",
          title: "北辰仓快递配送",
          priority: "P0",
          status: "active",
          created_at: "2026-09-07T00:00:00Z",
          updated_at: "2026-09-07T00:00:00Z",
          open_task_count: 3,
          meeting_count: 2,
          latest_meeting_date: "2026-09-09T00:00:00Z",
          folder_count: 2,
        },
      ],
      total: 1,
      limit: 200,
      offset: 0,
      counts: { active: 1, done: 0, shelved: 0, all: 1 },
    });
    const updateMeeting = vi.fn().mockResolvedValue(meeting());
    const apiClient = {
      transcriptVersionSegments: vi.fn(),
      requirements,
      updateMeeting,
    } as unknown as ApiClient;
    render(
      <MeetingDetailPage
        apiClient={apiClient}
        initialSeekMs={0}
        isMobile={false}
        meeting={{ ...meeting(false), project_id: "project-a" }}
        onBack={vi.fn()}
        onReload={vi.fn().mockResolvedValue(undefined)}
        projects={[{ id: "project-a", name: "项目甲", color: "#376f68" }]}
        tags={[]}
      />,
    );

    await userEvent.click(screen.getByRole("button", { name: "＋ 关联需求" }));
    expect(requirements).toHaveBeenCalledWith({ project_id: "project-a", status: "active", limit: 200 });
    await userEvent.click(await screen.findByRole("checkbox", { name: /北辰仓快递配送/ }));
    await userEvent.click(screen.getByRole("button", { name: "确定" }));

    expect(screen.getByRole("button", { name: "保存归档归属" })).toBeEnabled();
    await userEvent.click(screen.getByRole("button", { name: "保存归档归属" }));

    expect(updateMeeting).toHaveBeenCalledWith("vm-1", { requirement_ids: ["req-1"] });
  });

  it("D24：保存归档归属时后端 404（关联的需求已不存在），就地显示原因、选择保留、按钮仍可再点", async () => {
    const requirements = vi.fn().mockResolvedValue({
      items: [
        {
          id: "req-2", project_id: "project-a", project_name: "项目甲", project_color: "#376f68",
          title: "库存盘点", priority: "P3", status: "active",
          created_at: "2026-09-07T00:00:00Z", updated_at: "2026-09-07T00:00:00Z",
          open_task_count: 1, meeting_count: 1, latest_meeting_date: "2026-09-08T00:00:00Z", folder_count: 1,
        },
      ],
      total: 1, limit: 200, offset: 0, counts: { active: 1, done: 0, shelved: 0, all: 1 },
    });
    const updateMeeting = vi.fn().mockRejectedValue(new ApiError("需求不存在", 404));
    const apiClient = { transcriptVersionSegments: vi.fn(), requirements, updateMeeting } as unknown as ApiClient;
    render(
      <MeetingDetailPage
        apiClient={apiClient}
        initialSeekMs={0}
        isMobile={false}
        meeting={{
          ...meeting(false),
          project_id: "project-a",
          requirements: [{ id: "req-1", title: "北辰仓快递配送", priority: "P0", status: "active", project_id: "project-a" }],
        }}
        onBack={vi.fn()}
        onReload={vi.fn().mockResolvedValue(undefined)}
        projects={[{ id: "project-a", name: "项目甲", color: "#376f68" }]}
        tags={[]}
      />,
    );

    // 加勾一个需求，让归档归属真正处于「未保存」态，才能验证保存失败后选择原样留着。
    await userEvent.click(screen.getByRole("button", { name: "编辑" }));
    await userEvent.click(await screen.findByRole("checkbox", { name: /库存盘点/ }));
    await userEvent.click(screen.getByRole("button", { name: "确定" }));

    await userEvent.click(screen.getByRole("button", { name: "保存归档归属" }));

    expect(await screen.findByText("需求不存在")).toBeInTheDocument();
    expect(screen.getByText("北辰仓快递配送")).toBeInTheDocument();
    expect(screen.getByText("库存盘点")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "保存归档归属" })).toBeEnabled();
  });

  it("opens the requirement detail page when a linked requirement chip is clicked", () => {
    const onOpenRequirement = vi.fn();
    const apiClient = { transcriptVersionSegments: vi.fn() } as unknown as ApiClient;
    render(
      <MeetingDetailPage
        apiClient={apiClient}
        initialSeekMs={0}
        isMobile={false}
        meeting={{
          ...meeting(false),
          project_id: "project-a",
          requirements: [{ id: "req-1", title: "北辰仓快递配送", priority: "P0", status: "active", project_id: "project-a" }],
        }}
        onBack={vi.fn()}
        onOpenRequirement={onOpenRequirement}
        onReload={vi.fn().mockResolvedValue(undefined)}
        projects={[{ id: "project-a", name: "项目甲", color: "#376f68" }]}
        tags={[]}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: /北辰仓快递配送/ }));
    expect(onOpenRequirement).toHaveBeenCalledWith("req-1");
  });

  it("marks an AI-matched project with an origin badge and hides it once the assignment is manual", () => {
    const apiClient = { transcriptVersionSegments: vi.fn() } as unknown as ApiClient;
    const projects = [{ id: "project-a", name: "原项目", color: "#376f68" }];
    const { rerender } = render(
      <MeetingDetailPage
        apiClient={apiClient}
        initialSeekMs={0}
        isMobile={false}
        meeting={{ ...meeting(false), project_id: "project-a", project_origin: "ai" }}
        onBack={vi.fn()}
        onReload={vi.fn().mockResolvedValue(undefined)}
        projects={projects}
        tags={[]}
      />,
    );

    expect(screen.getByText("AI 归属")).toBeInTheDocument();
    expect(screen.getByText("由会议纪要自动匹配；改选项目后以你选的为准")).toBeInTheDocument();

    rerender(
      <MeetingDetailPage
        apiClient={apiClient}
        initialSeekMs={0}
        isMobile={false}
        meeting={{ ...meeting(false), project_id: "project-a", project_origin: "manual" }}
        onBack={vi.fn()}
        onReload={vi.fn().mockResolvedValue(undefined)}
        projects={projects}
        tags={[]}
      />,
    );

    expect(screen.queryByText("AI 归属")).not.toBeInTheDocument();
    expect(screen.queryByText("由会议纪要自动匹配；改选项目后以你选的为准")).not.toBeInTheDocument();
  });

  it("offers retranscription and all three explicit conflict resolutions on desktop", async () => {
    const retranscribe = vi.fn().mockResolvedValue({ status: "queued" });
    const resolveConflict = vi.fn().mockResolvedValue({ conflict: 0 });
    const apiClient = {
      transcriptVersionSegments: vi.fn(),
      retranscribe,
      resolveConflict,
    } as unknown as ApiClient;
    render(
      <MeetingDetailPage
        apiClient={apiClient}
        initialSeekMs={0}
        isMobile={false}
        meeting={{
          ...meeting(false),
          conflict: 1,
          conflicts: [
            {
              id: "conflict-1",
              meeting_id: "vm-1",
              kind: "external_source_change",
              status: "open",
              payload: { changed_resources: ["transcript"] },
              created_at: "2026-07-10T00:00:00Z",
              updated_at: "2026-07-10T00:00:00Z",
            },
          ],
        }}
        onBack={vi.fn()}
        onReload={vi.fn().mockResolvedValue(undefined)}
        projects={[]}
        tags={[]}
      />,
    );

    expect(screen.getByRole("alert")).toHaveTextContent("外部文件版本冲突");
    await userEvent.click(screen.getByRole("button", { name: "重新转写" }));
    await userEvent.click(screen.getByRole("button", { name: "保留草稿并确认覆盖" }));

    expect(retranscribe).toHaveBeenCalledWith("vm-1");
    expect(resolveConflict).toHaveBeenCalledWith("vm-1", "conflict-1", "keep_draft");
    expect(screen.getByRole("button", { name: "采用外部版本" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "丢弃草稿" })).toBeInTheDocument();
  });

  it("shows conflict context without any write entry on a mobile or coarse-pointer layout", () => {
    render(
      <MeetingDetailPage
        apiClient={client(vi.fn())}
        initialSeekMs={0}
        isMobile
        meeting={{ ...meeting(false), conflict: 1 }}
        onBack={vi.fn()}
        onReload={vi.fn().mockResolvedValue(undefined)}
        projects={[]}
        tags={[]}
      />,
    );

    expect(screen.getByRole("alert")).toHaveTextContent("检测到外部文件变更");
    expect(screen.queryByRole("button", { name: "重新转写" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "保留草稿并确认覆盖" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "采用外部版本" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "丢弃草稿" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "编辑逐字稿" })).not.toBeInTheDocument();
  });

  it("splits and merges locally, then saves all segments once with the opened base version", async () => {
    const saveTranscript = vi.fn().mockResolvedValue({ version_id: "tv-draft" });
    const splitSegment = vi.fn();
    const mergeSegments = vi.fn();
    const apiClient = {
      transcriptVersionSegments: vi.fn(),
      saveTranscript,
      splitSegment,
      mergeSegments,
    } as unknown as ApiClient;
    render(
      <MeetingDetailPage
        apiClient={apiClient}
        initialSeekMs={0}
        isMobile={false}
        meeting={meeting(false)}
        onBack={vi.fn()}
        onReload={vi.fn().mockResolvedValue(undefined)}
        projects={[]}
        tags={[]}
      />,
    );

    await userEvent.click(screen.getByRole("button", { name: "编辑逐字稿" }));
    const textarea = screen.getByLabelText("00:00 逐字稿") as HTMLTextAreaElement;
    textarea.setSelectionRange(2, 2);
    fireEvent.click(textarea);
    await userEvent.click(screen.getByRole("button", { name: "从光标拆分" }));

    expect(screen.getAllByRole("textbox", { name: /逐字稿/ })).toHaveLength(2);
    expect(splitSegment).not.toHaveBeenCalled();

    await userEvent.click(screen.getByRole("button", { name: "与上一段合并" }));
    expect(screen.getAllByRole("textbox", { name: /逐字稿/ })).toHaveLength(1);
    expect(mergeSegments).not.toHaveBeenCalled();

    await userEvent.click(screen.getByRole("button", { name: "保存草稿" }));
    expect(saveTranscript).toHaveBeenCalledWith(
      "vm-1",
      expect.arrayContaining([expect.objectContaining({ ordinal: 0, text: mainSegment.text })]),
      "tv-main",
    );
  });

  it("keeps local transcript edits after a 409 version conflict", async () => {
    const onReload = vi.fn().mockResolvedValue(undefined);
    const saveTranscript = vi.fn().mockRejectedValue(new ApiError("工作版本已经更新，请核对后重试", 409));
    const apiClient = {
      transcriptVersionSegments: vi.fn(),
      saveTranscript,
    } as unknown as ApiClient;
    render(
      <MeetingDetailPage
        apiClient={apiClient}
        initialSeekMs={0}
        isMobile={false}
        meeting={meeting(false)}
        onBack={vi.fn()}
        onReload={onReload}
        projects={[]}
        tags={[]}
      />,
    );

    await userEvent.click(screen.getByRole("button", { name: "编辑逐字稿" }));
    const textarea = screen.getByLabelText("00:00 逐字稿");
    await userEvent.clear(textarea);
    await userEvent.type(textarea, "本地尚未保存的修订");
    await userEvent.click(screen.getByRole("button", { name: "保存草稿" }));

    expect(screen.getByDisplayValue("本地尚未保存的修订")).toBeInTheDocument();
    expect(screen.getByText(/工作版本已经更新/)).toBeInTheDocument();
    expect(saveTranscript).toHaveBeenCalledWith("vm-1", expect.any(Array), "tv-main");
    expect(onReload).not.toHaveBeenCalled();
  });

  it("blocks destructive actions and warns before leaving while any local edit is dirty", async () => {
    const onBack = vi.fn();
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
    const apiClient = {
      transcriptVersionSegments: vi.fn(),
      retranscribe: vi.fn(),
      regenerateMinutes: vi.fn(),
      publish: vi.fn(),
      resolveConflict: vi.fn(),
    } as unknown as ApiClient;
    render(
      <MeetingDetailPage
        apiClient={apiClient}
        initialSeekMs={0}
        isMobile={false}
        meeting={{
          ...meeting(false),
          conflict: 1,
          conflicts: [
            {
              id: "conflict-external",
              meeting_id: "vm-1",
              kind: "external_source_change",
              status: "open",
              payload: { changed_resources: ["transcript"] },
              created_at: "2026-07-10T00:00:00Z",
              updated_at: "2026-07-10T00:00:00Z",
            },
          ],
        }}
        onBack={onBack}
        onReload={vi.fn().mockResolvedValue(undefined)}
        projects={[]}
        tags={[]}
      />,
    );

    await userEvent.click(screen.getByRole("button", { name: "编辑逐字稿" }));
    await userEvent.type(screen.getByLabelText("00:00 逐字稿"), "补充");

    expect(screen.getByRole("button", { name: "重新转写" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "采用外部版本" })).toBeDisabled();
    await userEvent.click(screen.getByRole("tab", { name: /会议纪要/ }));
    expect(screen.getByRole("button", { name: "重新生成纪要" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "写回会议文件夹" })).toBeDisabled();

    await userEvent.click(screen.getByRole("button", { name: "← 返回录音档案" }));
    expect(confirm).toHaveBeenCalled();
    expect(onBack).not.toHaveBeenCalled();

    const unload = new Event("beforeunload", { cancelable: true });
    window.dispatchEvent(unload);
    expect(unload.defaultPrevented).toBe(true);
    confirm.mockRestore();
  });

  it("treats an unsaved project or tag change as dirty without blocking its own save", async () => {
    const onBack = vi.fn();
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
    render(
      <MeetingDetailPage
        apiClient={{ transcriptVersionSegments: vi.fn() } as unknown as ApiClient}
        initialSeekMs={0}
        isMobile={false}
        meeting={meeting(false)}
        onBack={onBack}
        onReload={vi.fn().mockResolvedValue(undefined)}
        projects={[{ id: "project-new", name: "新项目", color: "#376f68" }]}
        tags={[]}
      />,
    );

    await userEvent.selectOptions(screen.getByLabelText("主项目"), "project-new");
    expect(screen.getByRole("button", { name: "保存归档归属" })).toBeEnabled();
    await userEvent.click(screen.getByRole("tab", { name: /会议纪要/ }));
    expect(screen.getByRole("button", { name: "写回会议文件夹" })).toBeDisabled();
    await userEvent.click(screen.getByRole("button", { name: "← 返回录音档案" }));
    expect(confirm).toHaveBeenCalled();
    expect(onBack).not.toHaveBeenCalled();
    confirm.mockRestore();
  });

  it("creates the first minutes draft from an empty desktop editor", async () => {
    const saveMinutes = vi.fn().mockResolvedValue({ version_id: "mv-first" });
    const apiClient = {
      transcriptVersionSegments: vi.fn(),
      saveMinutes,
    } as unknown as ApiClient;
    render(
      <MeetingDetailPage
        apiClient={apiClient}
        initialSeekMs={0}
        isMobile={false}
        meeting={meeting(false)}
        onBack={vi.fn()}
        onReload={vi.fn().mockResolvedValue(undefined)}
        projects={[]}
        tags={[]}
      />,
    );

    await userEvent.click(screen.getByRole("tab", { name: /会议纪要/ }));
    await userEvent.click(screen.getByRole("button", { name: "编辑纪要" }));
    const editor = screen.getByRole("textbox", { name: "会议纪要编辑器" });
    await userEvent.type(editor, "# 第一版纪要");
    await userEvent.click(screen.getByRole("button", { name: "保存纪要草稿" }));

    expect(saveMinutes).toHaveBeenCalledWith("vm-1", "# 第一版纪要", null);
  });

  it("保存纪要后在编辑器下方就地确认错字更正，已自动记入的给撤销", async () => {
    const saveMinutes = vi.fn().mockResolvedValue({
      version_id: "mv-first",
      corrections: [
        {
          id: "gs-1",
          wrong: "树立协会",
          correct: "数理协会",
          scope: "通用",
          meeting_id: "vm-1",
          context: null,
          status: "pending",
          created_at: "",
          updated_at: "",
          target_project_id: null,
          target_project_name: null,
          auto_recorded: false,
        },
        {
          id: "gs-2",
          wrong: "随方",
          correct: "随访",
          scope: "通用",
          meeting_id: "vm-1",
          context: null,
          status: "confirmed",
          created_at: "",
          updated_at: "",
          existing_term_id: "gt-1",
          existing_term_project_name: null,
          auto_recorded: true,
        },
      ],
    });
    const onGlossaryChanged = vi.fn();
    const confirmGlossarySuggestion = vi.fn().mockResolvedValue({
      ok: true,
      created: true,
      wrong: "树立协会",
      correct: "数理协会",
      term: { project_name: null },
      suggestion: null,
    });
    const apiClient = {
      transcriptVersionSegments: vi.fn(),
      saveMinutes,
      confirmGlossarySuggestion,
    } as unknown as ApiClient;
    render(
      <MeetingDetailPage
        apiClient={apiClient}
        initialSeekMs={0}
        isMobile={false}
        meeting={meeting(false)}
        onBack={vi.fn()}
        onGlossaryChanged={onGlossaryChanged}
        onReload={vi.fn().mockResolvedValue(undefined)}
        projects={[]}
        tags={[]}
      />,
    );

    await userEvent.click(screen.getByRole("tab", { name: /会议纪要/ }));
    await userEvent.click(screen.getByRole("button", { name: "编辑纪要" }));
    await userEvent.type(screen.getByRole("textbox", { name: "会议纪要编辑器" }), "成立数理协会");
    await userEvent.click(screen.getByRole("button", { name: "保存纪要草稿" }));

    expect(await screen.findByText(/1 处像是错字更正。这场会还没定项目/)).toBeInTheDocument();
    expect(screen.getByText("已自动记入『随访』（公共）")).toBeInTheDocument();
    expect(onGlossaryChanged).toHaveBeenCalled();
    await userEvent.click(screen.getByRole("button", { name: "记入 公共" }));
    expect(confirmGlossarySuggestion).toHaveBeenCalledWith("gs-1", { target: "public", short: false });
    expect(await screen.findByText("已记入 公共")).toBeInTheDocument();
  });

  it("renders safe GFM markdown without enabling raw HTML or dangerous links", async () => {
    const unsafe = {
      ...meeting(false),
      current_minutes_version_id: "mv-1",
      minutes_versions: [
        {
          id: "mv-1",
          meeting_id: "vm-1",
          version_no: 1,
          kind: "draft",
          published: 0,
          markdown:
            "# 决策结论\n\n|事项|负责人|\n|---|---|\n|发布|甲|\n\n[可信链接](https://example.com) [危险链接](javascript:alert(1)) ![远程跟踪图](https://attacker.example/pixel) <script>alert(1)</script>",
          created_at: "2026-07-10T00:00:00Z",
        },
      ],
    } satisfies MeetingDetail;
    render(
      <MeetingDetailPage
        apiClient={client(vi.fn())}
        initialSeekMs={0}
        isMobile={false}
        meeting={unsafe}
        onBack={vi.fn()}
        onReload={vi.fn().mockResolvedValue(undefined)}
        projects={[]}
        tags={[]}
      />,
    );

    await userEvent.click(screen.getByRole("tab", { name: /会议纪要/ }));
    expect(screen.getByRole("heading", { name: "决策结论" })).toBeInTheDocument();
    expect(screen.getByRole("table")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "可信链接" })).toHaveAttribute("href", "https://example.com");
    expect(screen.queryByRole("link", { name: "危险链接" })).not.toBeInTheDocument();
    expect(screen.queryByRole("img", { name: "远程跟踪图" })).not.toBeInTheDocument();
    expect(screen.getByText("远程跟踪图")).toBeInTheDocument();
    expect(document.querySelector("script")).toBeNull();
  });

  it("shows typed integrity conflicts as read-only and external conflicts as actionable", () => {
    render(
      <MeetingDetailPage
        apiClient={client(vi.fn())}
        initialSeekMs={0}
        isMobile={false}
        meeting={{
          ...meeting(false),
          conflict: 1,
          conflicts: [
            {
              id: "conflict-audio",
              meeting_id: "vm-1",
              kind: "audio_integrity",
              status: "open",
              payload: { reason: "canonical_missing" },
              created_at: "2026-07-10T00:00:00Z",
              updated_at: "2026-07-10T00:00:00Z",
            },
            {
              id: "conflict-external",
              meeting_id: "vm-1",
              kind: "external_source_change",
              status: "open",
              payload: { changed_resources: ["minutes"] },
              created_at: "2026-07-10T00:00:00Z",
              updated_at: "2026-07-10T00:00:00Z",
            },
          ],
        }}
        onBack={vi.fn()}
        onReload={vi.fn().mockResolvedValue(undefined)}
        projects={[]}
        tags={[]}
      />,
    );

    expect(screen.getByText("原音频完整性异常")).toBeInTheDocument();
    expect(screen.getByText("外部文件版本冲突")).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: "采用外部版本" })).toHaveLength(1);
  });

  it("locks transcript editing while saving and preserves a post-request local revision", async () => {
    let resolveFirst!: (value: { version_id: string }) => void;
    const saveTranscript = vi
      .fn()
      .mockImplementationOnce(
        () => new Promise<{ version_id: string }>((resolve) => { resolveFirst = resolve; }),
      )
      .mockResolvedValueOnce({ version_id: "tv-draft-2" });
    const onReload = vi.fn().mockResolvedValue(undefined);
    const onNavigationLockChange = vi.fn();
    render(
      <MeetingDetailPage
        apiClient={{ transcriptVersionSegments: vi.fn(), saveTranscript } as unknown as ApiClient}
        initialSeekMs={0}
        isMobile={false}
        meeting={meeting(false)}
        onBack={vi.fn()}
        onNavigationLockChange={onNavigationLockChange}
        onReload={onReload}
        projects={[]}
        tags={[]}
      />,
    );

    await userEvent.click(screen.getByRole("button", { name: "编辑逐字稿" }));
    const textarea = screen.getByLabelText("00:00 逐字稿") as HTMLTextAreaElement;
    await userEvent.clear(textarea);
    await userEvent.type(textarea, "请求快照文本");
    await userEvent.click(screen.getByRole("button", { name: "保存草稿" }));

    expect(textarea).toBeDisabled();
    expect(screen.getByRole("button", { name: "从光标拆分" })).toBeDisabled();
    expect(screen.getByRole("tab", { name: /会议纪要/ })).toBeDisabled();
    expect(screen.getByRole("button", { name: "← 返回录音档案" })).toBeDisabled();
    expect(onNavigationLockChange).toHaveBeenLastCalledWith(true);

    textarea.removeAttribute("disabled");
    fireEvent.change(textarea, { target: { value: "请求后继续输入" } });
    await act(async () => resolveFirst({ version_id: "tv-draft-1" }));

    expect(screen.getByDisplayValue("请求后继续输入")).toBeInTheDocument();
    expect(onReload).not.toHaveBeenCalled();
    expect(onNavigationLockChange).toHaveBeenLastCalledWith(false);

    await userEvent.click(screen.getByRole("button", { name: "保存草稿" }));
    expect(saveTranscript).toHaveBeenLastCalledWith(
      "vm-1",
      expect.any(Array),
      "tv-draft-1",
    );
  });

  it("locks minutes editing while saving and preserves a post-request local revision", async () => {
    let resolveFirst!: (value: { version_id: string }) => void;
    const saveMinutes = vi
      .fn()
      .mockImplementationOnce(
        () => new Promise<{ version_id: string }>((resolve) => { resolveFirst = resolve; }),
      )
      .mockResolvedValueOnce({ version_id: "mv-draft-2" });
    const onReload = vi.fn().mockResolvedValue(undefined);
    render(
      <MeetingDetailPage
        apiClient={{ transcriptVersionSegments: vi.fn(), saveMinutes } as unknown as ApiClient}
        initialSeekMs={0}
        isMobile={false}
        meeting={meeting(false)}
        onBack={vi.fn()}
        onReload={onReload}
        projects={[]}
        tags={[]}
      />,
    );

    await userEvent.click(screen.getByRole("tab", { name: /会议纪要/ }));
    await userEvent.click(screen.getByRole("button", { name: "编辑纪要" }));
    const textarea = screen.getByRole("textbox", { name: "会议纪要编辑器" });
    await userEvent.type(textarea, "请求快照纪要");
    await userEvent.click(screen.getByRole("button", { name: "保存纪要草稿" }));

    expect(textarea).toBeDisabled();
    expect(screen.getByRole("tab", { name: /逐字稿/ })).toBeDisabled();
    expect(screen.getByRole("button", { name: "← 返回录音档案" })).toBeDisabled();

    textarea.removeAttribute("disabled");
    fireEvent.change(textarea, { target: { value: "请求后继续写纪要" } });
    await act(async () => resolveFirst({ version_id: "mv-draft-1" }));

    expect(screen.getByDisplayValue("请求后继续写纪要")).toBeInTheDocument();
    expect(onReload).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole("button", { name: "保存纪要草稿" }));
    expect(saveMinutes).toHaveBeenLastCalledWith("vm-1", "请求后继续写纪要", "mv-draft-1");
  });

  it("locks classification while saving and preserves a post-request local revision", async () => {
    let resolveFirst!: (value: MeetingDetail) => void;
    const updateMeeting = vi
      .fn()
      .mockImplementationOnce(
        () => new Promise<MeetingDetail>((resolve) => { resolveFirst = resolve; }),
      )
      .mockResolvedValueOnce(meeting(false));
    const onReload = vi.fn().mockResolvedValue(undefined);
    const onClassificationSaved = vi.fn().mockResolvedValue(undefined);
    render(
      <MeetingDetailPage
        apiClient={{ transcriptVersionSegments: vi.fn(), updateMeeting } as unknown as ApiClient}
        initialSeekMs={0}
        isMobile={false}
        meeting={meeting(false)}
        onBack={vi.fn()}
        onClassificationSaved={onClassificationSaved}
        onReload={onReload}
        projects={[
          { id: "project-a", name: "项目甲", color: "#376f68" },
          { id: "project-b", name: "项目乙", color: "#f0783b" },
        ]}
        tags={[]}
      />,
    );

    const project = screen.getByLabelText("主项目");
    await userEvent.selectOptions(project, "project-a");
    await userEvent.click(screen.getByRole("button", { name: "保存归档归属" }));
    expect(project).toBeDisabled();
    expect(screen.getByRole("tab", { name: /会议纪要/ })).toBeDisabled();

    project.removeAttribute("disabled");
    fireEvent.change(project, { target: { value: "project-b" } });
    await act(async () => resolveFirst(meeting(false)));

    expect(project).toHaveValue("project-b");
    expect(onReload).not.toHaveBeenCalled();
    expect(onClassificationSaved).toHaveBeenCalledTimes(1);
    await userEvent.click(screen.getByRole("button", { name: "保存归档归属" }));
    expect(updateMeeting).toHaveBeenLastCalledWith("vm-1", { project_id: "project-b" });
  });

  it("regenerates minutes on the default backend, or pins Claude on demand", async () => {
    const regenerateMinutes = vi.fn().mockResolvedValue({ status: "queued" });
    const apiClient = {
      transcriptVersionSegments: vi.fn(),
      regenerateMinutes,
    } as unknown as ApiClient;
    render(
      <MeetingDetailPage
        apiClient={apiClient}
        initialSeekMs={0}
        isMobile={false}
        meeting={meeting(false)}
        onBack={vi.fn()}
        onReload={vi.fn().mockResolvedValue(undefined)}
        projects={[]}
        tags={[]}
      />,
    );

    await userEvent.click(screen.getByRole("tab", { name: /会议纪要/ }));

    // 默认入口不带后端，跟 relay 的全局默认走
    await userEvent.click(screen.getByRole("button", { name: "重新生成纪要" }));
    expect(regenerateMinutes).toHaveBeenLastCalledWith("vm-1");

    // Claude 入口必须显式钉住后端，否则又落回 DeepSeek
    await userEvent.click(screen.getByRole("button", { name: /用 Claude 重写/ }));
    expect(regenerateMinutes).toHaveBeenLastCalledWith("vm-1", "claude");
  });
});

describe("MeetingDetailPage 归属条", () => {
  const projects = [
    { id: "project-a", name: "云图AI", color: "#376f68" },
    { id: "project-b", name: "数据中台", color: "#f0783b" },
  ];
  const baseAttribution: Omit<MeetingAttribution, "state"> = {
    project_id: null,
    origin: null,
    method: null,
    evidence: [],
    candidates: [],
    reason: "",
    new_project_name: null,
    reassigned_from: null,
    ai_configured: true,
  };

  it("待你选时点候选就地改归属，同步右侧下拉，不重载详情也不丢没保存的纪要", async () => {
    const reviewing: MeetingDetail = {
      ...meeting(false),
      attribution: {
        ...baseAttribution,
        state: "needs_review",
        candidates: [
          { project_id: "project-a", project_name: "云图AI", count: 2, llm: true, current: false },
          { project_id: "project-b", project_name: "数据中台", count: 1, llm: false, current: false },
        ],
      },
    };
    const updateMeeting = vi.fn().mockResolvedValue({
      ...reviewing,
      project_id: "project-b",
      project_name: "数据中台",
      project_color: "#f0783b",
      project_origin: "manual",
      effects: { tasks_moved: 2, tasks_left: [], undo_until: "2099-01-01T00:00:00Z" },
      attribution: { ...baseAttribution, state: "manual", project_id: "project-b", origin: "manual" },
    });
    const undoMeetingProject = vi.fn().mockResolvedValue({
      ...reviewing,
      project_id: null,
      project_name: null,
    });
    const onReload = vi.fn().mockResolvedValue(undefined);
    render(
      <MeetingDetailPage
        apiClient={{ transcriptVersionSegments: vi.fn(), updateMeeting, undoMeetingProject } as unknown as ApiClient}
        initialSeekMs={0}
        isMobile={false}
        meeting={reviewing}
        onBack={vi.fn()}
        onReload={onReload}
        projects={projects}
        tags={[]}
      />,
    );

    expect(screen.getByRole("option", { name: "待你选" })).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "数据中台" }));

    expect(updateMeeting).toHaveBeenCalledWith("vm-1", { project_id: "project-b" });
    expect(onReload).not.toHaveBeenCalled();
    expect(screen.getByLabelText("主项目")).toHaveValue("project-b");
    expect(screen.queryByRole("button", { name: "保存归档归属" })).toBeDisabled();
    expect(await screen.findByText(/已改到 数据中台：2 条待确认\/过期的任务一起移过去/)).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "撤销" }));
    expect(undoMeetingProject).toHaveBeenCalledWith("vm-1");
    expect(await screen.findByText("已撤销刚才的改动")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "数据中台" })).toBeInTheDocument();
  });

  it("像新项目的会：下拉写像新项目「X」；建成需求后提示带［打开需求］［撤销］，右侧关联需求跟着更新", async () => {
    const hint = { kind: "project" as const, name: "云图看板", spoken: [] };
    const nameCandidates = vi.fn().mockResolvedValue({
      hint,
      candidates: [{ name: "云图看板", folder_path: null, spoken: null, ai: true, similar_folder_path: null }],
      meetings: [{ id: "vm-1", title: "对照测试会议", date: "2026-07-10", said_ms: null }],
      default_action: "create_project",
      project: null,
      requirement_projects: [{ id: "project-a", name: "云图AI", color: "#376f68", suggested: true }],
      folder: { mode: "none", reason: "还没有可参照的项目文件夹，这次先不建文件夹" },
      folders_state: "ready",
      create_parent: null,
      create_parent_source: null,
      create_parent_state: null,
    });
    const nameAsRequirement = vi.fn().mockResolvedValue({
      requirement_id: "req-9",
      requirement_title: "云图看板",
      project_id: "project-a",
      project_name: "云图AI",
      existing: false,
      priority: "P2",
      meetings_linked: 1,
      meetings_assigned: 1,
      meeting_ids: ["vm-1"],
      folder_attached: null,
      folder_error: null,
      event_id: 3,
      undo_until: "2099-01-01T00:00:00Z",
    });
    const reloaded = vi.fn().mockResolvedValue({
      ...meeting(false),
      project_id: "project-a",
      project_name: "云图AI",
      project_color: "#376f68",
      project_origin: "manual",
      attribution: { ...baseAttribution, state: "manual", project_id: "project-a", origin: "manual", name_hint: null },
      requirements: [{ id: "req-9", title: "云图看板", priority: "P2", status: "active", project_id: "project-a" }],
    });
    const onOpenRequirement = vi.fn();
    const { container } = render(
      <MeetingDetailPage
        apiClient={
          { transcriptVersionSegments: vi.fn(), nameCandidates, nameAsRequirement, meeting: reloaded } as unknown as ApiClient
        }
        initialSeekMs={0}
        isMobile={false}
        meeting={{
          ...meeting(false),
          attribution: { ...baseAttribution, state: "new_project", new_project_name: "云图看板", name_hint: hint },
        }}
        onBack={vi.fn()}
        onOpenRequirement={onOpenRequirement}
        onReload={vi.fn().mockResolvedValue(undefined)}
        projects={projects}
        tags={[]}
      />,
    );

    expect(screen.getByRole("option", { name: "像新项目「云图看板」" })).toBeInTheDocument();
    await screen.findByDisplayValue("云图看板");
    await userEvent.click(screen.getByRole("button", { name: "建成需求" }));

    expect(nameAsRequirement).toHaveBeenCalledWith("vm-1", { title: "云图看板", project_id: "project-a" });
    const notice = (await screen.findByText("已在『云图AI』建好需求『云图看板』（P2），1 场会已关联")).closest(
      ".action-banner",
    ) as HTMLElement;
    expect(within(notice).getAllByRole("button", { name: "撤销" })).toHaveLength(1);
    expect(screen.getByLabelText("主项目")).toHaveValue("project-a");
    expect(container.querySelector(".requirement-chip")).toHaveTextContent("云图看板");
    expect(screen.getByRole("button", { name: "保存归档归属" })).toBeDisabled();

    await userEvent.click(within(notice).getByRole("button", { name: "打开需求" }));
    expect(onOpenRequirement).toHaveBeenCalledWith("req-9");
  });

  it("右侧下拉改了项目还没保存时，归属条按钮置灰", async () => {
    render(
      <MeetingDetailPage
        apiClient={{ transcriptVersionSegments: vi.fn() } as unknown as ApiClient}
        initialSeekMs={0}
        isMobile={false}
        meeting={{ ...meeting(false), attribution: { ...baseAttribution, state: "ai_pending" } }}
        onBack={vi.fn()}
        onReload={vi.fn()}
        projects={projects}
        tags={[]}
      />,
    );

    expect(screen.getByText("正在判断属于哪个项目（通常一分钟内）")).toBeInTheDocument();
    expect(screen.getByRole("option", { name: "等 AI 判断" })).toBeInTheDocument();
    await userEvent.selectOptions(screen.getByLabelText("主项目"), "project-a");

    expect(screen.getByText("右侧有未保存的归属修改")).toBeInTheDocument();
    expect(screen.getByRole("combobox", { name: "选项目" })).toBeDisabled();
  });

  it("没项目的会可以显式标不归项目；标过的可以交给 AI", async () => {
    const updateMeeting = vi.fn().mockResolvedValue(meeting(false));
    const { unmount } = render(
      <MeetingDetailPage
        apiClient={{ transcriptVersionSegments: vi.fn(), updateMeeting } as unknown as ApiClient}
        initialSeekMs={0}
        isMobile={false}
        meeting={{ ...meeting(false), attribution: { ...baseAttribution, state: "none" } }}
        onBack={vi.fn()}
        onReload={vi.fn().mockResolvedValue(undefined)}
        projects={projects}
        tags={[]}
      />,
    );

    expect(screen.getByRole("option", { name: "AI 没认出" })).toBeInTheDocument();
    await userEvent.selectOptions(screen.getByLabelText("主项目"), "不归项目");
    await userEvent.click(screen.getByRole("button", { name: "保存归档归属" }));
    expect(updateMeeting).toHaveBeenCalledWith("vm-1", { project_id: "" });
    unmount();

    render(
      <MeetingDetailPage
        apiClient={{ transcriptVersionSegments: vi.fn(), updateMeeting } as unknown as ApiClient}
        initialSeekMs={0}
        isMobile={false}
        meeting={{
          ...meeting(false),
          project_origin: "manual",
          attribution: { ...baseAttribution, state: "manual_none", origin: "manual" },
        }}
        onBack={vi.fn()}
        onReload={vi.fn().mockResolvedValue(undefined)}
        projects={projects}
        tags={[]}
      />,
    );
    expect(screen.getByRole("option", { name: "不归项目（你标的）" })).toBeInTheDocument();
    await userEvent.selectOptions(screen.getByLabelText("主项目"), "交给 AI 判断");
    await userEvent.click(screen.getByRole("button", { name: "保存归档归属" }));
    expect(updateMeeting).toHaveBeenLastCalledWith("vm-1", { project_id: "__ai__" });
  });
});


describe("MeetingDetailPage 相关材料栏（4d）", () => {
  it("新后台时右栏最上面一节是相关材料，点条目打开预览并定位；旧后台（没有 linksFlags）不画", async () => {
    const { LinksFlagsContext } = await import("./links/LinksFlagsContext");
    const relatedMaterials = vi.fn().mockResolvedValue({
      state: { kind: "ok", text: null, action: null },
      files: {
        "q2:3f6c0000000000000000": {
          file_id: 812, name: "接口文档.docx", ext: "docx", root_online: true, playable: false, can_open: false, state_text: "",
        },
      },
      copies: [],
      windows: [
        {
          start_ms: 0,
          end_ms: 90_000,
          items: [
            {
              content_key: "q2:3f6c0000000000000000", ordinal: 14, loc: "第三节", start_ms: null,
              text: "字段命名统一用小驼峰", words: ["字段命名"], at_ms: 2_000,
            },
          ],
        },
      ],
      rejected: 0,
    });
    const onOpenPreview = vi.fn();
    const apiClient = {
      relatedMaterials,
      asrGoldSamples: vi.fn().mockResolvedValue({ items: [] }),
    } as unknown as ApiClient;
    const page = (
      <MeetingDetailPage
        apiClient={apiClient}
        canWriteTasks
        initialSeekMs={0}
        isMobile={false}
        meeting={meeting(false)}
        onBack={vi.fn()}
        onOpenPreview={onOpenPreview}
        onReload={vi.fn().mockResolvedValue(undefined)}
        projects={[]}
        tags={[]}
      />
    );
    const { unmount } = render(
      <LinksFlagsContext.Provider value={{ linksEnabled: true, semanticEnabled: true, llmConfigured: false }}>
        {page}
      </LinksFlagsContext.Provider>,
    );
    const inspector = document.querySelector("aside.edit-inspector") as HTMLElement;
    const section = await within(inspector).findByText("接口文档.docx");
    expect(inspector.firstElementChild?.contains(section)).toBe(true);
    expect(relatedMaterials).toHaveBeenCalledWith("vm-1");
    await userEvent.click(screen.getByRole("button", { name: "预览 接口文档.docx 第三节" }));
    expect(onOpenPreview).toHaveBeenCalledWith({
      fileId: 812,
      passage: { contentKey: "q2:3f6c0000000000000000", ordinal: 14, from: "related", words: ["字段命名"] },
    });
    unmount();
    relatedMaterials.mockClear();
    render(page);
    expect(screen.queryByText("相关材料")).not.toBeInTheDocument();
    expect(relatedMaterials).not.toHaveBeenCalled();
  });

  it("在放时手动滚逐字稿，栏跟阅读线下那一行，4 秒后回到播放位置；暂停时停在滚到的地方", async () => {
    const { LinksFlagsContext } = await import("./links/LinksFlagsContext");
    const relatedMaterials = vi.fn().mockResolvedValue({
      state: { kind: "ok", text: null, action: null }, files: {}, copies: [], windows: [], rejected: 0,
    });
    // 这场会有录音：波形换成假的（jsdom 里画不了）
    const WaveSurfer = (await import("wavesurfer.js")).default;
    const waveform = vi.spyOn(WaveSurfer, "create").mockReturnValue({
      destroy: vi.fn(),
      load: vi.fn().mockResolvedValue(undefined),
      on: vi.fn().mockReturnValue(() => undefined),
      setOptions: vi.fn(),
      setTime: vi.fn(),
    } as unknown as ReturnType<typeof WaveSurfer.create>);
    const segments: Segment[] = [
      { id: "seg-a", ordinal: 0, start_ms: 0, end_ms: 60_000, speaker_name: "甲", text: "先说接口文档" },
      { id: "seg-b", ordinal: 1, start_ms: 100_000, end_ms: 110_000, speaker_name: "乙", text: "再说驻场服务" },
    ];
    render(
      <LinksFlagsContext.Provider value={{ linksEnabled: true, semanticEnabled: true, llmConfigured: false }}>
        <MeetingDetailPage
          apiClient={{ relatedMaterials, asrGoldSamples: vi.fn().mockResolvedValue({ items: [] }) } as unknown as ApiClient}
          initialSeekMs={0}
          isMobile={false}
          meeting={{
            ...meeting(false),
            segments,
            artifacts: [{ id: 7, meeting_id: "vm-1", kind: "audio", role: "original", source_root: "", path: "" }],
          }}
          onBack={vi.fn()}
          onReload={vi.fn().mockResolvedValue(undefined)}
          projects={[]}
          tags={[]}
        />
      </LinksFlagsContext.Provider>,
    );
    const inspector = document.querySelector("aside.edit-inspector") as HTMLElement;
    await within(inspector).findByText("00:00 前后");
    const position = () => inspector.querySelector(".related-head small")?.textContent;
    const raf = vi.spyOn(window, "requestAnimationFrame").mockImplementation((callback) => {
      callback(0);
      return 1;
    });
    const scrollIntoView = Element.prototype.scrollIntoView;
    Element.prototype.scrollIntoView = vi.fn();
    const box = document.querySelector(".transcript-scroll") as HTMLDivElement;
    Object.defineProperty(box, "clientHeight", { configurable: true, value: 300 });
    Object.defineProperty(box, "scrollTop", { configurable: true, value: 100, writable: true });
    vi.spyOn(box, "getBoundingClientRect").mockReturnValue({ top: 0 } as DOMRect);
    vi.spyOn(screen.getByTestId("segment-seg-a"), "getBoundingClientRect").mockReturnValue({ top: -100 } as DOMRect);
    // 阅读线在 100 + 300/3 = 200，seg-b 的上沿 150 在线上：阅读位置 01:40，落在 01:30 那一段
    vi.spyOn(screen.getByTestId("segment-seg-b"), "getBoundingClientRect").mockReturnValue({ top: 50 } as DOMRect);
    const audio = screen.getByLabelText("录音播放器") as HTMLAudioElement;
    Object.defineProperty(audio, "currentTime", { configurable: true, value: 10, writable: true });
    const userScroll = () => {
      fireEvent.wheel(box);
      fireEvent.scroll(box);
    };
    vi.useFakeTimers();
    try {
      fireEvent.play(audio);
      fireEvent.timeUpdate(audio);
      expect(position()).toBe("00:00 前后");
      userScroll();
      expect(position()).toBe("01:30 前后");
      await act(async () => {
        await vi.advanceTimersByTimeAsync(3_900);
      });
      expect(position()).toBe("01:30 前后");
      await act(async () => {
        await vi.advanceTimersByTimeAsync(200);
      });
      expect(position()).toBe("00:00 前后");
      // 暂停时滚：停在滚到的地方，过了 4 秒也不回去
      fireEvent.pause(audio);
      userScroll();
      expect(position()).toBe("01:30 前后");
      await act(async () => {
        await vi.advanceTimersByTimeAsync(10_000);
      });
      expect(position()).toBe("01:30 前后");
    } finally {
      vi.useRealTimers();
      raf.mockRestore();
      waveform.mockRestore();
      Element.prototype.scrollIntoView = scrollIntoView;
    }
  });
});

describe("MeetingDetailPage 抽需求候选（R01-4）", () => {
  const withMinutes: MeetingDetail = {
    ...meeting(false),
    current_minutes_version_id: "mv-1",
    minutes_versions: [
      {
        id: "mv-1",
        meeting_id: "vm-1",
        version_no: 1,
        kind: "generated",
        published: 1,
        markdown: "# 纪要",
        created_at: "2026-09-30T02:46:40Z",
      },
    ],
  };

  function renderPage(page: MeetingDetail, api: Partial<ApiClient>, props: { canWriteTasks?: boolean } = {}) {
    const onOpenPendingCandidates = vi.fn();
    render(
      <MeetingDetailPage
        apiClient={{ transcriptVersionSegments: vi.fn(), ...api } as unknown as ApiClient}
        canWriteTasks={props.canWriteTasks ?? true}
        initialSeekMs={0}
        isMobile={false}
        meeting={page}
        onBack={vi.fn()}
        onOpenPendingCandidates={onOpenPendingCandidates}
        onReload={vi.fn().mockResolvedValue(undefined)}
        projects={[]}
        tags={[]}
      />,
    );
    return { onOpenPendingCandidates };
  }

  it("有纪要的会在归档归属里能手动补抽，抽完提示放在待认领，［去看看］打开需求池", async () => {
    const extractRequirementCandidates = vi
      .fn()
      .mockResolvedValue({ status: "done", created: 1, updated: 1, merged: 1, removed: 2 });
    const { onOpenPendingCandidates } = renderPage(withMinutes, { extractRequirementCandidates });

    await userEvent.click(screen.getByRole("button", { name: "抽需求候选" }));

    expect(extractRequirementCandidates).toHaveBeenCalledWith("vm-1");
    expect(
      await screen.findByText(
        "抽出 2 条需求候选，放在需求池「待认领」；另有 1 条并进了已有的候选；原来待认领、这次没再抽到的 2 条撤下了",
      ),
    ).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "去看看" }));
    expect(onOpenPendingCandidates).toHaveBeenCalledTimes(1);
  });

  it("没抽出来、没配 AI、抽失败各给一句；没抽出来时不给［去看看］", async () => {
    const extractRequirementCandidates = vi
      .fn()
      .mockResolvedValueOnce({ status: "done", created: 0, merged: 0 })
      .mockResolvedValueOnce({ status: "unavailable", created: 0, merged: 0 })
      .mockResolvedValueOnce({ status: "failed", created: 0, merged: 0 });
    renderPage(withMinutes, { extractRequirementCandidates });
    const button = screen.getByRole("button", { name: "抽需求候选" });

    await userEvent.click(button);
    expect(await screen.findByText("这场会没抽出新的需求候选")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "去看看" })).not.toBeInTheDocument();
    await userEvent.click(button);
    expect(await screen.findByText("没配置 AI，抽不了需求候选")).toBeInTheDocument();
    await userEvent.click(button);
    expect(await screen.findByRole("alert")).toHaveTextContent("抽需求候选失败，请稍后再试");
  });

  it("还没有纪要、或者不能写（手机只读）时没有这个按钮", () => {
    renderPage(meeting(false), {});
    expect(screen.queryByRole("button", { name: "抽需求候选" })).not.toBeInTheDocument();
  });
});

describe("MeetingDetailPage 从原话时间进来（R02-3）、被新增页盖住", () => {
  function pageWithAudio(props: { autoplay?: boolean; covered?: boolean }) {
    // 有录音时播放器会去取波形；让它一直等着，用例里不真建播放器
    vi.stubGlobal("fetch", vi.fn(() => new Promise(() => {})));
    const page: MeetingDetail = {
      ...meeting(false),
      duration_ms: 60_000,
      artifacts: [{ id: 42, kind: "audio", role: "source", path: "/x/vm-1.m4a" } as MeetingDetail["artifacts"][number]],
    };
    return (
      <MeetingDetailPage
        apiClient={client(vi.fn())}
        initialSeekMs={0}
        isMobile={false}
        meeting={page}
        onBack={vi.fn()}
        onReload={vi.fn().mockResolvedValue(undefined)}
        projects={[]}
        tags={[]}
        {...props}
      />
    );
  }

  it("带着 autoplay 打开：读到录音就开始放；不带不放", () => {
    const play = vi.spyOn(HTMLMediaElement.prototype, "play").mockResolvedValue(undefined);
    const { container, unmount } = render(pageWithAudio({ autoplay: true }));
    const audio = container.querySelector("audio")!;
    Object.defineProperty(audio, "readyState", { configurable: true, value: HTMLMediaElement.HAVE_METADATA });
    fireEvent(audio, new Event("loadedmetadata"));
    expect(play).toHaveBeenCalledTimes(1);
    unmount();

    play.mockClear();
    const plain = render(pageWithAudio({}));
    const quiet = plain.container.querySelector("audio")!;
    Object.defineProperty(quiet, "readyState", { configurable: true, value: HTMLMediaElement.HAVE_METADATA });
    fireEvent(quiet, new Event("loadedmetadata"));
    expect(play).not.toHaveBeenCalled();
    play.mockRestore();
    vi.unstubAllGlobals();
  });

  it("新增页盖上来时停下录音：页面上看不到播放器了（第二轮审查建议 2）", () => {
    const pause = vi.spyOn(HTMLMediaElement.prototype, "pause").mockImplementation(() => undefined);
    const { rerender } = render(pageWithAudio({}));
    expect(pause).not.toHaveBeenCalled();

    rerender(pageWithAudio({ covered: true }));

    expect(pause).toHaveBeenCalledTimes(1);
    pause.mockRestore();
    vi.unstubAllGlobals();
  });
});

describe("MeetingDetailPage 逐字稿选句建需求（R01-10）", () => {
  it("选中一段［建成需求］：带着这场会、选中的原话、第一句的时间和会议归属去新增需求页", () => {
    // 有录音时播放器会去取波形；让它一直等着，用例里不真建播放器
    vi.stubGlobal("fetch", vi.fn(() => new Promise(() => {})));
    const onCreateRequirement = vi.fn();
    const page: MeetingDetail = {
      ...meeting(false),
      project_id: "project-a",
      project_name: "云图AI",
      recording_date: "2026-07-10T09:00:00+08:00",
      duration_ms: 60_000,
      artifacts: [{ id: 42, kind: "audio", role: "source", path: "/x/vm-1.m4a" } as MeetingDetail["artifacts"][number]],
    };
    render(
      <MeetingDetailPage
        apiClient={client(vi.fn())}
        canWriteTasks
        initialSeekMs={0}
        isMobile={false}
        meeting={page}
        onBack={vi.fn()}
        onCreateRequirement={onCreateRequirement}
        onReload={vi.fn().mockResolvedValue(undefined)}
        projects={[{ id: "project-a", name: "云图AI", color: "#376f68" }]}
        tags={[]}
      />,
    );

    const text = screen.getByTestId("segment-seg-main").querySelector(".segment-text")!.firstChild!;
    const range = document.createRange();
    range.setStart(text, 3);
    range.setEnd(text, text.textContent!.length);
    window.getSelection()!.removeAllRanges();
    window.getSelection()!.addRange(range);
    fireEvent.mouseUp(screen.getByTestId("segment-seg-main"));
    fireEvent.click(screen.getByRole("button", { name: "建成需求" }));

    expect(onCreateRequirement).toHaveBeenCalledWith({
      source: {
        meeting_id: "vm-1",
        meeting_title: "对照测试会议",
        recording_date: "2026-07-10T09:00:00+08:00",
        duration_ms: 60_000,
        audio_artifact_id: 42,
        quote: "FunASR 当前工作稿",
        anchor_ms: 0,
      },
      projectId: "project-a",
    });
    vi.unstubAllGlobals();
  });

  it("改了逐字稿、退出编辑但没保存：选句时浮条只说原因，不带没存下来的字去建需求（审查 M5）", async () => {
    const onCreateRequirement = vi.fn();
    render(
      <MeetingDetailPage
        apiClient={client(vi.fn())}
        canWriteTasks
        initialSeekMs={0}
        isMobile={false}
        meeting={{ ...meeting(false), project_id: "project-a", project_name: "云图AI" }}
        onBack={vi.fn()}
        onCreateRequirement={onCreateRequirement}
        onReload={vi.fn().mockResolvedValue(undefined)}
        projects={[{ id: "project-a", name: "云图AI", color: "#376f68" }]}
        tags={[]}
      />,
    );
    await userEvent.click(screen.getByRole("button", { name: "编辑逐字稿" }));
    const textarea = screen.getByLabelText("00:00 逐字稿") as HTMLTextAreaElement;
    fireEvent.change(textarea, { target: { value: `${textarea.value}（改过没存）` } });
    await userEvent.click(screen.getByRole("button", { name: "退出编辑" }));

    const text = screen.getByTestId("segment-seg-main").querySelector(".segment-text")!.firstChild!;
    const range = document.createRange();
    range.setStart(text, 0);
    range.setEnd(text, text.textContent!.length);
    window.getSelection()!.removeAllRanges();
    window.getSelection()!.addRange(range);
    fireEvent.mouseUp(screen.getByTestId("segment-seg-main"));

    expect(screen.getByRole("toolbar", { name: "选中的原话" })).toHaveTextContent("逐字稿有没保存的修改，先保存或放弃再选句");
    expect(screen.queryByRole("button", { name: "建成需求" })).not.toBeInTheDocument();
    expect(onCreateRequirement).not.toHaveBeenCalled();
  });
});

describe("MeetingDetailPage 同一场会静默刷新不冲掉没保存的编辑", () => {
  // 服务器上这场会的当前样子；保存、词典改过来之后由用例改它，onReload 照 App 的静默刷新换一个新对象
  interface Server {
    transcriptVersion: string;
    text: string;
    minutesVersion: string;
    markdown: string;
  }
  function detailOf(server: Server, extra: Partial<MeetingDetail> = {}): MeetingDetail {
    return {
      id: "vm-1",
      title: "会",
      status: "completed_unreviewed",
      tags: [],
      artifacts: [],
      segments: [{ id: "s1", ordinal: 0, start_ms: 0, end_ms: 5_000, text: server.text }],
      speakers: [],
      events: [],
      current_transcript_version_id: server.transcriptVersion,
      current_minutes_version_id: server.minutesVersion,
      transcript_versions: [
        { id: server.transcriptVersion, meeting_id: "vm-1", version_no: 1, kind: "funasr", published: 0, created_at: "2026-07-10T00:00:00Z" },
      ],
      minutes_versions: [
        { id: server.minutesVersion, meeting_id: "vm-1", version_no: 1, markdown: server.markdown, kind: "ai", published: 0, created_at: "2026-07-10T00:00:00Z" },
      ],
      ...extra,
    };
  }
  function renderHost(apiClient: ApiClient, server: Server, extra: Partial<MeetingDetail> = {}) {
    const onDirty = vi.fn();
    let flipMobile: (value: boolean) => void = () => undefined;
    function Host() {
      const [detail, setDetail] = useState(() => detailOf(server, extra));
      const [isMobile, setIsMobile] = useState(false);
      // App 的 loadDetail 静默刷新后会把自己记的 dirty 清掉，页面要重新报一次
      const [appDirty, setAppDirty] = useState(false);
      const [reloads, setReloads] = useState(0);
      flipMobile = setIsMobile;
      onDirty.mockImplementation(setAppDirty);
      return (
        <>
          <output data-testid="app-dirty">{String(appDirty)}</output>
          <output data-testid="reloads">{reloads}</output>
          <MeetingDetailPage
            apiClient={apiClient}
            initialSeekMs={0}
            isMobile={isMobile}
            meeting={detail}
            onBack={vi.fn()}
            onDirtyChange={onDirty}
            onReload={async () => {
              setDetail(detailOf(server, extra));
              setAppDirty(false);
              setReloads((count) => count + 1);
            }}
            projects={[]}
            tags={[]}
          />
        </>
      );
    }
    render(<Host />);
    return { setMobile: (value: boolean) => act(() => flipMobile(value)) };
  }
  // 等静默刷新落地，再等页面把「还有没保存的」重新报给外层
  async function settledAfterReload(count: number) {
    await waitFor(() => expect(screen.getByTestId("reloads")).toHaveTextContent(String(count)));
    await waitFor(() => expect(screen.getByTestId("app-dirty")).toHaveTextContent("true"));
  }
  async function editMinutes(text: string) {
    await userEvent.click(screen.getByRole("tab", { name: /会议纪要/ }));
    if (!screen.queryByRole("textbox", { name: "会议纪要编辑器" })) {
      await userEvent.click(screen.getByRole("button", { name: "编辑纪要" }));
    }
    await userEvent.type(screen.getByRole("textbox", { name: "会议纪要编辑器" }), text);
  }
  async function editTranscript(text: string) {
    await userEvent.click(screen.getByRole("tab", { name: /逐字稿/ }));
    if (!screen.queryByRole("button", { name: "保存草稿" })) {
      await userEvent.click(screen.getByRole("button", { name: "编辑逐字稿" }));
    }
    const row = screen.getByLabelText("00:00 逐字稿");
    await userEvent.clear(row);
    await userEvent.type(row, text);
  }

  it("纪要改了没保存，去保存逐字稿：纪要改动还在，之后保存纪要仍按打开时的版本去比", async () => {
    const server: Server = { transcriptVersion: "tv-1", text: "原句", minutesVersion: "mv-1", markdown: "服务器上的纪要" };
    const saveTranscript = vi.fn().mockImplementation(async (_id: string, segments: Segment[]) => {
      server.transcriptVersion = "tv-2";
      server.text = segments[0].text;
      return { version_id: "tv-2" };
    });
    const saveMinutes = vi.fn().mockResolvedValue({ version_id: "mv-2" });
    renderHost({ transcriptVersionSegments: vi.fn(), saveTranscript, saveMinutes } as unknown as ApiClient, server);

    await editMinutes("——我加的一大段");
    await editTranscript("改过的句子");
    await userEvent.click(screen.getByRole("button", { name: "保存草稿" }));
    expect(saveTranscript).toHaveBeenCalledWith("vm-1", expect.any(Array), "tv-1");
    // 纪要还是没保存的状态，外层也知道
    await settledAfterReload(1);

    // 逐字稿这侧跟着服务器，不再算没保存
    expect(screen.queryByRole("button", { name: "保存草稿" })).not.toBeInTheDocument();
    expect(screen.getByText("改过的句子")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("tab", { name: /会议纪要/ }));
    expect(screen.getByRole("textbox", { name: "会议纪要编辑器" })).toHaveValue("服务器上的纪要——我加的一大段");
    await userEvent.click(screen.getByRole("button", { name: "保存纪要草稿" }));
    expect(saveMinutes).toHaveBeenCalledWith("vm-1", "服务器上的纪要——我加的一大段", "mv-1");
  });

  it("逐字稿改了没保存，去保存纪要：逐字稿改动还在，之后保存逐字稿仍按打开时的版本去比", async () => {
    const server: Server = { transcriptVersion: "tv-1", text: "原句", minutesVersion: "mv-1", markdown: "服务器上的纪要" };
    const saveMinutes = vi.fn().mockImplementation(async (_id: string, markdown: string) => {
      server.minutesVersion = "mv-2";
      server.markdown = markdown;
      return { version_id: "mv-2" };
    });
    const saveTranscript = vi.fn().mockResolvedValue({ version_id: "tv-2" });
    renderHost({ transcriptVersionSegments: vi.fn(), saveTranscript, saveMinutes } as unknown as ApiClient, server);

    await editTranscript("我改的句子");
    await editMinutes("——补一句");
    await userEvent.click(screen.getByRole("button", { name: "保存纪要草稿" }));
    expect(saveMinutes).toHaveBeenCalledWith("vm-1", "服务器上的纪要——补一句", "mv-1");
    await settledAfterReload(1);
    expect(screen.getByText("服务器上的纪要——补一句")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("tab", { name: /逐字稿/ }));
    expect(screen.getByLabelText("00:00 逐字稿")).toHaveValue("我改的句子");
    await userEvent.click(screen.getByRole("button", { name: "保存草稿" }));
    expect(saveTranscript).toHaveBeenCalledWith("vm-1", expect.any(Array), "tv-1");
  });

  it("逐字稿改了没保存，词典「改过来」改了纪要：纪要换成服务器的新内容，逐字稿改动还在", async () => {
    const server: Server = { transcriptVersion: "tv-1", text: "原句", minutesVersion: "mv-1", markdown: "云途的纪要" };
    const glossary = {
      basis: "public", project: null, meeting_project: null, mismatch: false, receipt: null,
      minutes_version_id: "mv-1", stale: false, checked_at: "2026-09-26T10:00:00+00:00", corrected: [],
      missed: [{ kind: "missed", term: "云图", wrong: "云途", term_project_id: null, transcript_count: 0, minutes_count: 1 }],
      applied: null,
    } satisfies MeetingDetail["glossary"];
    const applyMeetingGlossary = vi.fn().mockImplementation(async () => {
      server.minutesVersion = "mv-2";
      server.markdown = "云图的纪要";
      return { version_id: "mv-2", replaced: 1, glossary: null };
    });
    renderHost(
      { transcriptVersionSegments: vi.fn(), applyMeetingGlossary, checkMeetingGlossary: vi.fn() } as unknown as ApiClient,
      server,
      { glossary },
    );

    await editTranscript("我改的句子");
    await userEvent.click(screen.getByRole("tab", { name: /会议纪要/ }));
    await userEvent.click(screen.getByRole("button", { name: "改过来" }));
    expect(applyMeetingGlossary).toHaveBeenCalledWith("vm-1", "mv-1");
    await settledAfterReload(1);
    expect(screen.getByText("云图的纪要")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("tab", { name: /逐字稿/ }));
    expect(screen.getByLabelText("00:00 逐字稿")).toHaveValue("我改的句子");
  });

  it("纪要没保存时服务器上的纪要被别处改了：本地改动保留，保存时走版本冲突，丢弃后才换成最新", async () => {
    const server: Server = { transcriptVersion: "tv-1", text: "原句", minutesVersion: "mv-1", markdown: "服务器上的纪要" };
    const saveTranscript = vi.fn().mockImplementation(async () => {
      // 保存逐字稿的同时，别处（另一个窗口、重新生成）把纪要改成了新版本
      server.transcriptVersion = "tv-2";
      server.minutesVersion = "mv-9";
      server.markdown = "别处改过的纪要";
      return { version_id: "tv-2" };
    });
    const saveMinutes = vi.fn().mockRejectedValue(new ApiError("纪要已有更新版本", 409));
    renderHost({ transcriptVersionSegments: vi.fn(), saveTranscript, saveMinutes } as unknown as ApiClient, server);

    await editMinutes("——我加的");
    await editTranscript("改过的句子");
    await userEvent.click(screen.getByRole("button", { name: "保存草稿" }));
    await settledAfterReload(1);

    await userEvent.click(screen.getByRole("tab", { name: /会议纪要/ }));
    expect(screen.getByRole("textbox", { name: "会议纪要编辑器" })).toHaveValue("服务器上的纪要——我加的");
    await userEvent.click(screen.getByRole("button", { name: "保存纪要草稿" }));
    expect(saveMinutes).toHaveBeenCalledWith("vm-1", "服务器上的纪要——我加的", "mv-1");
    expect(await screen.findByRole("heading", { name: "纪要有更新版本" })).toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: "会议纪要编辑器" })).toHaveValue("服务器上的纪要——我加的");

    await userEvent.click(screen.getByRole("button", { name: "丢弃我的修改并加载最新" }));
    await userEvent.click(within(screen.getByRole("alertdialog")).getByRole("button", { name: "丢弃并加载最新" }));
    expect(await screen.findByText("别处改过的纪要")).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "纪要有更新版本" })).not.toBeInTheDocument();
    await waitFor(() => expect(screen.getByTestId("app-dirty")).toHaveTextContent("false"));
  });

  it("窗口跨过手机断点再回来：纪要和逐字稿没保存的改动都还在", async () => {
    const server: Server = { transcriptVersion: "tv-1", text: "原句", minutesVersion: "mv-1", markdown: "服务器上的纪要" };
    const { setMobile } = renderHost({ transcriptVersionSegments: vi.fn() } as unknown as ApiClient, server);

    await editTranscript("我改的句子");
    await editMinutes("——我加的");
    setMobile(true);
    setMobile(false);

    await userEvent.click(screen.getByRole("tab", { name: /会议纪要/ }));
    if (!screen.queryByRole("textbox", { name: "会议纪要编辑器" })) {
      await userEvent.click(screen.getByRole("button", { name: "编辑纪要" }));
    }
    expect(screen.getByRole("textbox", { name: "会议纪要编辑器" })).toHaveValue("服务器上的纪要——我加的");
    await userEvent.click(screen.getByRole("tab", { name: /逐字稿/ }));
    expect(screen.getByText("我改的句子")).toBeInTheDocument();
  });

  it("归属勾了没保存、热词填了没用，去保存逐字稿：都还在", async () => {
    const server: Server = { transcriptVersion: "tv-1", text: "原句", minutesVersion: "mv-1", markdown: "纪要" };
    const saveTranscript = vi.fn().mockImplementation(async (_id: string, segments: Segment[]) => {
      server.transcriptVersion = "tv-2";
      server.text = segments[0].text;
      return { version_id: "tv-2" };
    });
    function Host() {
      const [detail, setDetail] = useState(() => detailOf(server));
      return (
        <MeetingDetailPage
          apiClient={{ transcriptVersionSegments: vi.fn(), saveTranscript } as unknown as ApiClient}
          initialSeekMs={0}
          isMobile={false}
          meeting={detail}
          onBack={vi.fn()}
          onReload={async () => setDetail(detailOf(server))}
          projects={[]}
          tags={[{ id: "tag-a", name: "周会", color: "#376f68" }]}
        />
      );
    }
    render(<Host />);
    await userEvent.click(screen.getByRole("checkbox", { name: "周会" }));
    await userEvent.type(screen.getByLabelText("重新转写本场热词"), "云图");
    await editTranscript("改过的句子");
    await userEvent.click(screen.getByRole("button", { name: "保存草稿" }));

    await waitFor(() => expect(screen.getByText("改过的句子")).toBeInTheDocument());
    expect(screen.getByRole("checkbox", { name: "周会" })).toBeChecked();
    expect(screen.getByRole("button", { name: "保存归档归属" })).toBeEnabled();
    expect(screen.getByLabelText("重新转写本场热词")).toHaveValue("云图");
  });

  it("换了一场会才全部重置", async () => {
    const first = detailOf({ transcriptVersion: "tv-1", text: "原句", minutesVersion: "mv-1", markdown: "第一场纪要" });
    const second = { ...detailOf({ transcriptVersion: "tv-b", text: "第二场原句", minutesVersion: "mv-b", markdown: "第二场纪要" }), id: "vm-2" };
    const props = {
      apiClient: { transcriptVersionSegments: vi.fn() } as unknown as ApiClient,
      initialSeekMs: 0,
      isMobile: false,
      onBack: vi.fn(),
      onReload: vi.fn(),
      projects: [],
      tags: [],
    };
    const { rerender } = render(<MeetingDetailPage {...props} meeting={first} />);
    await editMinutes("——我加的");
    rerender(<MeetingDetailPage {...props} meeting={second} />);
    expect(screen.queryByRole("textbox", { name: "会议纪要编辑器" })).not.toBeInTheDocument();
    expect(screen.getByText("第二场纪要")).toBeInTheDocument();
  });
});
