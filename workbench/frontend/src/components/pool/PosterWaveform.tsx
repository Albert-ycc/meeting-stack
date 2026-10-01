import { useEffect, useRef, useState } from "react";

import "./PosterWaveform.css";

interface Peaks {
  duration_seconds: number;
  peaks: number[];
}

/*
 * 海报上的声波用这场会录音的真实波形（会议详情播放器在用的 /api/media/<id>/peaks）。
 * 同一段录音在墙上、认领页、详情页只取一次；没生成过的峰值第一次要现算（ffmpeg），
 * 所以同时最多取 3 段，墙上十几张海报一起挂上来时不把机器压满。取不到就不画（R02 异常）。
 */
const peaksCache = new Map<number, Promise<Peaks | null>>();
const MAX_CONCURRENT = 3;
let running = 0;
const waiting: Array<() => void> = [];

function runQueued<T>(task: () => Promise<T>): Promise<T> {
  return new Promise<T>((resolve) => {
    const start = () => {
      running += 1;
      void task()
        .then(resolve)
        .finally(() => {
          running -= 1;
          waiting.shift()?.();
        });
    };
    if (running < MAX_CONCURRENT) start();
    else waiting.push(start);
  });
}

export function loadPeaks(artifactId: number): Promise<Peaks | null> {
  let cached = peaksCache.get(artifactId);
  if (!cached) {
    cached = runQueued(async () => {
      try {
        const response = await fetch(`/api/media/${artifactId}/peaks`, {
          credentials: "same-origin",
          headers: { Accept: "application/json" },
        });
        if (!response.ok) return null;
        const payload = (await response.json()) as Peaks;
        return Array.isArray(payload.peaks) && payload.peaks.length > 0 && payload.duration_seconds > 0
          ? payload
          : null;
      } catch {
        return null;
      }
    });
    peaksCache.set(artifactId, cached);
  }
  return cached;
}

/** 测试之间清空 */
export function clearPeaksCache() {
  peaksCache.clear();
}

/** 把峰值按柱数降采样：每一段取最大值，细节少了但起伏还在。 */
export function downsamplePeaks(peaks: number[], bars: number): number[] {
  if (peaks.length <= bars) return peaks;
  const result: number[] = [];
  const step = peaks.length / bars;
  for (let index = 0; index < bars; index += 1) {
    let max = 0;
    for (let cursor = Math.floor(index * step); cursor < Math.floor((index + 1) * step); cursor += 1) {
      max = Math.max(max, peaks[cursor] ?? 0);
    }
    result.push(max);
  }
  return result;
}

export interface WaveMarker {
  atMs: number;
  /** origin：提出它的那句（橙色实线）；merged：合并进来的原话（橙色细线） */
  kind?: "origin" | "merged";
}

interface PosterWaveformProps {
  artifactId: number | null;
  markers: WaveMarker[];
  /** 柱子数，海报上 96 根，认领页、详情页更宽 */
  bars?: number;
  height?: number;
  /** 点波形：从第一个标记那一秒开始放（R02-3） */
  onActivate?: () => void;
  label?: string;
}

export function PosterWaveform({ artifactId, markers, bars = 96, height = 28, onActivate, label }: PosterWaveformProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const [visible, setVisible] = useState(false);
  const [peaks, setPeaks] = useState<Peaks | null>(null);

  // 进了视口才取峰值：墙往下拉才看得到的海报不抢前面的
  useEffect(() => {
    const element = containerRef.current;
    if (!element || artifactId === null) return;
    if (typeof IntersectionObserver === "undefined") {
      setVisible(true);
      return;
    }
    const observer = new IntersectionObserver((entries) => {
      if (entries.some((entry) => entry.isIntersecting)) {
        setVisible(true);
        observer.disconnect();
      }
    });
    observer.observe(element);
    return () => observer.disconnect();
  }, [artifactId]);

  useEffect(() => {
    if (!visible || artifactId === null) return;
    let active = true;
    void loadPeaks(artifactId).then((payload) => {
      if (active) setPeaks(payload);
    });
    return () => {
      active = false;
    };
  }, [artifactId, visible]);

  if (artifactId === null) return null;
  const durationMs = peaks ? peaks.duration_seconds * 1000 : 0;
  const values = peaks ? downsamplePeaks(peaks.peaks, bars) : [];
  const ceiling = Math.max(0.05, ...values);
  const anchor = markers[0]?.atMs ?? null;
  const anchorBar = anchor !== null && durationMs > 0 ? (anchor / durationMs) * values.length : -1;
  const width = values.length * 3;
  const placed = durationMs > 0 ? markers.filter((marker) => marker.atMs >= 0 && marker.atMs <= durationMs) : [];

  return (
    <div className="poster-wave" ref={containerRef} style={{ height }}>
      {peaks && (
        <svg
          aria-label={label ?? "录音波形"}
          className={`poster-wave__svg ${onActivate ? "is-clickable" : ""}`}
          onClick={(event) => {
            if (!onActivate) return;
            event.stopPropagation();
            onActivate();
          }}
          onKeyDown={(event) => {
            if (!onActivate || (event.key !== "Enter" && event.key !== " ")) return;
            event.preventDefault();
            event.stopPropagation();
            onActivate();
          }}
          preserveAspectRatio="none"
          role={onActivate ? "button" : "img"}
          tabIndex={onActivate ? 0 : undefined}
          viewBox={`0 0 ${width} ${height}`}
        >
          {values.map((value, index) => {
            const barHeight = Math.max(2, (value / ceiling) * (height - 2));
            return (
              <rect
                className={index < anchorBar ? "poster-wave__bar is-before" : "poster-wave__bar"}
                height={barHeight}
                key={index}
                rx={0.8}
                width={2}
                x={index * 3}
                y={(height - barHeight) / 2}
              />
            );
          })}
          {placed.map((marker, index) => (
            <rect
              className={`poster-wave__marker ${marker.kind === "merged" ? "is-merged" : ""}`}
              height={height}
              key={`${marker.atMs}-${index}`}
              width={marker.kind === "merged" ? 1.5 : 2}
              x={Math.min(width - 2, (marker.atMs / durationMs) * width)}
              y={0}
            />
          ))}
        </svg>
      )}
    </div>
  );
}
