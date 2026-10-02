import { useCallback, useEffect, useRef, useState } from "react";

import { formatTime } from "../../format";
import { claimSound } from "../soundFocus";

/** 点 ▶ 从原话前 3 秒播到后 15 秒。 */
export const PLAY_BEFORE_MS = 3_000;
export const PLAY_AFTER_MS = 15_000;

export interface PlayOptions {
  /** 默认只放原话前 3 秒到后 15 秒；材料录音传 false，从那一刻一直放下去 */
  clip?: boolean;
}

export interface MiniPlayerHandle {
  play: (url: string, atMs: number, label: string, options?: PlayOptions) => void;
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
  // 还在等元数据的那次起播：新点一个 ▶ 时要撤掉，不然元数据到了会跳回上一次点的位置
  const pendingRef = useRef<(() => void) | null>(null);

  const play = useCallback((url: string, atMs: number, label: string, options?: PlayOptions) => {
    const clipped = options?.clip !== false;
    const start = clipped ? Math.max(0, atMs - PLAY_BEFORE_MS) : Math.max(0, atMs);
    stopAtRef.current = clipped ? atMs + PLAY_AFTER_MS : Number.POSITIVE_INFINITY;
    setClip({ url, atMs, label });
    setPositionMs(start);
    const audio = audioRef.current;
    if (!audio) return;
    const stale = pendingRef.current;
    if (stale) audio.removeEventListener("loadedmetadata", stale);
    pendingRef.current = null;
    const begin = () => {
      pendingRef.current = null;
      audio.currentTime = start / 1000;
      void audio.play?.()?.catch?.(() => setPlaying(false));
    };
    const wait = () => {
      pendingRef.current = begin;
      audio.addEventListener("loadedmetadata", begin, { once: true });
    };
    if (audio.getAttribute("src") !== url) {
      audio.setAttribute("src", url);
      wait();
      audio.load?.();
    } else if (stale) {
      // 同一个录音还没加载完：接着等，元数据到了从这一次点的位置起播；上次没加载成就重新加载
      wait();
      if (audio.error) audio.load?.();
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
    const onPlay = () => {
      claimSound(audio);
      setPlaying(true);
    };
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

  return { play, audioElement, node, clip, playing, positionMs };
}
