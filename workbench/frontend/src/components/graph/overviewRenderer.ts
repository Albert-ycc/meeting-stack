// 全部项目星图的 Canvas2D 画法：盘面粒子场、点线轨道、光点行星（深色辉光 + 衍射星芒，浅色印刷星图）、
// 太阳、港湾与文件夹的小盘、比例尺。只管画，不管状态：每个函数拿到这一帧算好的屏幕坐标和主题色。
//
// 颜色一律从主题 token 读（readStarTheme），换主题时重读。光照类的固定白、黑是唯一例外：
// 星点核心的白光、深色舞台四周的暗角，它们是「光」，不随主题换。

import { mix, type Rgb } from "../charts/dither";
import type { IslandNode, OverviewRing } from "./layoutOverview";
import type { OverviewHarbour } from "./overviewTypes";
import { PLATE_R, planetRadius, seededRandom } from "./layoutOverview";
import { TAU, project, type StarView } from "./overviewProjection";

// 光照：星点核心的白、暗角的黑（不随主题换）
const WHITE: Rgb = [255, 255, 255];
const BLACK: Rgb = [0, 0, 0];

export interface StarTheme {
  dark: boolean;
  stage: Rgb;
  ink: Rgb;
  muted: Rgb;
  signal: Rgb;
  /** 半透明叠色的基色：深色叠白、浅色叠黑 */
  fg: Rgb;
}

/** 读 #rgb、#rrggbb、rgb()/rgba()（逗号或空格分隔）和裸的「r g b」三元组 */
export function parseColor(raw: string): Rgb | null {
  const text = raw.trim();
  const hex = /^#([0-9a-f]{3}|[0-9a-f]{6})$/i.exec(text);
  if (hex) {
    const h = hex[1].length === 3 ? hex[1].replace(/./g, (c) => c + c) : hex[1];
    return [parseInt(h.slice(0, 2), 16), parseInt(h.slice(2, 4), 16), parseInt(h.slice(4, 6), 16)];
  }
  const nums = text.match(/-?\d+(\.\d+)?/g);
  if (nums && nums.length >= 3) return [Number(nums[0]), Number(nums[1]), Number(nums[2])];
  return null;
}

/** 主题 token 换成 canvas 用的 RGB；读不到时（还没挂样式表）按深色主题的值兜底 */
export function readStarTheme(): StarTheme {
  const root = document.documentElement;
  const style = getComputedStyle(root);
  const read = (name: string, fallback: Rgb) => parseColor(style.getPropertyValue(name)) ?? fallback;
  return {
    dark: root.dataset.theme !== "light",
    stage: read("--stage", [19, 19, 19]),
    ink: read("--ink", [237, 237, 237]),
    muted: read("--muted", [154, 154, 154]),
    signal: read("--signal", [240, 120, 59]),
    fg: read("--fg-rgb", [255, 255, 255]),
  };
}

export const rgba = (c: Rgb, a = 1) => `rgba(${c[0]},${c[1]},${c[2]},${a})`;

// ------------------------------------------------------------------ 这一帧要画的东西

/** 一颗行星（或带子里的一颗粒子）在这一帧的样子 */
export interface FrameBody {
  node: IslandNode;
  color: Rgb;
  /** 本体中心（抬起后） */
  x: number;
  y: number;
  /** 没抬起时的位置：命中按它算，悬停抬起时不会从鼠标下溜走 */
  hx: number;
  hy: number;
  /** 落在盘面上的点 */
  gx: number;
  gy: number;
  /** 外半径（星芒的一半左右）、核心半径 */
  rs: number;
  core: number;
  k: number;
  zr: number;
  d: number;
  /** 0 在正后方、1 在正前方 */
  f: number;
  /** 动画中的大小和场次（换时间窗时从旧值过渡过来） */
  r: number;
  m: number;
  lift: number;
}

export interface FrameState {
  hover: IslandNode | null;
  peekId: string | null;
  /** 「在图上找」命中的；null 表示没在找 */
  found: Set<string> | null;
  entering: IslandNode | null;
  /** 飞入的进度：0 平时，1 完全聚焦到一个项目上 */
  focusFade: number;
  hoverDock: boolean;
}

// ------------------------------------------------------------------ 尺寸

/** 光点核心与外半径：亮度和大小表示场次，外半径大约到星芒的一半 */
export function glyphSize(r: number, k: number): { core: number; outer: number } {
  const core = (1.2 + 0.32 * r) * k;
  return { core, outer: core * 1.9 + 2 };
}

/** 小行星带的粒子：比行星小一个量级 */
export function beltSize(m: number, k: number, fit: number): { core: number; outer: number } {
  const core = Math.max(1.2, (1.4 + 0.5 * Math.sqrt(Math.max(0, m))) * Math.min(1.8, k / fit));
  return { core, outer: core + 3 };
}

/** 星芒长度：窗口内没开会的没有星芒 */
function spikeLength(r: number, m: number, k: number): number {
  return m > 0.01 ? r * 1.9 * k * Math.min(1, m) : 0;
}

// ------------------------------------------------------------------ 舞台底纹

/** 盘面上的粒子：越靠近太阳越密（固定种子，每次一样） */
const PARTICLES = (() => {
  const random = seededRandom(23);
  const out: Array<[number, number, number, number]> = [];
  for (let i = 0; i < 1500; i += 1) {
    const rr = (0.1 + 0.9 * Math.pow(random(), 0.8)) * PLATE_R;
    const t = random() * TAU;
    out.push([rr * Math.cos(t), rr * Math.sin(t), random(), Math.floor(random() * 4)]);
  }
  return out;
})();

/** 深色舞台底上的远星（按舞台比例撒） */
const FAR_STARS = (() => {
  const random = seededRandom(5);
  const out: Array<[number, number, number, number]> = [];
  for (let i = 0; i < 150; i += 1) out.push([random(), random(), 0.35 + random() * 0.75, 0.12 + random() * 0.4]);
  return out;
})();

export interface BackdropCache {
  key: string;
  canvas: HTMLCanvasElement | null;
}

/**
 * 舞台底纹（缓存成一张图）：深色是 24px 点阵 + 远星，浅色是工程制图的细网格（24px 细线、120px 粗线）；
 * 四角定位角标。再叠一层太阳的暖光。
 */
export function drawBackdrop(
  ctx: CanvasRenderingContext2D,
  cache: BackdropCache,
  width: number,
  height: number,
  dpr: number,
  theme: StarTheme,
  sun: { x: number; y: number },
) {
  const key = [width, height, dpr, theme.dark, theme.fg.join()].join("|");
  if (cache.key !== key) {
    cache.key = key;
    const canvas = document.createElement("canvas");
    canvas.width = Math.round(width * dpr);
    canvas.height = Math.round(height * dpr);
    const c = canvas.getContext("2d");
    cache.canvas = c ? canvas : null;
    if (c) {
      c.setTransform(dpr, 0, 0, dpr, 0, 0);
      if (theme.dark) {
        c.fillStyle = rgba(theme.fg, 0.05);
        for (let x = 12; x < width; x += 24) for (let y = 12; y < height; y += 24) c.fillRect(x - 0.6, y - 0.6, 1.2, 1.2);
        for (const [u, v, r, a] of FAR_STARS) {
          c.fillStyle = rgba(theme.fg, a * 0.5);
          c.beginPath();
          c.arc(u * width, v * height, r * 0.7, 0, TAU);
          c.fill();
        }
      } else {
        c.lineWidth = 1;
        for (let x = 24; x < width; x += 24) {
          c.strokeStyle = rgba(theme.fg, x % 120 === 0 ? 0.06 : 0.028);
          c.beginPath();
          c.moveTo(x + 0.5, 0);
          c.lineTo(x + 0.5, height);
          c.stroke();
        }
        for (let y = 24; y < height; y += 24) {
          c.strokeStyle = rgba(theme.fg, y % 120 === 0 ? 0.06 : 0.028);
          c.beginPath();
          c.moveTo(0, y + 0.5);
          c.lineTo(width, y + 0.5);
          c.stroke();
        }
      }
      c.strokeStyle = rgba(theme.fg, theme.dark ? 0.32 : 0.42);
      c.lineWidth = 1;
      const m = 9;
      const L = 14;
      for (const [x, y, sx, sy] of [
        [m, m, 1, 1],
        [width - m, m, -1, 1],
        [m, height - m, 1, -1],
        [width - m, height - m, -1, -1],
      ]) {
        c.beginPath();
        c.moveTo(x + 0.5 * sx, y + L * sy);
        c.lineTo(x + 0.5 * sx, y + 0.5 * sy);
        c.lineTo(x + L * sx, y + 0.5 * sy);
        c.stroke();
      }
    }
  }
  if (cache.canvas) ctx.drawImage(cache.canvas, 0, 0, width, height);
  const glow = ctx.createRadialGradient(sun.x, sun.y, 0, sun.x, sun.y, Math.max(width, height) * 0.5);
  glow.addColorStop(0, rgba(theme.signal, theme.dark ? 0.075 : 0.04));
  glow.addColorStop(1, rgba(theme.signal, 0));
  ctx.fillStyle = glow;
  ctx.fillRect(0, 0, width, height);
}

/** 盘面：盘沿前实后虚，盘上是一层越近太阳越密的粒子 */
export function drawPlane(ctx: CanvasRenderingContext2D, view: StarView, theme: StarTheme, focusFade: number, width: number, height: number) {
  const fade = 1 - focusFade * 0.7;
  ctx.lineWidth = 1;
  let prev = project(view, PLATE_R, 0, 0);
  for (let i = 1; i <= 120; i += 1) {
    const t = (i / 120) * TAU;
    const q = project(view, PLATE_R * Math.cos(t), 0, PLATE_R * Math.sin(t));
    const front = Math.max(0, (prev.zr + q.zr) / 2 / PLATE_R);
    ctx.strokeStyle = rgba(theme.fg, ((theme.dark ? 0.05 : 0.08) + front * 0.11) * fade);
    ctx.beginPath();
    ctx.moveTo(prev.x, prev.y);
    ctx.lineTo(q.x, q.y);
    ctx.stroke();
    prev = q;
  }
  if (theme.dark) ctx.globalCompositeOperation = "lighter";
  const buckets: Array<Array<[number, number, number]>> = [[], [], [], []];
  for (const [px, pz, size, bucket] of PARTICLES) {
    const q = project(view, px, 0, pz);
    if (q.x < -2 || q.x > width + 2 || q.y < -2 || q.y > height + 2) continue;
    buckets[bucket].push([q.x, q.y, (0.5 + size * 0.9) * Math.min(1.5, q.k / view.fit)]);
  }
  buckets.forEach((list, i) => {
    ctx.fillStyle = rgba(theme.fg, (theme.dark ? 0.07 + i * 0.05 : 0.06 + i * 0.035) * fade);
    ctx.beginPath();
    for (const [x, y, s] of list) ctx.rect(x - s / 2, y - s / 2, s, s);
    ctx.fill();
  });
  ctx.globalCompositeOperation = "source-over";
}

/**
 * 轨道是点线，亮暗是这一圈的时间顺序：圈头（最新）最亮，往正后方逐渐变暗；悬停的那一圈换成声档橙。
 * 最外圈每 12° 一个刻度。pass 是先画后半圈（在太阳后面）还是前半圈。
 */
export function drawOrbits(
  ctx: CanvasRenderingContext2D,
  view: StarView,
  theme: StarTheme,
  rings: OverviewRing[],
  pass: "back" | "front",
  state: FrameState,
) {
  const N = 240;
  for (const ring of rings) {
    const R = ring.R;
    const hot = state.hover !== null && state.hover.ring === ring.index;
    const dimOthers = state.hover !== null && !hot;
    let prev = project(view, R, 0, 0);
    for (let i = 1; i <= N; i += 1) {
      const t = (i / N) * TAU;
      const tm = ((i - 0.5) / N) * TAU;
      const q = project(view, R * Math.cos(t), 0, R * Math.sin(t));
      const midZr = (prev.zr + q.zr) / 2;
      if ((pass === "back") === midZr < 0) {
        let dth = Math.abs((((tm - ring.head) % TAU) + TAU) % TAU);
        if (dth > Math.PI) dth = TAU - dth;
        const recent = Math.pow((1 + Math.cos(dth)) / 2, 1.25);
        const depth = 0.72 + 0.28 * Math.min(1, Math.max(0, (midZr / R + 1) / 2));
        let alpha = (theme.dark ? 0.1 + 0.42 * recent : 0.12 + 0.42 * recent) * depth;
        let color = theme.fg;
        if (hot) {
          alpha = 0.5 + 0.45 * recent;
          color = theme.signal;
        } else if (dimOthers) alpha *= 0.5;
        if (ring.belt && !hot) alpha *= 0.45;
        if (state.found) alpha *= 0.5;
        alpha *= 1 - state.focusFade * 0.85;
        const lw = Math.max(0.6, (hot ? 1.5 : 1) * Math.min(1.3, (prev.k + q.k) / 2 / view.fit));
        if (i % 2 === 0) {
          const s = lw * 1.2;
          ctx.fillStyle = rgba(color, Math.min(1, alpha * 1.5));
          ctx.fillRect(q.x - s / 2, q.y - s / 2, s, s);
        }
        if (ring.index === 2 && i % 8 === 0) {
          const major = i % 20 === 0;
          const len = major ? 9 : 4.5;
          const o = project(view, (R + len) * Math.cos(t), 0, (R + len) * Math.sin(t));
          ctx.strokeStyle = rgba(color, alpha * (major ? 0.95 : 0.65));
          ctx.lineWidth = 1;
          ctx.beginPath();
          ctx.moveTo(q.x, q.y);
          ctx.lineTo(o.x, o.y);
          ctx.stroke();
        }
      }
      prev = q;
    }
  }
}

// ------------------------------------------------------------------ 行星

/** 一道收尖的星芒（从中心往外由亮到无） */
function taper(c: CanvasRenderingContext2D, x: number, y: number, a: number, L: number, w: number, color: Rgb, alpha: number) {
  const ex = x + Math.cos(a) * L;
  const ey = y - Math.sin(a) * L;
  const nx = (Math.sin(a) * w) / 2;
  const ny = (Math.cos(a) * w) / 2;
  const g = c.createLinearGradient(x, y, ex, ey);
  g.addColorStop(0, rgba(color, alpha));
  g.addColorStop(1, rgba(color, 0));
  c.fillStyle = g;
  c.beginPath();
  c.moveTo(x + nx, y + ny);
  c.lineTo(ex, ey);
  c.lineTo(x - nx, y - ny);
  c.closePath();
  c.fill();
}

/** 6 道主星芒（竖直与 ±30°）+ 2 道短横芒：韦布望远镜的衍射纹样 */
const SPIKES = [90, 30, -30, -90, -150, 150].map((d) => (d * Math.PI) / 180);

function drawSpikes(c: CanvasRenderingContext2D, x: number, y: number, L: number, w: number, color: Rgb, alpha: number) {
  for (const a of SPIKES) taper(c, x, y, a, L, w, color, alpha);
  for (const a of [0, Math.PI]) taper(c, x, y, a, L * 0.42, w * 0.7, color, alpha * 0.7);
}

/**
 * 一颗光点。深色：克制的辉光 + 星芒 + 白核（'lighter' 叠加）；浅色：印刷星图——细墨线星芒、白色挖空环、项目色实心点。
 * 从没开过会的只是一个空心小圈。
 */
function drawStarGlyph(
  c: CanvasRenderingContext2D,
  theme: StarTheme,
  x: number,
  y: number,
  rc: number,
  color: Rgb,
  hot: boolean,
  never: boolean,
  L: number,
) {
  if (never) {
    c.strokeStyle = rgba(theme.ink, theme.dark ? 0.55 : 0.62);
    c.lineWidth = 1;
    c.beginPath();
    c.arc(x, y, Math.max(2.6, rc * 0.9), 0, TAU);
    c.stroke();
    return;
  }
  if (theme.dark) {
    c.globalCompositeOperation = "lighter";
    const haloColor = mix(color, WHITE, 0.45);
    const halo = rc * 3.4;
    const g = c.createRadialGradient(x, y, 0, x, y, halo);
    g.addColorStop(0, rgba(haloColor, 0.34));
    g.addColorStop(0.35, rgba(haloColor, 0.09));
    g.addColorStop(1, rgba(haloColor, 0));
    c.fillStyle = g;
    c.beginPath();
    c.arc(x, y, halo, 0, TAU);
    c.fill();
    if (L > 0) drawSpikes(c, x, y, L, Math.max(1, rc * 0.42), mix(color, WHITE, 0.62), 0.6);
    const core = c.createRadialGradient(x, y, 0, x, y, rc);
    core.addColorStop(0, rgba(WHITE, 0.95));
    core.addColorStop(0.55, rgba(mix(color, WHITE, 0.55), 0.85));
    core.addColorStop(1, rgba(color, 0));
    c.fillStyle = core;
    c.beginPath();
    c.arc(x, y, rc, 0, TAU);
    c.fill();
    c.globalCompositeOperation = "source-over";
  } else {
    if (L > 0) {
      c.lineWidth = 0.75;
      c.strokeStyle = rgba(theme.fg, 0.5);
      for (const a of SPIKES) {
        c.beginPath();
        c.moveTo(x, y);
        c.lineTo(x + Math.cos(a) * L * 0.78, y - Math.sin(a) * L * 0.78);
        c.stroke();
      }
      c.strokeStyle = rgba(theme.fg, 0.32);
      for (const a of [0, Math.PI]) {
        c.beginPath();
        c.moveTo(x, y);
        c.lineTo(x + Math.cos(a) * L * 0.34, y);
        c.stroke();
      }
    }
    c.fillStyle = rgba(theme.stage, 1);
    c.beginPath();
    c.arc(x, y, rc + 2.2, 0, TAU);
    c.fill();
    c.fillStyle = rgba(mix(color, BLACK, 0.18));
    c.beginPath();
    c.arc(x, y, rc, 0, TAU);
    c.fill();
  }
  if (hot) {
    c.strokeStyle = rgba(theme.signal, 1);
    c.lineWidth = 1.3;
    c.beginPath();
    c.arc(x, y, rc + 4.5, 0, TAU);
    c.stroke();
  }
}

/** 悬停、找到时的四角括号（目标锁定） */
function drawBrackets(c: CanvasRenderingContext2D, theme: StarTheme, x: number, y: number, h: number, alpha: number) {
  const a = Math.max(4, Math.min(9, h * 0.45));
  c.strokeStyle = rgba(theme.signal, alpha);
  c.lineWidth = 1.4;
  for (const [sx, sy] of [
    [-1, -1],
    [1, -1],
    [-1, 1],
    [1, 1],
  ]) {
    const cx = x + sx * h;
    const cy = y + sy * h;
    c.beginPath();
    c.moveTo(cx, cy - sy * a);
    c.lineTo(cx, cy);
    c.lineTo(cx - sx * a, cy);
    c.stroke();
  }
}

/** 飞进一个项目时，在它周围展开项目图的三圈（同样的点线） */
function drawChildRings(ctx: CanvasRenderingContext2D, view: StarView, theme: StarTheme, body: FrameBody, ringR: number, focusFade: number) {
  const { node } = body;
  const R0 = ringR + node.rj;
  const cx0 = R0 * Math.cos(node.theta);
  const cz0 = R0 * Math.sin(node.theta);
  for (let i = 0; i < 3; i += 1) {
    const rr = body.r * (2.3 + i * 1.45) * (0.6 + 0.4 * focusFade);
    const color = i === 0 ? theme.signal : theme.fg;
    const alpha = (i === 0 ? 0.6 : 0.24 - i * 0.04) * focusFade;
    ctx.fillStyle = rgba(color, alpha * 1.4);
    for (let j = 2; j <= 96; j += 2) {
      const t = (j / 96) * TAU;
      const q = project(view, cx0 + rr * Math.cos(t), 0, cz0 + rr * Math.sin(t));
      ctx.fillRect(q.x - 0.8, q.y - 0.8, 1.6, 1.6);
    }
  }
}

function bodyAlpha(body: FrameBody, state: FrameState): number {
  const { node } = body;
  let alpha = 0.72 + 0.28 * body.f;
  if (state.hover && state.hover !== node && state.hover.ring !== node.ring) alpha *= 0.55;
  if (state.entering && state.entering !== node) alpha *= 1 - state.focusFade;
  if (state.found && !state.found.has(node.id)) alpha *= 0.2;
  return alpha;
}

/** 一颗行星或带子里的一颗粒子：悬停时有一根落到盘面的垂线，锁定时有四角括号 */
export function drawBody(
  ctx: CanvasRenderingContext2D,
  view: StarView,
  theme: StarTheme,
  body: FrameBody,
  state: FrameState,
  ringR: number,
) {
  const alpha = bodyAlpha(body, state);
  if (alpha <= 0.01) return;
  const { node } = body;
  const hot = state.hover === node;
  const never = !node.data.last_day;
  ctx.save();
  ctx.globalAlpha = alpha;
  if (state.entering === node && state.focusFade > 0.02) drawChildRings(ctx, view, theme, body, ringR, state.focusFade);
  if (body.lift > 0.5) {
    ctx.strokeStyle = hot ? rgba(theme.signal, 0.85) : rgba(theme.fg, theme.dark ? 0.28 : 0.34);
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(body.gx, body.gy);
    ctx.lineTo(body.x, body.y + body.rs);
    ctx.stroke();
    const foot = hot ? 5 : 3.2;
    ctx.lineWidth = hot ? 1.2 : 1;
    ctx.beginPath();
    ctx.ellipse(body.gx, body.gy, foot, foot * Math.max(0.2, view.se), 0, 0, TAU);
    ctx.stroke();
  }
  if (node.belt) {
    const s = body.core;
    if (never) {
      ctx.strokeStyle = rgba(theme.ink, theme.dark ? 0.5 : 0.55);
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.arc(body.x, body.y, s + 0.5, 0, TAU);
      ctx.stroke();
    } else if (theme.dark) {
      ctx.globalCompositeOperation = "lighter";
      const g = ctx.createRadialGradient(body.x, body.y, 0, body.x, body.y, s * 3);
      g.addColorStop(0, rgba(mix(body.color, WHITE, 0.55), 0.55));
      g.addColorStop(1, rgba(body.color, 0));
      ctx.fillStyle = g;
      ctx.beginPath();
      ctx.arc(body.x, body.y, s * 3, 0, TAU);
      ctx.fill();
      ctx.globalCompositeOperation = "source-over";
    } else {
      ctx.fillStyle = rgba(theme.stage, 1);
      ctx.beginPath();
      ctx.arc(body.x, body.y, s + 1.4, 0, TAU);
      ctx.fill();
      ctx.fillStyle = rgba(mix(body.color, BLACK, 0.2));
      ctx.beginPath();
      ctx.arc(body.x, body.y, s, 0, TAU);
      ctx.fill();
    }
  } else {
    drawStarGlyph(ctx, theme, body.x, body.y, body.core, body.color, hot, never, spikeLength(body.r, body.m, body.k));
  }
  if (hot || state.peekId === node.id || state.found?.has(node.id)) {
    drawBrackets(ctx, theme, body.x, body.y, body.rs + (node.belt ? 4 : 6), 0.95);
  }
  ctx.restore();
}

/** 太阳：最亮的那颗星。先挖空一小块挡住它身后的轨道 */
export function drawSun(ctx: CanvasRenderingContext2D, theme: StarTheme, sun: { x: number; y: number }, focusFade: number) {
  const { x, y } = sun;
  ctx.save();
  ctx.globalAlpha = 1 - focusFade * 0.6;
  ctx.fillStyle = rgba(theme.stage, 1);
  ctx.beginPath();
  ctx.arc(x, y, 15, 0, TAU);
  ctx.fill();
  if (theme.dark) {
    ctx.globalCompositeOperation = "lighter";
    const g = ctx.createRadialGradient(x, y, 0, x, y, 72);
    g.addColorStop(0, rgba(theme.signal, 0.3));
    g.addColorStop(0.3, rgba(theme.signal, 0.08));
    g.addColorStop(1, rgba(theme.signal, 0));
    ctx.fillStyle = g;
    ctx.beginPath();
    ctx.arc(x, y, 72, 0, TAU);
    ctx.fill();
    drawSpikes(ctx, x, y, 120, 3, mix(theme.signal, WHITE, 0.45), 0.7);
    const core = ctx.createRadialGradient(x, y, 0, x, y, 10);
    core.addColorStop(0, rgba(WHITE, 1));
    core.addColorStop(0.5, rgba(mix(theme.signal, WHITE, 0.4), 0.95));
    core.addColorStop(1, rgba(theme.signal, 0));
    ctx.fillStyle = core;
    ctx.beginPath();
    ctx.arc(x, y, 10, 0, TAU);
    ctx.fill();
  } else {
    ctx.lineWidth = 0.8;
    ctx.strokeStyle = rgba(theme.fg, 0.42);
    for (const a of SPIKES) {
      ctx.beginPath();
      ctx.moveTo(x, y);
      ctx.lineTo(x + Math.cos(a) * 62, y - Math.sin(a) * 62);
      ctx.stroke();
    }
    ctx.strokeStyle = rgba(theme.fg, 0.28);
    for (const a of [0, Math.PI]) {
      ctx.beginPath();
      ctx.moveTo(x, y);
      ctx.lineTo(x + Math.cos(a) * 28, y);
      ctx.stroke();
    }
    ctx.fillStyle = rgba(theme.stage, 1);
    ctx.beginPath();
    ctx.arc(x, y, 12, 0, TAU);
    ctx.fill();
    ctx.fillStyle = rgba(theme.signal);
    ctx.beginPath();
    ctx.arc(x, y, 8.5, 0, TAU);
    ctx.fill();
    ctx.strokeStyle = rgba(theme.signal, 0.8);
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.arc(x, y, 17, 0, TAU);
    ctx.stroke();
  }
  ctx.restore();
}

// ------------------------------------------------------------------ 港湾与文件夹的小盘

export const DOCK_R = 40;
/** 两个小盘的中心：舞台左上、右上 */
export function dockCenters(width: number) {
  return { harbour: { x: 64, y: 56 }, folder: { x: width - 64, y: 56 } };
}

function drawDockDisc(ctx: CanvasRenderingContext2D, theme: StarTheme, c: { x: number; y: number }, se: number, dashed: boolean) {
  const ry = DOCK_R * Math.max(0.16, se);
  ctx.fillStyle = rgba(theme.fg, theme.dark ? 0.025 : 0.02);
  ctx.beginPath();
  ctx.ellipse(c.x, c.y, DOCK_R, ry, 0, 0, TAU);
  ctx.fill();
  ctx.strokeStyle = rgba(theme.fg, dashed ? 0.32 : 0.24);
  ctx.lineWidth = 1;
  if (dashed) ctx.setLineDash([3, 4]);
  ctx.beginPath();
  ctx.ellipse(c.x, c.y, DOCK_R, ry, 0, 0, TAU);
  ctx.stroke();
  ctx.setLineDash([]);
  // 盘心十字
  ctx.strokeStyle = rgba(theme.fg, 0.16);
  ctx.beginPath();
  ctx.moveTo(c.x - 5, c.y);
  ctx.lineTo(c.x + 5, c.y);
  ctx.moveTo(c.x, c.y - 5 * Math.max(0.2, se));
  ctx.lineTo(c.x, c.y + 5 * Math.max(0.2, se));
  ctx.stroke();
}

/** 向日葵排布的点（跟着方位角转），近的在后画 */
function sunflower(c: { x: number; y: number }, n: number, az: number, se: number) {
  const pts: Array<{ x: number; y: number; z: number; i: number }> = [];
  for (let i = 0; i < n; i += 1) {
    const rr = DOCK_R * 0.86 * Math.sqrt((i + 0.5) / n);
    const t = i * 2.39996 + (az * Math.PI) / 180;
    pts.push({ x: c.x + rr * Math.cos(t), y: c.y + rr * Math.sin(t) * Math.max(0.16, se), z: Math.sin(t), i });
  }
  return pts.sort((a, b) => a.z - b.z);
}

/**
 * 左上的港湾盘：一个点是一场没归项目的会，「等 AI 判断」的是橙色空心圈；
 * 右上的文件夹盘：一个小方块是一个还没挂的文件夹，一个都没有时是空的虚线盘。
 */
export function drawDocks(
  ctx: CanvasRenderingContext2D,
  theme: StarTheme,
  width: number,
  view: StarView,
  harbour: OverviewHarbour,
  folderCount: number,
  hoverDock: boolean,
  focusFade: number,
) {
  const alpha = 1 - focusFade;
  if (alpha <= 0.01) return;
  const c = dockCenters(width);
  ctx.save();
  ctx.globalAlpha = alpha;
  drawDockDisc(ctx, theme, c.harbour, view.se, false);
  const pending = harbour.counts.ai_pending || 0;
  const dot = hoverDock ? theme.ink : theme.muted;
  for (const q of sunflower(c.harbour, Math.min(harbour.total, 90), view.cam.az, view.se)) {
    const s = 1.9 * (1 + 0.16 * q.z);
    const y = q.y - s;
    if (q.i < pending) {
      ctx.strokeStyle = rgba(theme.signal, 1);
      ctx.lineWidth = 1.2;
      ctx.beginPath();
      ctx.arc(q.x, y, s + 0.8, 0, TAU);
      ctx.stroke();
      continue;
    }
    ctx.fillStyle = rgba(dot, 0.9);
    ctx.beginPath();
    ctx.arc(q.x, y, s * 0.85, 0, TAU);
    ctx.fill();
  }
  drawDockDisc(ctx, theme, c.folder, view.se, folderCount === 0);
  ctx.fillStyle = rgba(theme.muted, 0.9);
  for (const q of sunflower(c.folder, Math.min(folderCount, 60), view.cam.az, view.se)) {
    const s = 1.6 * (1 + 0.16 * q.z);
    ctx.fillRect(q.x - s, q.y - 2 * s, s * 2, s * 2);
  }
  ctx.restore();
}

/** 深色舞台四周压一圈暗角，把视线收向中间（浅色不用） */
export function drawVignette(ctx: CanvasRenderingContext2D, theme: StarTheme, width: number, height: number) {
  if (!theme.dark) return;
  const g = ctx.createRadialGradient(
    width / 2,
    height * 0.48,
    Math.min(width, height) * 0.45,
    width / 2,
    height * 0.48,
    Math.hypot(width, height) * 0.62,
  );
  g.addColorStop(0, rgba(BLACK, 0));
  g.addColorStop(1, rgba(BLACK, 0.28));
  ctx.fillStyle = g;
  ctx.fillRect(0, 0, width, height);
}

/** 名字离自己的行星太远时的细引线 */
export function drawLeaders(
  ctx: CanvasRenderingContext2D,
  theme: StarTheme,
  leaders: Array<{ x: number; y: number; nx: number; ny: number; r: number }>,
) {
  if (!leaders.length) return;
  ctx.save();
  ctx.strokeStyle = rgba(theme.fg, 0.32);
  ctx.lineWidth = 1;
  for (const { x, y, nx, ny, r } of leaders) {
    const a = Math.atan2(ny - y, nx - x);
    ctx.beginPath();
    ctx.moveTo(x + Math.cos(a) * (r + 2), y + Math.sin(a) * (r + 2));
    ctx.lineTo(nx, ny);
    ctx.stroke();
  }
  ctx.restore();
}

/** 跨项目的线：两颗行星之间的虚线，两头从本体边起；lit 是悬停或选中的那颗连出去的 */
export function drawBridge(ctx: CanvasRenderingContext2D, theme: StarTheme, a: FrameBody, b: FrameBody, lit: boolean, fade: number) {
  const dx = b.x - a.x;
  const dy = b.y - a.y;
  const length = Math.hypot(dx, dy) || 1;
  const ra = a.rs + 4;
  const rb = b.rs + 4;
  if (length <= ra + rb) return;
  ctx.save();
  ctx.strokeStyle = rgba(theme.fg, (lit ? 0.6 : 0.28) * fade);
  ctx.lineWidth = lit ? 1.6 : 1.1;
  ctx.setLineDash([4, 4]);
  ctx.beginPath();
  ctx.moveTo(a.x + (dx / length) * ra, a.y + (dy / length) * ra);
  ctx.lineTo(b.x - (dx / length) * rb, b.y - (dy / length) * rb);
  ctx.stroke();
  ctx.restore();
}

// ------------------------------------------------------------------ 比例尺

/** 比例尺上的三档：1 场、最多的一半、最多 */
export function sizeKeyValues(maxMeetings: number): number[] {
  const max = Math.max(1, maxMeetings);
  return [...new Set([1, Math.max(1, Math.round(max / 2)), max])];
}

/** 右下读数块里的比例尺：用同一种光点画出三档 */
export function drawSizeKey(
  canvas: HTMLCanvasElement,
  theme: StarTheme,
  values: number[],
  color: Rgb,
  fit: number,
  dpr: number,
) {
  const w = canvas.clientWidth;
  const h = canvas.clientHeight;
  if (!w || !h) return;
  canvas.width = Math.round(w * dpr);
  canvas.height = Math.round(h * dpr);
  const c = canvas.getContext("2d");
  if (!c) return;
  c.setTransform(dpr, 0, 0, dpr, 0, 0);
  c.clearRect(0, 0, w, h);
  const k = fit * 0.82;
  values.forEach((m, i) => {
    const r = planetRadius(m);
    const x = sizeKeyX(i, values.length, w);
    drawStarGlyph(c, theme, x, h / 2, (1.2 + 0.32 * r) * k, color, false, false, r * 1.9 * k * 0.7);
  });
}

/** 比例尺第 i 档的横坐标（数字标在同一个位置下面） */
export function sizeKeyX(i: number, count: number, width: number): number {
  return count === 1 ? width / 2 : 22 + (i * (width - 44)) / (count - 1);
}

