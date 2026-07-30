import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { TranscriptPanel } from "./TranscriptPanel";

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
});
