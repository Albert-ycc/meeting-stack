import {
  forwardRef,
  useEffect,
  useImperativeHandle,
  useRef,
  useState,
} from "react";
import type { PointerEvent as ReactPointerEvent } from "react";
import WaveSurfer from "wavesurfer.js";

import { formatTime } from "../format";

export interface AudioPlayerHandle {
  seekTo: (milliseconds: number) => void;
}

interface AudioPlayerProps {
  durationMs?: number | null;
  initialSeekMs?: number;
  mediaUrl: string | null;
  peaksUrl?: string | null;
  onTimeChange: (milliseconds: number) => void;
}

export const AudioPlayer = forwardRef<AudioPlayerHandle, AudioPlayerProps>(function AudioPlayer(
  { durationMs, initialSeekMs = 0, mediaUrl, peaksUrl, onTimeChange },
  ref,
) {
  const audioRef = useRef<HTMLAudioElement>(null);
  const waveformRef = useRef<HTMLDivElement>(null);
  const waveSurferRef = useRef<WaveSurfer | null>(null);
  const draggingWaveformRef = useRef(false);
  const pendingSeekRef = useRef<number | null>(initialSeekMs > 0 ? initialSeekMs : null);
  const [currentMs, setCurrentMs] = useState(0);
  const [duration, setDuration] = useState(durationMs ?? 0);
  const [rate, setRate] = useState(1);
  const [waveformState, setWaveformState] = useState<"loading" | "ready" | "fallback">("loading");

  const seekTo = (milliseconds: number) => {
    const seconds = Math.max(0, milliseconds / 1000);
    pendingSeekRef.current = milliseconds;
    if (audioRef.current && audioRef.current.readyState >= HTMLMediaElement.HAVE_METADATA) {
      audioRef.current.currentTime = seconds;
      pendingSeekRef.current = null;
    }
    waveSurferRef.current?.setTime(seconds);
    setCurrentMs(milliseconds);
    onTimeChange(milliseconds);
  };

  useImperativeHandle(ref, () => ({ seekTo }));

  useEffect(() => {
    if (initialSeekMs <= 0) return;
    pendingSeekRef.current = initialSeekMs;
    if (audioRef.current && audioRef.current.readyState >= HTMLMediaElement.HAVE_METADATA) {
      seekTo(initialSeekMs);
    }
  }, [initialSeekMs]);

  useEffect(() => {
    const audio = audioRef.current;
    const container = waveformRef.current;
    if (!audio || !container || !mediaUrl) {
      setWaveformState("fallback");
      return;
    }

    setWaveformState("loading");
    let cancelled = false;
    let wavesurfer: WaveSurfer | null = null;
    let unsubReady: () => void = () => {};
    let unsubError: () => void = () => {};
    const createWaveform = (peaks?: number[], seconds?: number) => {
      wavesurfer = WaveSurfer.create({
        container,
        media: audio,
        height: 86,
        barGap: 2,
        barRadius: 2,
        barWidth: 2,
        cursorColor: "#ff7a3d",
        cursorWidth: 2,
        dragToSeek: true,
        progressColor: "#ff7a3d",
        waveColor: "#5c6e69",
        normalize: true,
        ...(peaks && seconds ? { peaks: [peaks], duration: seconds } : {}),
      });
      waveSurferRef.current = wavesurfer;
      unsubReady = wavesurfer.on("ready", (readySeconds) => {
        setDuration(readySeconds * 1000);
        setWaveformState("ready");
        if (pendingSeekRef.current !== null) seekTo(pendingSeekRef.current);
      });
      unsubError = wavesurfer.on("error", () => setWaveformState("fallback"));
      if (peaks && seconds) {
        setDuration(seconds * 1000);
        setWaveformState("ready");
        if (pendingSeekRef.current !== null) seekTo(pendingSeekRef.current);
      } else {
        void wavesurfer.load(mediaUrl).catch(() => setWaveformState("fallback"));
      }
    };

    void (async () => {
      if (peaksUrl) {
        try {
          const response = await fetch(peaksUrl, {
            credentials: "same-origin",
            headers: { Accept: "application/json" },
          });
          if (!response.ok) throw new Error("peaks unavailable");
          const payload = (await response.json()) as {
            duration_seconds: number;
            peaks: number[];
          };
          if (!cancelled && payload.peaks.length > 0 && payload.duration_seconds > 0) {
            createWaveform(payload.peaks, payload.duration_seconds);
            return;
          }
        } catch {
          // Short or unsupported files can still use WaveSurfer's browser decoder.
        }
      }
      if (!cancelled) createWaveform();
    })();

    return () => {
      cancelled = true;
      unsubReady();
      unsubError();
      waveSurferRef.current = null;
      wavesurfer?.destroy();
    };
    // The initial anchor is intentionally consumed when a new media source loads.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [mediaUrl, peaksUrl]);

  useEffect(() => {
    const audio = audioRef.current;
    if (!audio) return;
    const updateTime = () => {
      const value = audio.currentTime * 1000;
      setCurrentMs(value);
      onTimeChange(value);
    };
    const updateDuration = () => {
      setDuration(audio.duration * 1000 || durationMs || 0);
      if (pendingSeekRef.current !== null) seekTo(pendingSeekRef.current);
    };
    audio.addEventListener("timeupdate", updateTime);
    audio.addEventListener("loadedmetadata", updateDuration);
    return () => {
      audio.removeEventListener("timeupdate", updateTime);
      audio.removeEventListener("loadedmetadata", updateDuration);
    };
  }, [durationMs, onTimeChange]);

  const jump = (seconds: number) => seekTo(currentMs + seconds * 1000);
  const changeRate = (value: number) => {
    setRate(value);
    if (audioRef.current) audioRef.current.playbackRate = value;
  };
  const seekFromWaveformPointer = (event: ReactPointerEvent<HTMLDivElement>) => {
    const totalDuration = duration || durationMs || 0;
    const bounds = event.currentTarget.getBoundingClientRect();
    if (totalDuration <= 0 || bounds.width <= 0) return;
    const ratio = Math.min(1, Math.max(0, (event.clientX - bounds.left) / bounds.width));
    seekTo(ratio * totalDuration);
  };
  const startWaveformDrag = (event: ReactPointerEvent<HTMLDivElement>) => {
    draggingWaveformRef.current = true;
    event.currentTarget.setPointerCapture?.(event.pointerId);
    seekFromWaveformPointer(event);
  };
  const moveWaveformDrag = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (draggingWaveformRef.current) seekFromWaveformPointer(event);
  };
  const stopWaveformDrag = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (!draggingWaveformRef.current) return;
    seekFromWaveformPointer(event);
    draggingWaveformRef.current = false;
    event.currentTarget.releasePointerCapture?.(event.pointerId);
  };
  const moveWaveformWithKeyboard = (event: React.KeyboardEvent<HTMLDivElement>) => {
    if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
    event.preventDefault();
    jump(event.key === "ArrowLeft" ? -10 : 10);
  };

  if (!mediaUrl) {
    return (
      <div className="audio-missing" role="status">
        <span>∿</span>
        <div><strong>原音频暂不可用</strong><p>逐字稿仍可阅读；来源路径会保留在档案记录中。</p></div>
      </div>
    );
  }

  return (
    <>
      <section aria-label="录音波形" className="audio-waveform-console">
        <div className="waveform-frame">
          <div
            aria-label="音频波形"
            aria-valuemax={Math.round((duration || durationMs || 0) / 1000)}
            aria-valuemin={0}
            aria-valuenow={Math.round(currentMs / 1000)}
            className="waveform"
            onKeyDown={moveWaveformWithKeyboard}
            onPointerCancel={() => { draggingWaveformRef.current = false; }}
            onPointerDown={startWaveformDrag}
            onPointerMove={moveWaveformDrag}
            onPointerUp={stopWaveformDrag}
            ref={waveformRef}
            role="slider"
            tabIndex={0}
          />
          {waveformState === "loading" && <span className="waveform-status">正在解码波形…</span>}
          {waveformState === "fallback" && <span className="waveform-status">波形不可用，可使用下方原生播放器</span>}
        </div>
      </section>
      <section aria-label="吸顶录音播放控制" className="audio-transport-dock audio-console--sticky">
        <div className="transport">
          <div className="time-readout mono">
            <strong>{formatTime(currentMs, true)}</strong>
            <span>/ {formatTime(duration, true)}</span>
          </div>
          <div className="transport-buttons">
            <button aria-label="后退 10 秒" onClick={() => jump(-10)} type="button">−10</button>
            <audio aria-label="录音播放器" controls preload="metadata" ref={audioRef} src={mediaUrl} />
            <button aria-label="前进 10 秒" onClick={() => jump(10)} type="button">+10</button>
          </div>
          <label className="speed-control">
            <span>倍速</span>
            <select onChange={(event) => changeRate(Number(event.target.value))} value={rate}>
              {[0.75, 1, 1.25, 1.5, 2].map((value) => <option key={value} value={value}>{value}×</option>)}
            </select>
          </label>
        </div>
      </section>
    </>
  );
});
