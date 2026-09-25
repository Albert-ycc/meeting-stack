import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { useTheme } from "../../theme";
import { mix, readToken, sampleSmooth, threshold, type Rgb } from "./dither";
import "./dither.css";

/** 面积图的绘图内边距，hover 反查数据点时要用同一套数值。 */
const AREA_PAD = { left: 40, right: 16, top: 14, bottomWithLabels: 24, bottomBare: 10 };

/** 容器宽度自适应：canvas 要按真实像素画，缩放会把网点糊掉。 */
function useWidth<T extends HTMLElement>(ref: React.RefObject<T | null>): number {
  const [w, setW] = useState(0);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const sync = () => setW(el.clientWidth);
    sync();
    if (typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(sync);
    ro.observe(el);
    return () => ro.disconnect();
  }, [ref]);
  return w;
}

function put(d: Uint8ClampedArray, i: number, c: Rgb, a: number) {
  d[i] = c[0];
  d[i + 1] = c[1];
  d[i + 2] = c[2];
  d[i + 3] = a;
}

interface AreaHover {
  index: number;
  x: number;
}

interface DitherAreaProps {
  /** 每个刻度的数值，等距分布 */
  values: readonly number[];
  /** y 轴上限，不传则取数据最大值上浮 12% */
  max?: number;
  height?: number;
  /** x 轴标签，均分放置 */
  labels?: readonly string[];
  /** y 轴刻度值 */
  ticks?: readonly number[];
  /** 每个数据点对应的说明（如日期），hover 时显示 */
  pointLabels?: readonly string[];
  /** 数值单位，hover 时跟在数字后面 */
  unit?: string;
  /** hover 读数的取整位数 */
  precision?: number;
}

/** 抖动面积图：顶部边缘网点密、往下逐渐稀疏，做出体积感。hover 可读出当天数值。 */
export function DitherArea({
  values,
  max,
  height = 210,
  labels = [],
  ticks,
  pointLabels,
  unit = "",
  precision = 0,
}: DitherAreaProps) {
  const hostRef = useRef<HTMLDivElement>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const theme = useTheme().resolved;
  const w = useWidth(hostRef);
  const [hover, setHover] = useState<AreaHover | null>(null);

  const geom = useMemo(() => {
    const padB = labels.length ? AREA_PAD.bottomWithLabels : AREA_PAD.bottomBare;
    return {
      padL: AREA_PAD.left,
      padR: AREA_PAD.right,
      padT: AREA_PAD.top,
      padB,
      plotW: Math.max(1, w - AREA_PAD.left - AREA_PAD.right),
      plotH: Math.max(1, height - AREA_PAD.top - padB),
      ceiling: max ?? Math.max(...values, 1) * 1.12,
    };
  }, [w, height, labels.length, max, values]);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas || w <= 0) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;

    canvas.width = w;
    canvas.height = height;
    const ink = readToken("--ink", [237, 237, 237]);
    const faint = readToken("--faint", [90, 90, 90]);
    const { padL, padR, padT, padB, plotW, plotH, ceiling } = geom;
    const img = ctx.createImageData(w, height);

    for (let y = 0; y < height; y++) {
      for (let x = 0; x < w; x++) {
        const i = (y * w + x) * 4;
        if (x < padL || x > w - padR || y < padT || y > height - padB) {
          put(img.data, i, ink, 0);
          continue;
        }
        const v = sampleSmooth(values, (x - padL) / plotW);
        const topY = padT + plotH * (1 - v / ceiling);
        if (y < topY) {
          put(img.data, i, ink, 0);
          continue;
        }
        const depth = (y - topY) / Math.max(1, height - padB - topY);
        const intensity = 0.16 + 0.84 * Math.pow(1 - depth, 1.5);
        if (intensity > threshold(x, y)) {
          put(img.data, i, mix(faint, ink, 0.75 + 0.25 * (1 - depth)), 255);
        } else {
          put(img.data, i, ink, 0);
        }
      }
    }
    ctx.putImageData(img, 0, 0);

    const grid = ticks ?? [0, Math.round(ceiling / 3), Math.round((ceiling / 3) * 2), Math.round(ceiling)];
    ctx.strokeStyle = `rgba(${ink.join(",")},.07)`;
    ctx.fillStyle = `rgb(${faint.join(",")})`;
    ctx.font = '10px "SFMono-Regular", monospace';
    ctx.lineWidth = 1;
    for (const t of grid) {
      const y = Math.round(padT + plotH * (1 - t / ceiling)) + 0.5;
      ctx.beginPath();
      ctx.moveTo(padL, y);
      ctx.lineTo(w - padR, y);
      ctx.stroke();
      ctx.fillText(String(t), 10, y + 3.5);
    }
    labels.forEach((label, k) => {
      const x = padL + (plotW * k) / Math.max(1, labels.length - 1);
      ctx.fillText(label, k === labels.length - 1 ? x - 26 : x, height - 6);
    });
  }, [values, height, labels, ticks, w, geom, theme]);

  const onMove = useCallback(
    (event: React.PointerEvent<HTMLDivElement>) => {
      const host = hostRef.current;
      if (!host || values.length === 0) return;
      const rect = host.getBoundingClientRect();
      const x = event.clientX - rect.left;
      const { padL, plotW } = geom;
      if (x < padL || x > padL + plotW) {
        setHover(null);
        return;
      }
      const index = Math.round(((x - padL) / plotW) * (values.length - 1));
      setHover({ index, x: padL + (plotW * index) / Math.max(1, values.length - 1) });
    },
    [geom, values.length],
  );

  const hoverValue = hover ? values[hover.index] ?? 0 : 0;
  const { padT, plotH, ceiling } = geom;
  const markerY = hover ? padT + plotH * (1 - hoverValue / ceiling) : 0;
  // 气泡高约 46px，压在标记点上就看不清指的是哪个点了；顶部空间不够时翻到下方
  const tipAbove = markerY > 62;
  const tipTop = tipAbove ? markerY - 58 : markerY + 16;

  return (
    <div
      className="dither-host dither-host--interactive"
      onPointerLeave={() => setHover(null)}
      onPointerMove={onMove}
      ref={hostRef}
      style={{ height }}
    >
      <canvas className="dither-canvas" ref={canvasRef} />
      {hover && (
        <>
          <span className="dither-crosshair" style={{ left: hover.x, top: padT, height: plotH }} />
          <span className="dither-marker" style={{ left: hover.x, top: markerY }} />
          <span
            className={`dither-tip${hover.x > geom.padL + geom.plotW * 0.66 ? " dither-tip--flip" : ""}`}
            style={{ left: hover.x, top: tipTop }}
          >
            <strong>{hoverValue.toFixed(precision)}{unit}</strong>
            {pointLabels?.[hover.index] && <em>{pointLabels[hover.index]}</em>}
          </span>
        </>
      )}
    </div>
  );
}

export interface DonutSegment {
  name: string;
  value: number;
  /** signal 用声档橙高亮，其余按传入顺序由亮到暗排布 */
  tone?: "signal";
}

interface DitherDonutProps {
  segments: readonly DonutSegment[];
  size?: number;
  /** 圆心标注 */
  centerValue?: string;
  centerLabel?: string;
}

/** 抖动甜甜圈：扇区靠网点密度区分，环内侧密外侧疏。 */
export function DitherDonut({ segments, size = 176, centerValue, centerLabel }: DitherDonutProps) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const theme = useTheme().resolved;

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    canvas.width = size;
    canvas.height = size;

    const signal = readToken("--signal", [240, 120, 59]);
    const muted2 = readToken("--muted-2", [118, 118, 118]);
    const ink = readToken("--ink", [237, 237, 237]);
    const heading = readToken("--heading", [255, 255, 255]);
    const total = segments.reduce((s, x) => s + x.value, 0) || 1;
    const cx = size / 2;
    const cy = size / 2;
    const rOut = size / 2 - 16;
    const rIn = rOut * 0.58;

    const ordinary = segments.filter((s) => s.tone !== "signal").length;
    let ordIdx = -1;
    let acc = -Math.PI / 2;
    const bounds = segments.map((s) => {
      const a0 = acc;
      acc += (s.value / total) * Math.PI * 2;
      if (s.tone === "signal") return { a0, a1: acc, color: signal, weight: 1 };
      ordIdx += 1;
      const step = ordinary > 1 ? ordIdx / (ordinary - 1) : 0;
      return { a0, a1: acc, color: mix(muted2, ink, 1 - 0.8 * step), weight: 0.95 - 0.5 * step };
    });

    const img = ctx.createImageData(size, size);
    for (let y = 0; y < size; y++) {
      for (let x = 0; x < size; x++) {
        const i = (y * size + x) * 4;
        const dx = x - cx;
        const dy = y - cy;
        const d = Math.hypot(dx, dy);
        if (d > rOut || d < rIn) {
          put(img.data, i, muted2, 0);
          continue;
        }
        let a = Math.atan2(dy, dx);
        if (a < -Math.PI / 2) a += Math.PI * 2;
        const seg = bounds.find((s) => a >= s.a0 && a < s.a1) ?? bounds[bounds.length - 1];
        const rt = (d - rIn) / (rOut - rIn);
        const intensity = seg.weight * (0.45 + 0.55 * Math.pow(1 - rt, 0.9));
        if (intensity > threshold(x, y)) put(img.data, i, seg.color, 255);
        else put(img.data, i, muted2, 0);
      }
    }
    ctx.putImageData(img, 0, 0);

    if (centerValue) {
      ctx.textAlign = "center";
      ctx.fillStyle = `rgb(${heading.join(",")})`;
      ctx.font = '300 26px "Outfit", "PingFang SC", sans-serif';
      ctx.fillText(centerValue, cx, cy + 3);
    }
    if (centerLabel) {
      ctx.textAlign = "center";
      ctx.fillStyle = `rgb(${muted2.join(",")})`;
      ctx.font = '10px "SFMono-Regular", monospace';
      ctx.fillText(centerLabel, cx, cy + 19);
    }
  }, [segments, size, centerValue, centerLabel, theme]);

  return <canvas className="dither-canvas" ref={canvasRef} />;
}

export interface CalendarCell {
  /** 0~1 的强度，决定网点密度 */
  intensity: number;
  /** hover 时显示的日期 */
  label?: string;
  /** hover 时显示的原始计数 */
  count?: number;
}

interface DitherCalendarProps {
  /** 按列优先排布（一列 = 一周） */
  cells: readonly CalendarCell[];
  cols?: number;
  rows?: number;
  cell?: number;
  gap?: number;
  /** 计数的单位词，如「场」 */
  unit?: string;
}

/** 抖动日历热力图：格子内部也是网点，最高频的日子用声档橙。hover 可读出当天场次。 */
export function DitherCalendar({
  cells,
  cols = 30,
  rows = 7,
  cell = 11,
  gap = 3,
  unit = "",
}: DitherCalendarProps) {
  const hostRef = useRef<HTMLDivElement>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const theme = useTheme().resolved;
  const [hoverIndex, setHoverIndex] = useState<number | null>(null);
  const W = cols * (cell + gap);
  const H = rows * (cell + gap);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    canvas.width = W;
    canvas.height = H;

    const signal = readToken("--signal", [240, 120, 59]);
    const ink = readToken("--ink", [237, 237, 237]);
    const muted2 = readToken("--muted-2", [118, 118, 118]);
    const img = ctx.createImageData(W, H);
    for (let y = 0; y < H; y++) {
      for (let x = 0; x < W; x++) {
        const i = (y * W + x) * 4;
        const c = Math.floor(x / (cell + gap));
        const r = Math.floor(y / (cell + gap));
        const ix = x % (cell + gap);
        const iy = y % (cell + gap);
        if (ix >= cell || iy >= cell || c >= cols || r >= rows) {
          put(img.data, i, signal, 0);
          continue;
        }
        const v = cells[c * rows + r]?.intensity ?? 0;
        if (v < 0.06) {
          put(img.data, i, ink, 13);
          continue;
        }
        if (0.12 + 0.88 * v > threshold(x, y)) {
          if (v > 0.82) put(img.data, i, signal, 255);
          else {
            put(img.data, i, mix(muted2, ink, v), 255);
          }
        } else {
          put(img.data, i, signal, 0);
        }
      }
    }
    ctx.putImageData(img, 0, 0);
  }, [cells, cols, rows, cell, gap, W, H, theme]);

  const onMove = (event: React.PointerEvent<HTMLDivElement>) => {
    const host = hostRef.current;
    if (!host) return;
    const rect = host.getBoundingClientRect();
    const x = event.clientX - rect.left;
    const y = event.clientY - rect.top;
    const c = Math.floor(x / (cell + gap));
    const r = Math.floor(y / (cell + gap));
    if (c < 0 || c >= cols || r < 0 || r >= rows) {
      setHoverIndex(null);
      return;
    }
    // 落在格间空隙上时不切换，避免读数抖动
    if (x % (cell + gap) >= cell || y % (cell + gap) >= cell) return;
    setHoverIndex(c * rows + r);
  };

  const active = hoverIndex === null ? null : cells[hoverIndex];
  const hoverCol = hoverIndex === null ? 0 : Math.floor(hoverIndex / rows);
  const hoverRow = hoverIndex === null ? 0 : hoverIndex % rows;
  const cellTop = hoverRow * (cell + gap);
  // 上方放不下（前两行）就翻到格子下方，免得气泡把被指的格子盖住
  const tipTop = cellTop > 54 ? cellTop - 54 : cellTop + cell + 10;

  return (
    <div
      className="dither-host dither-host--fit dither-host--interactive"
      onPointerLeave={() => setHoverIndex(null)}
      onPointerMove={onMove}
      ref={hostRef}
      style={{ width: W, height: H }}
    >
      <canvas className="dither-canvas" ref={canvasRef} />
      {active && (
        <>
          <span
            className="dither-cell-ring"
            style={{
              left: hoverCol * (cell + gap) - 2,
              top: hoverRow * (cell + gap) - 2,
              width: cell + 4,
              height: cell + 4,
            }}
          />
          <span
            className={`dither-tip${hoverCol > cols * 0.66 ? " dither-tip--flip" : ""}`}
            style={{ left: hoverCol * (cell + gap) + cell / 2, top: tipTop }}
          >
            <strong>
              {active.count ?? 0}
              {unit}
            </strong>
            {active.label && <em>{active.label}</em>}
          </span>
        </>
      )}
    </div>
  );
}
