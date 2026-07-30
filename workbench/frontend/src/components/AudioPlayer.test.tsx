import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { AudioPlayer } from "./AudioPlayer";

const setTime = vi.fn();

vi.mock("wavesurfer.js", () => ({
  default: {
    create: () => ({
      destroy: vi.fn(),
      load: vi.fn().mockResolvedValue(undefined),
      on: vi.fn().mockReturnValue(() => undefined),
      setTime,
    }),
  },
}));

describe("AudioPlayer", () => {
  beforeEach(() => {
    setTime.mockClear();
  });

  it("updates the audio anchor while dragging across the waveform", () => {
    const onTimeChange = vi.fn();
    render(
      <AudioPlayer
        durationMs={10_000}
        mediaUrl="/api/media/1"
        onTimeChange={onTimeChange}
      />,
    );
    const waveform = screen.getByLabelText("音频波形");
    vi.spyOn(waveform, "getBoundingClientRect").mockReturnValue({
      bottom: 86,
      height: 86,
      left: 0,
      right: 100,
      top: 0,
      width: 100,
      x: 0,
      y: 0,
      toJSON: () => ({}),
    });

    fireEvent.pointerDown(waveform, { clientX: 20, pointerId: 1 });
    fireEvent.pointerMove(waveform, { clientX: 80, pointerId: 1 });
    fireEvent.pointerUp(waveform, { clientX: 80, pointerId: 1 });

    expect(onTimeChange).toHaveBeenLastCalledWith(8_000);
    expect(setTime).toHaveBeenLastCalledWith(8);
  });

  it("lets keyboard users move the waveform anchor with arrow keys", () => {
    const onTimeChange = vi.fn();
    render(
      <AudioPlayer
        durationMs={60_000}
        mediaUrl="/api/media/1"
        onTimeChange={onTimeChange}
      />,
    );

    const waveform = screen.getByLabelText("音频波形");
    fireEvent.keyDown(waveform, { key: "ArrowRight" });

    expect(waveform).toHaveAttribute("tabindex", "0");
    expect(onTimeChange).toHaveBeenLastCalledWith(10_000);
    expect(setTime).toHaveBeenLastCalledWith(10);
  });

  it("exposes playback controls as a sticky region", () => {
    render(
      <AudioPlayer
        durationMs={60_000}
        mediaUrl="/api/media/1"
        onTimeChange={vi.fn()}
      />,
    );

    expect(screen.getByRole("region", { name: "吸顶录音播放控制" })).toHaveClass(
      "audio-console--sticky",
    );
  });
});
