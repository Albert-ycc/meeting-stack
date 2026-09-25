/**
 * Bayer 8×8 有序抖动（ordered dithering）。
 *
 * 用网点密度表达数值，全程只用灰阶 + 声档橙，不引入任何新配色。
 * 半调网点本身是印刷制版语言，和「会议档案」的气质同源。
 */
export const BAYER8: readonly (readonly number[])[] = [
  [0, 32, 8, 40, 2, 34, 10, 42],
  [48, 16, 56, 24, 50, 18, 58, 26],
  [12, 44, 4, 36, 14, 46, 6, 38],
  [60, 28, 52, 20, 62, 30, 54, 22],
  [3, 35, 11, 43, 1, 33, 9, 41],
  [51, 19, 59, 27, 49, 17, 57, 25],
  [15, 47, 7, 39, 13, 45, 5, 37],
  [63, 31, 55, 23, 61, 29, 53, 21],
];

/** 某像素位置的抖动阈值（0~1）。intensity 高于它才落点。 */
export function threshold(x: number, y: number): number {
  return (BAYER8[y & 7][x & 7] + 0.5) / 64;
}

export type Rgb = [number, number, number];

/** 读 :root 上的颜色 token，解析成 canvas 能用的 RGB。主题换了图表跟着换。 */
export function readToken(name: string, fallback: Rgb): Rgb {
  if (typeof window === "undefined") return fallback;
  const raw = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  if (!raw) return fallback;
  const hex = raw.match(/^#([0-9a-f]{3}|[0-9a-f]{6})$/i);
  if (hex) {
    const h = hex[1].length === 3 ? hex[1].replace(/./g, (c) => c + c) : hex[1];
    return [parseInt(h.slice(0, 2), 16), parseInt(h.slice(2, 4), 16), parseInt(h.slice(4, 6), 16)];
  }
  const rgb = raw.match(/(\d+)\s*,\s*(\d+)\s*,\s*(\d+)/);
  if (rgb) return [+rgb[1], +rgb[2], +rgb[3]];
  return fallback;
}

/** 平滑插值取样，让稀疏数据点连成连续曲线。 */
export function sampleSmooth(values: readonly number[], t: number): number {
  if (values.length === 0) return 0;
  const pos = t * (values.length - 1);
  const i = Math.floor(pos);
  const f = pos - i;
  const a = values[Math.max(0, Math.min(values.length - 1, i))];
  const b = values[Math.max(0, Math.min(values.length - 1, i + 1))];
  return a + (b - a) * (f * f * (3 - 2 * f));
}
