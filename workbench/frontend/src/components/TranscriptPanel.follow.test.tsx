import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { Segment } from "../types";
import { TranscriptPanel } from "./TranscriptPanel";

const segments: Segment[] = [
  { id: "a", ordinal: 0, start_ms: 0, end_ms: 10_000, speaker_name: "甲", text: "第一句" },
  { id: "b", ordinal: 1, start_ms: 10_000, end_ms: 20_000, speaker_name: "乙", text: "第二句" },
  { id: "c", ordinal: 2, start_ms: 20_000, end_ms: 30_000, speaker_name: "甲", text: "第三句" },
];

// 播放时跟随当前句：只有正在打字、检索（焦点在输入框里）时不跟；点了时间键、焦点停在键上要跟，
// 因为点时间现在会从那里开始放
describe("TranscriptPanel 跟随当前句", () => {
  const original = Element.prototype.scrollIntoView;
  const scrollIntoView = vi.fn();
  beforeEach(() => {
    scrollIntoView.mockClear();
    Element.prototype.scrollIntoView = scrollIntoView;
  });
  afterEach(() => {
    Element.prototype.scrollIntoView = original;
  });

  it("点了时间键（焦点停在键上）以后，播放推进到下一句照样跟随", async () => {
    const onSeek = vi.fn();
    const { rerender } = render(<TranscriptPanel currentTimeMs={1_000} editable={false} onSeek={onSeek} segments={segments} />);
    await userEvent.click(screen.getByRole("button", { name: "00:10" }));
    expect(onSeek).toHaveBeenCalledWith(10_000);
    expect(screen.getByRole("button", { name: "00:10" })).toHaveFocus();
    scrollIntoView.mockClear();

    rerender(<TranscriptPanel currentTimeMs={20_500} editable={false} onSeek={onSeek} segments={segments} />);

    expect(scrollIntoView).toHaveBeenCalledTimes(1);
    expect(scrollIntoView.mock.contexts[0]).toBe(screen.getByTestId("segment-c"));
  });

  it("焦点在查找框里（还在打字）时不跟，免得把视线拽走", async () => {
    const { rerender } = render(<TranscriptPanel currentTimeMs={1_000} editable={false} onSeek={vi.fn()} segments={segments} />);
    await userEvent.click(screen.getByLabelText("在本次逐字稿中搜索"));
    scrollIntoView.mockClear();

    rerender(<TranscriptPanel currentTimeMs={20_500} editable={false} onSeek={vi.fn()} segments={segments} />);

    expect(scrollIntoView).not.toHaveBeenCalled();
  });

  it("编辑态不跟，焦点在文本框里改字时更不跟", async () => {
    const { rerender } = render(<TranscriptPanel currentTimeMs={1_000} editable onChange={vi.fn()} onSeek={vi.fn()} segments={segments} />);
    await userEvent.click(screen.getByLabelText("00:10 逐字稿"));
    scrollIntoView.mockClear();

    rerender(<TranscriptPanel currentTimeMs={20_500} editable onChange={vi.fn()} onSeek={vi.fn()} segments={segments} />);

    expect(scrollIntoView).not.toHaveBeenCalled();
  });

  it("焦点在页面别处（比如播放器）时照常跟", () => {
    const { rerender } = render(<TranscriptPanel currentTimeMs={1_000} editable={false} onSeek={vi.fn()} segments={segments} />);
    scrollIntoView.mockClear();

    rerender(<TranscriptPanel currentTimeMs={20_500} editable={false} onSeek={vi.fn()} segments={segments} />);

    expect(scrollIntoView).toHaveBeenCalledTimes(1);
  });
});
