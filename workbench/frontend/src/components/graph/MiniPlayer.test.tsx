import { act, render } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { useMiniPlayer, type MiniPlayerHandle } from "./MiniPlayer";

function Probe({ onReady }: { onReady: (play: MiniPlayerHandle["play"]) => void }) {
  const player = useMiniPlayer();
  onReady(player.play);
  return player.audioElement;
}

/** jsdom 放不了音频：记下每次设的 currentTime，元数据到没到由用例手动发 loadedmetadata */
function setup() {
  let play: MiniPlayerHandle["play"] = () => undefined;
  const { container } = render(<Probe onReady={(fn) => (play = fn)} />);
  const audio = container.querySelector("audio") as HTMLAudioElement;
  const seeks: number[] = [];
  Object.defineProperty(audio, "currentTime", {
    configurable: true,
    get: () => seeks.at(-1) ?? 0,
    set: (value: number) => seeks.push(value),
  });
  audio.play = vi.fn(async () => undefined);
  audio.load = vi.fn();
  return { audio, seeks, play: (...args: Parameters<MiniPlayerHandle["play"]>) => act(() => play(...args)) };
}

const metadata = (audio: HTMLAudioElement) => act(() => void audio.dispatchEvent(new Event("loadedmetadata")));

describe("useMiniPlayer", () => {
  it("同一个录音还没加载完时连点两个 ▶，加载完从第二次点的位置起播", () => {
    const { audio, seeks, play } = setup();
    play("/api/media/1", 10_000, "第一处");
    play("/api/media/1", 300_000, "第二处");
    expect(seeks).toEqual([]);
    metadata(audio);
    expect(seeks).toEqual([297]);
  });

  it("先点 A 录音、没加载完又点 B 录音，B 的元数据到了只按 B 的位置起播", () => {
    const { audio, seeks, play } = setup();
    play("/api/media/a", 10_000, "A");
    play("/api/media/b", 60_000, "B");
    metadata(audio);
    expect(seeks).toEqual([57]);
  });

  it("加载完以后再点同一个录音的 ▶，直接跳过去", () => {
    const { audio, seeks, play } = setup();
    play("/api/media/1", 10_000, "第一处");
    metadata(audio);
    play("/api/media/1", 300_000, "第二处");
    expect(seeks).toEqual([7, 297]);
    // 已经起播过的那次不会再被后来的元数据事件拉回去
    metadata(audio);
    expect(seeks).toEqual([7, 297]);
  });
});
