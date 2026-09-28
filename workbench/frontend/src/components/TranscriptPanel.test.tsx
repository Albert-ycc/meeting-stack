import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { rowAtLine, TranscriptPanel } from "./TranscriptPanel";

const segments = [
  {
    id: "seg-a",
    ordinal: 0,
    start_ms: 0,
    end_ms: 5_000,
    speaker_label: "speaker_0",
    speaker_name: "甲",
    text: "先确认范围。",
  },
  {
    id: "seg-b",
    ordinal: 1,
    start_ms: 5_000,
    end_ms: 12_000,
    speaker_label: "speaker_1",
    speaker_name: "乙",
    text: "范围已经确认。",
  },
];

describe("TranscriptPanel", () => {
  it("highlights the segment containing the playback time", () => {
    render(
      <TranscriptPanel
        currentTimeMs={7_200}
        editable={false}
        onSeek={vi.fn()}
        segments={segments}
      />,
    );

    expect(screen.getByTestId("segment-seg-b")).toHaveAttribute("aria-current", "true");
    expect(screen.getByTestId("segment-seg-a")).not.toHaveAttribute("aria-current");
  });

  it("captures the textarea cursor before updating split state", () => {
    const onSplit = vi.fn();
    render(
      <TranscriptPanel
        currentTimeMs={0}
        editable
        onSeek={vi.fn()}
        onSplit={onSplit}
        segments={segments}
      />,
    );
    const textarea = screen.getByLabelText("00:00 逐字稿") as HTMLTextAreaElement;
    textarea.setSelectionRange(3, 3);
    fireEvent.keyUp(textarea, { key: "ArrowRight" });

    const split = screen.getAllByRole("button", { name: "从光标拆分" })[0];
    expect(split).toBeEnabled();
    fireEvent.click(split);
    expect(onSplit).toHaveBeenCalledWith("seg-a", 3);
  });

  it("每行带 data-start-ms（4d）", () => {
    render(<TranscriptPanel currentTimeMs={0} editable={false} onSeek={vi.fn()} segments={segments} />);
    expect(screen.getByTestId("segment-seg-b")).toHaveAttribute("data-start-ms", "5000");
  });

  it("用户自己滚动时上报阅读线下那一行，自动跟随时上报 null（4d）", () => {
    const onReadingTimeChange = vi.fn();
    const raf = vi.spyOn(window, "requestAnimationFrame").mockImplementation((callback) => {
      callback(0);
      return 1;
    });
    const scrollIntoView = vi.fn();
    Element.prototype.scrollIntoView = scrollIntoView;
    const { container, rerender } = render(
      <TranscriptPanel currentTimeMs={0} editable={false} onReadingTimeChange={onReadingTimeChange} onSeek={vi.fn()} segments={segments} />,
    );
    // 自动跟随滚到播放行：上报 null
    expect(onReadingTimeChange).toHaveBeenLastCalledWith(null);
    const box = container.querySelector(".transcript-scroll") as HTMLDivElement;
    Object.defineProperty(box, "clientHeight", { configurable: true, value: 300 });
    Object.defineProperty(box, "scrollTop", { configurable: true, value: 100, writable: true });
    vi.spyOn(box, "getBoundingClientRect").mockReturnValue({ top: 0 } as DOMRect);
    vi.spyOn(screen.getByTestId("segment-seg-a"), "getBoundingClientRect").mockReturnValue({ top: -100 } as DOMRect);
    vi.spyOn(screen.getByTestId("segment-seg-b"), "getBoundingClientRect").mockReturnValue({ top: 50 } as DOMRect);
    // 没有手动滚（程序滚）的 scroll 不上报
    onReadingTimeChange.mockClear();
    fireEvent.scroll(box);
    expect(onReadingTimeChange).not.toHaveBeenCalled();
    // 滚轮之后的 scroll：阅读线在 100 + 300/3 = 200，seg-b 的上沿 150 在线上
    fireEvent.wheel(box);
    fireEvent.scroll(box);
    expect(onReadingTimeChange).toHaveBeenLastCalledWith(5_000);
    // 翻页键也算
    onReadingTimeChange.mockClear();
    rerender(
      <TranscriptPanel currentTimeMs={0} editable={false} onReadingTimeChange={onReadingTimeChange} onSeek={vi.fn()} segments={segments} />,
    );
    fireEvent.keyDown(box, { key: "PageDown" });
    fireEvent.scroll(box);
    expect(onReadingTimeChange).toHaveBeenLastCalledWith(5_000);
    raf.mockRestore();
  });

  it("rowAtLine：上沿不超过阅读线的最后一行", () => {
    const rows = [
      { startMs: 0, top: 0 },
      { startMs: 5_000, top: 120 },
      { startMs: 9_000, top: 260 },
    ];
    expect(rowAtLine(rows, 0, 300)).toBe(0);
    expect(rowAtLine(rows, 30, 300)).toBe(5_000);
    expect(rowAtLine(rows, 200, 300)).toBe(9_000);
    expect(rowAtLine([], 0, 300)).toBeNull();
  });
});
