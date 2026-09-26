import { useCallback, useEffect, useRef, useState } from "react";

import { formatTime } from "../../format";

/** 点 ▶ 从原话前 3 秒播到后 15 秒。 */
export const PLAY_BEFORE_MS = 3_000;
export const PLAY_AFTER_MS = 15_000;

export interface MiniPlayerHandle {
  play: (url: string, atMs: number, label: string) => void;
}

interface Clip {
  url: string;
  atMs: number;
  label: string;
}

/**
 * 关系图面板顶上常驻的迷你播放器，全图只有一个。媒体接口支持拖进度条，直接设 currentTime。
 * audio 元素要一直挂着（换面板时不断播、事件只绑一次），控制条只在和会议有关的面板里出现。
 */
export function useMiniPlayer() {
  const audioRef = useRef<HTMLAudioElement>(null);
  const [clip, setClip] = useState<Clip | null>(null);
  const [playing, setPlaying] = useState(false);
  const [positionMs, setPositionMs] = useState(0);
  const stopAtRef = useRef(0);

  const play = useCallback((url: string, atMs: number, label: string) => {
    const start = Math.max(0, atMs - PLAY_BEFORE_MS);
    stopAtRef.current = atMs + PLAY_AFTER_MS;
    setClip({ url, atMs, label });
    setPositionMs(start);
    const audio = audioRef.current;
    if (!audio) return;
    const begin = () => {
      audio.currentTime = start / 1000;
      void audio.play?.()?.catch?.(() => setPlaying(false));
    };
    if (audio.getAttribute("src") !== url) {
      audio.setAttribute("src", url);
      audio.addEventListener("loadedmetadata", begin, { once: true });
      audio.load?.();
    } else {
      begin();
    }
  }, []);

  useEffect(() => {
    const audio = audioRef.current;
    if (!audio) return;
    const onTime = () => {
      const now = audio.currentTime * 1000;
      setPositionMs(now);
      if (now >= stopAtRef.current) audio.pause();
    };
    const onPlay = () => setPlaying(true);
    const onPause = () => setPlaying(false);
    audio.addEventListener("timeupdate", onTime);
    audio.addEventListener("play", onPlay);
    audio.addEventListener("pause", onPause);
    return () => {
      audio.removeEventListener("timeupdate", onTime);
      audio.removeEventListener("play", onPlay);
      audio.removeEventListener("pause", onPause);
    };
  }, []);

  const toggle = () => {
    const audio = audioRef.current;
    if (!audio || !clip) return;
    if (playing) audio.pause();
    else {
      if (audio.currentTime * 1000 >= stopAtRef.current) {
        stopAtRef.current = audio.currentTime * 1000 + PLAY_AFTER_MS;
      }
      void audio.play?.()?.catch?.(() => setPlaying(false));
    }
  };

  const audioElement = <audio className="graph-player__audio" preload="none" ref={audioRef} />;

  const node = (
    <div aria-label="迷你播放器" className="graph-player" role="group">
      <button
        aria-label={playing ? "暂停" : "播放"}
        className="graph-player__toggle"
        disabled={!clip}
        onClick={toggle}
        type="button"
      >
        {playing ? "❚❚" : "▶"}
      </button>
      <span className="graph-player__label">
        {clip ? `${clip.label} · ${formatTime(positionMs)}` : "点任何一个 ▶，从原话前 3 秒播起"}
      </span>
    </div>
  );

  return { play, audioElement, node, clip, playing };
}
