import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { AsrGoldSample, MinutesEvidence, TranscriptComparisonItem } from "../types";
import { MinutesEvidencePanel, TranscriptComparisonPanel } from "./QualityReviewPanels";

const row: TranscriptComparisonItem = {
  primary_segment_id: "seg-1",
  candidate_segment_ids: ["ref-1"],
  start_ms: 12_000,
  end_ms: 15_000,
  primary_text: "预算 125 万元，进入 CRM",
  candidate_text: "预算 120 万元，进入 SCRM",
  risk_kinds: ["number", "latin_term"],
  similarity: 0.82,
};

describe("TranscriptComparisonPanel", () => {
  it("blocks gold writes until the gold ledger is ready and offers an explicit retry", async () => {
    const onRetryGold = vi.fn();
    const onSaveGold = vi.fn();
    const { rerender } = render(
      <TranscriptComparisonPanel candidateLabel="Whisper" currentTimeMs={0} goldSamples={[]} goldState="loading" isMobile={false} items={[row]} onRetryGold={onRetryGold} onSaveGold={onSaveGold} onSeek={vi.fn()} />,
    );
    expect(screen.getByRole("button", { name: "金标加载中" })).toBeDisabled();
    rerender(<TranscriptComparisonPanel candidateLabel="Whisper" currentTimeMs={0} goldSamples={[]} goldState="error" isMobile={false} items={[row]} onRetryGold={onRetryGold} onSaveGold={onSaveGold} onSeek={vi.fn()} />);
    expect(screen.getByRole("alert")).toHaveTextContent("金标读取失败");
    expect(screen.getByRole("button", { name: "标为金标" })).toBeDisabled();
    await userEvent.click(screen.getByRole("button", { name: "重试读取金标" }));
    expect(onRetryGold).toHaveBeenCalledTimes(1);
    expect(onSaveGold).not.toHaveBeenCalled();
  });

  it("reports gold dirty state and keeps it after a failed save until cancel", async () => {
    const onGoldDirtyChange = vi.fn();
    render(
      <TranscriptComparisonPanel candidateLabel="Whisper" currentTimeMs={0} goldSamples={[]} goldState="ready" isMobile={false} items={[row]} onGoldDirtyChange={onGoldDirtyChange} onRetryGold={vi.fn()} onSaveGold={vi.fn().mockRejectedValue(new Error("版本冲突"))} onSeek={vi.fn()} />,
    );
    await userEvent.click(screen.getByRole("button", { name: "标为金标" }));
    expect(onGoldDirtyChange).toHaveBeenLastCalledWith(true);
    await userEvent.type(screen.getByLabelText("seg-1 金标文本"), "修订");
    await userEvent.click(screen.getByRole("button", { name: "确认保存金标" }));
    expect(screen.getByText("版本冲突")).toBeInTheDocument();
    expect(onGoldDirtyChange).toHaveBeenLastCalledWith(true);
    await userEvent.click(screen.getByRole("button", { name: "取消" }));
    expect(onGoldDirtyChange).toHaveBeenLastCalledWith(false);
  });

  it("does not clear parent dirty state merely because the comparison panel unmounts", async () => {
    const onGoldDirtyChange = vi.fn();
    const { unmount } = render(
      <TranscriptComparisonPanel candidateLabel="Whisper" currentTimeMs={0} goldSamples={[]} goldState="ready" isMobile={false} items={[row]} onGoldDirtyChange={onGoldDirtyChange} onSaveGold={vi.fn()} onSeek={vi.fn()} />,
    );
    await userEvent.click(screen.getByRole("button", { name: "标为金标" }));
    expect(onGoldDirtyChange).toHaveBeenLastCalledWith(true);
    unmount();
    expect(onGoldDirtyChange).toHaveBeenLastCalledWith(true);
  });

  it("requires confirmation before replacing an unsaved gold edit with another row", async () => {
    const confirm = vi.spyOn(window, "confirm").mockReturnValueOnce(false).mockReturnValueOnce(true);
    render(<TranscriptComparisonPanel candidateLabel="Whisper" currentTimeMs={0} goldSamples={[]} goldState="ready" isMobile={false} items={[row, { ...row, primary_segment_id: "seg-2", primary_text: "第二段" }]} onSaveGold={vi.fn()} onSeek={vi.fn()} />);
    await userEvent.click(screen.getAllByRole("button", { name: "标为金标" })[0]);
    await userEvent.clear(screen.getByLabelText("seg-1 金标文本"));
    await userEvent.type(screen.getByLabelText("seg-1 金标文本"), "A 未保存");
    await userEvent.click(screen.getAllByRole("button", { name: "标为金标" })[1]);
    expect(screen.getByLabelText("seg-1 金标文本")).toHaveValue("A 未保存");
    expect(screen.queryByLabelText("seg-2 金标文本")).not.toBeInTheDocument();
    await userEvent.click(screen.getAllByRole("button", { name: "标为金标" })[1]);
    expect(screen.getByLabelText("seg-2 金标文本")).toHaveValue("第二段");
    expect(confirm).toHaveBeenCalledTimes(2);
    confirm.mockRestore();
  });

  it("does not reset text, cursor or dirty state when the active row trigger is invoked again", async () => {
    const confirm = vi.spyOn(window, "confirm");
    const onGoldDirtyChange = vi.fn();
    render(<TranscriptComparisonPanel candidateLabel="Whisper" currentTimeMs={0} goldSamples={[]} goldState="ready" isMobile={false} items={[row]} onGoldDirtyChange={onGoldDirtyChange} onSaveGold={vi.fn()} onSeek={vi.fn()} />);
    const trigger = screen.getByRole("button", { name: "标为金标" });
    await userEvent.click(trigger);
    const editor = screen.getByLabelText("seg-1 金标文本") as HTMLTextAreaElement;
    await userEvent.clear(editor);
    await userEvent.type(editor, "保留这段人工修订");
    editor.setSelectionRange(4, 4);
    expect(trigger).toBeDisabled();
    await userEvent.click(trigger);
    expect(editor).toHaveValue("保留这段人工修订");
    expect(editor.selectionStart).toBe(4);
    expect(onGoldDirtyChange).toHaveBeenLastCalledWith(true);
    expect(confirm).not.toHaveBeenCalled();
    confirm.mockRestore();
  });

  it("locks every row trigger during save and closes only the request editor", async () => {
    let resolveSave!: (sample: AsrGoldSample) => void;
    const onSaveGold = vi.fn().mockReturnValue(new Promise<AsrGoldSample>((resolve) => { resolveSave = resolve; }));
    render(<TranscriptComparisonPanel candidateLabel="Whisper" currentTimeMs={0} goldSamples={[]} goldState="ready" isMobile={false} items={[row, { ...row, primary_segment_id: "seg-2", primary_text: "第二段" }]} onSaveGold={onSaveGold} onSeek={vi.fn()} />);
    await userEvent.click(screen.getAllByRole("button", { name: "标为金标" })[0]);
    await userEvent.click(screen.getByRole("button", { name: "确认保存金标" }));
    expect(screen.getAllByRole("button", { name: "标为金标" }).every((button) => button.hasAttribute("disabled"))).toBe(true);
    await userEvent.click(screen.getAllByRole("button", { name: "标为金标" })[1]);
    expect(screen.queryByLabelText("seg-2 金标文本")).not.toBeInTheDocument();
    resolveSave({ id: "gold-a", meeting_id: "vm-1", segment_id: "seg-1", start_ms: 12_000, end_ms: 15_000, reference: row.primary_text, entities: [], numbers: [], tags: [], created_at: "2026-07-14T00:00:00Z", updated_at: "2026-07-14T00:00:00Z" });
    expect(await screen.findByRole("button", { name: "更新金标" })).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "标为金标" }));
    expect(screen.getByLabelText("seg-2 金标文本")).toHaveValue("第二段");
  });

  it("shows evidence-based risks and seeks from mouse and keyboard without fake highlights", async () => {
    const onSeek = vi.fn();
    const { container } = render(
      <TranscriptComparisonPanel
        candidateLabel="Qwen 影子稿"
        currentTimeMs={0}
        goldSamples={[]}
        isMobile={false}
        items={[row, { ...row, primary_segment_id: "seg-2", candidate_segment_ids: [], candidate_text: "", risk_kinds: ["missing_candidate"], start_ms: 20_000 }]}
        onSaveGold={vi.fn()}
        onSeek={onSeek}
      />,
    );

    expect(screen.getByText("数字差异")).toBeInTheDocument();
    expect(screen.getByText("英文术语差异")).toBeInTheDocument();
    expect(screen.getByText("候选缺失")).toBeInTheDocument();
    expect(container.querySelector("mark")).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "对照第 1 段，跳转到 00:12" }));
    expect(onSeek).toHaveBeenLastCalledWith(12_000);
    const comparisonAnchor = screen.getByRole("button", { name: "对照第 2 段，跳转到 00:20" });
    comparisonAnchor.focus();
    await userEvent.keyboard("{Enter}");
    expect(onSeek).toHaveBeenLastCalledWith(20_000);
  });

  it("saves an editable desktop gold sample and labels an existing sample as update", async () => {
    const onSaveGold = vi.fn().mockImplementation(async (_segmentId: string, reference: string) => ({
      id: "gold-1",
      meeting_id: "vm-1",
      segment_id: "seg-1",
      start_ms: 12_000,
      end_ms: 15_000,
      reference,
      entities: [],
      numbers: [],
      tags: [],
      created_at: "2026-07-14T00:00:00Z",
      updated_at: "2026-07-14T00:00:00Z",
    } satisfies AsrGoldSample));
    render(
      <TranscriptComparisonPanel
        candidateLabel="Whisper 对照稿"
        currentTimeMs={12_500}
        goldSamples={[]}
        isMobile={false}
        items={[row]}
        onSaveGold={onSaveGold}
        onSeek={vi.fn()}
      />,
    );

    await userEvent.click(screen.getByRole("button", { name: "标为金标" }));
    const editor = screen.getByLabelText("seg-1 金标文本");
    expect(editor).toHaveValue(row.primary_text);
    await userEvent.clear(editor);
    await userEvent.type(editor, "人工校正后的文本");
    await userEvent.click(screen.getByRole("button", { name: "确认保存金标" }));
    expect(onSaveGold).toHaveBeenCalledWith("seg-1", "人工校正后的文本");
    expect(await screen.findByRole("button", { name: "更新金标" })).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("金标已保存");
  });

  it("is strictly read-only on mobile", () => {
    render(
      <TranscriptComparisonPanel
        candidateLabel="Whisper 对照稿"
        currentTimeMs={0}
        goldSamples={[]}
        isMobile
        items={[row]}
        onSaveGold={vi.fn()}
        onSeek={vi.fn()}
      />,
    );
    expect(screen.queryByRole("button", { name: "标为金标" })).not.toBeInTheDocument();
  });
});

const evidence: MinutesEvidence = {
  coverage: { total_items: 2, included_items: 1, omitted_items: 1 },
  topics: [{
    topic_id: "T01",
    title: "预算决策",
    start_sec: 10,
    end_sec: 30,
    items: [
      { item_id: "D01", kind: "decision", text: "确认预算", status: "included", source_start_sec: 12, source_end_sec: 15, minutes_anchor: "[00:00:12]" },
      { item_id: "R01", kind: "risk", text: "未决风险", status: "omitted", source_start_sec: 20, source_end_sec: 22, omitted_reason: "讨论未形成结论" },
    ],
  }],
  anchors: [{ item_id: "D01", minutes_anchor: "[00:00:12]", source_start_sec: 12, source_end_sec: 15 }],
};

describe("MinutesEvidencePanel", () => {
  it("separates included and omitted items and seeks their source anchors", async () => {
    const onSeek = vi.fn();
    render(<MinutesEvidencePanel evidence={evidence} onSeek={onSeek} state="ready" />);
    expect(screen.getByText("2 条证据")).toBeInTheDocument();
    expect(screen.getByText("已写入 1")).toBeInTheDocument();
    expect(screen.getByText("明确省略 1")).toBeInTheDocument();
    expect(screen.getByText("讨论未形成结论")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "跳转到证据 00:12" }));
    expect(onSeek).toHaveBeenCalledWith(12_000);
  });

  it("uses honest, non-path-leaking copy for missing and unverifiable evidence", () => {
    const { rerender } = render(<MinutesEvidencePanel evidence={null} onSeek={vi.fn()} state="missing" />);
    expect(screen.getByText("该会议暂无可验证证据")).toBeInTheDocument();
    rerender(<MinutesEvidencePanel evidence={null} message="来源版本不一致" onSeek={vi.fn()} state="unavailable" />);
    expect(screen.getByText("证据暂不可验证")).toBeInTheDocument();
    expect(screen.getByText("来源版本不一致")).toBeInTheDocument();
  });
});
