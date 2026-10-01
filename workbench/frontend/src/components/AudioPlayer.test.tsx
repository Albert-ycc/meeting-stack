import { act, fireEvent, render, screen } from "@testing-library/react";
import { createRef } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { AudioPlayer, type AudioPlayerHandle } from "./AudioPlayer";

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

  it("还没读到录音时说放：读到元数据、跳到要去的那一秒以后再放（从原话时间进会议，R02-3）", () => {
    const play = vi.spyOn(HTMLMediaElement.prototype, "play").mockResolvedValue(undefined);
    const ref = createRef<AudioPlayerHandle>();
    const onTimeChange = vi.fn();
    const { container } = render(
      <AudioPlayer durationMs={747_000} initialSeekMs={576_900} mediaUrl="/api/media/1" onTimeChange={onTimeChange} ref={ref} />,
    );
    const audio = container.querySelector("audio")!;

    act(() => ref.current!.play());
    expect(play).not.toHaveBeenCalled();

    Object.defineProperty(audio, "readyState", { configurable: true, value: HTMLMediaElement.HAVE_METADATA });
    fireEvent(audio, new Event("loadedmetadata"));

    expect(onTimeChange).toHaveBeenLastCalledWith(576_900);
    expect(play).toHaveBeenCalledTimes(1);
    play.mockRestore();
  });

  it("说了放又说停（新增页盖上来）：读到录音以后也不放", () => {
    const play = vi.spyOn(HTMLMediaElement.prototype, "play").mockResolvedValue(undefined);
    const pause = vi.spyOn(HTMLMediaElement.prototype, "pause").mockImplementation(() => undefined);
    const ref = createRef<AudioPlayerHandle>();
    const { container } = render(<AudioPlayer durationMs={747_000} mediaUrl="/api/media/1" onTimeChange={vi.fn()} ref={ref} />);
    const audio = container.querySelector("audio")!;

    act(() => ref.current!.play());
    act(() => ref.current!.pause());
    Object.defineProperty(audio, "readyState", { configurable: true, value: HTMLMediaElement.HAVE_METADATA });
    fireEvent(audio, new Event("loadedmetadata"));

    expect(pause).toHaveBeenCalledTimes(1);
    expect(play).not.toHaveBeenCalled();
    play.mockRestore();
    pause.mockRestore();
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

  it("在 <audio> 的 play、pause、ended 上报在不在放（4d）", () => {
    const onPlayingChange = vi.fn();
    render(<AudioPlayer durationMs={10_000} mediaUrl="/api/media/1" onPlayingChange={onPlayingChange} onTimeChange={vi.fn()} />);
    const audio = screen.getByLabelText("录音播放器");
    fireEvent.play(audio);
    expect(onPlayingChange).toHaveBeenLastCalledWith(true);
    fireEvent.pause(audio);
    expect(onPlayingChange).toHaveBeenLastCalledWith(false);
    fireEvent.play(audio);
    fireEvent.ended(audio);
    expect(onPlayingChange).toHaveBeenLastCalledWith(false);
    expect(onPlayingChange).toHaveBeenCalledTimes(4);
  });
});
