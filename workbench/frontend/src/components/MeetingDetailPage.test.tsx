import { act, fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { ApiError, type ApiClient } from "../api";
import type { MeetingDetail, Segment } from "../types";
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
      requirement_ids: [],
    });
  });

  it("disables the requirement picker with a placeholder until a project is chosen", async () => {
    const apiClient = { transcriptVersionSegments: vi.fn() } as unknown as ApiClient;
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

    expect(screen.getByText("先选择主项目")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "＋ 关联需求" })).not.toBeInTheDocument();

    await userEvent.selectOptions(screen.getByLabelText("主项目"), "project-a");
    expect(screen.queryByText("先选择主项目")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "＋ 关联需求" })).toBeInTheDocument();
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

    expect(updateMeeting).toHaveBeenCalledWith("vm-1", {
      project_id: "project-a",
      tag_ids: [],
      requirement_ids: ["req-1"],
    });
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
    expect(screen.getByText("由会议纪要自动匹配；保存一次后不再自动改动")).toBeInTheDocument();

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
    expect(screen.queryByText("由会议纪要自动匹配；保存一次后不再自动改动")).not.toBeInTheDocument();
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

    await userEvent.click(screen.getByRole("button", { name: "← 返回资料库" }));
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
    await userEvent.click(screen.getByRole("button", { name: "← 返回资料库" }));
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
    expect(screen.getByRole("button", { name: "← 返回资料库" })).toBeDisabled();
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
    expect(screen.getByRole("button", { name: "← 返回资料库" })).toBeDisabled();

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
    expect(updateMeeting).toHaveBeenLastCalledWith("vm-1", {
      project_id: "project-b",
      tag_ids: [],
      requirement_ids: [],
    });
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
