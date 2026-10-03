// 全部项目星图的场景：镜头、补间动画、拖转惯性、悬停与命中、飞入、「在图上找」，以及每帧把名字摆到 DOM 上。
//
// React（OverviewCanvas.tsx）只渲染一次性的结构：canvas、每个项目一个名字按钮、琥珀数、圈名、读数块。
// 每帧会变的东西（名字的位置和显隐、圈名、太阳的字、读数里的方位角）在这里直接改 DOM，不走 React 重画。
// 只在有动画、惯性或拖动时跑 requestAnimationFrame，静止时一帧都不画。
// 拿不到 2D context（jsdom）时什么都不画、也不报错；舞台还没有大小时连名字都不摆，按钮保持原样，用例照样能点。

import { mix, type Rgb } from "../charts/dither";
import {
  FAR_R,
  SUN_R,
  SUN_Y,
  planetRadius,
  type IslandNode,
  type OverviewLayout,
} from "./layoutOverview";
import { FIND_NAMED_MAX, placeLabels, type LabelBody, type PlacedLabel, type Rect } from "./overviewLabels";
import type { OverviewHarbour } from "./overviewTypes";
import {
  DEG,
  VIEW0,
  clampEl,
  clampZoom,
  fitScale,
  headingDegrees,
  initialCamera,
  makeView,
  project,
  resetAzimuth,
  turnToFront,
  type StarCamera,
  type StarView,
} from "./overviewProjection";
import {
  DOCK_R,
  beltSize,
  dockCenters,
  drawBackdrop,
  drawBody,
  drawBridge,
  drawDocks,
  drawLeaders,
  drawOrbits,
  drawPlane,
  drawSizeKey,
  drawSun,
  drawVignette,
  glyphSize,
  parseColor,
  readStarTheme,
  sizeKeyValues,
  type BackdropCache,
  type FrameBody,
  type FrameState,
  type StarTheme,
} from "./overviewRenderer";

/** 悬停时光点沿垂线抬起多少（世界单位） */
const LIFT = 18;
const DRAG_AZ = 0.26;
const DRAG_EL = 0.14;
/** 超过这么多条跨项目的线就只在悬停或选中时画 */
const BRIDGE_ALWAYS_MAX = 30;
/** 换时间窗时大小过渡的时长 */
const SIZE_MS = 680;
/** 面板（OverviewPanel）盖住舞台右边这么宽，悬停卡片不往下面放 */
const PANEL_COVER = 412;
/**
 * 舞台比这窄（窗口 1280 宽以下）时，左上、右上的小盘不画，两块字各占半边，搜索框和顶部读数挪到右下：
 * 视口上设 data-narrow，排法在 OverviewGraph.css
 */
const NARROW_STAGE = 1000;

// 补间：cubic-bezier(0.16, 1, 0.3, 1)，和全站的 --exp 一样
function bezier(p1x: number, p1y: number, p2x: number, p2y: number) {
  const cx = 3 * p1x;
  const bx = 3 * (p2x - p1x) - cx;
  const ax = 1 - cx - bx;
  const cy = 3 * p1y;
  const by = 3 * (p2y - p1y) - cy;
  const ay = 1 - cy - by;
  const sx = (t: number) => ((ax * t + bx) * t + cx) * t;
  const sy = (t: number) => ((ay * t + by) * t + cy) * t;
  const dx = (t: number) => (3 * ax * t + 2 * bx) * t + cx;
  return (x: number) => {
    if (x <= 0) return 0;
    if (x >= 1) return 1;
    let t = x;
    for (let i = 0; i < 8; i += 1) {
      const e = sx(t) - x;
      const d = dx(t);
      if (Math.abs(e) < 1e-5 || Math.abs(d) < 1e-6) break;
      t -= e / d;
    }
    return sy(Math.min(1, Math.max(0, t)));
  };
}
const EASE = bezier(0.16, 1, 0.3, 1);

type Numeric = Record<string, number>;
interface Tween {
  target: Numeric;
  from: Numeric;
  to: Numeric;
  t0: number;
  ms: number;
  done?: () => void;
}

function prefersReducedMotion(): boolean {
  return typeof window.matchMedia === "function" && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

/** 每个项目随动画变的量：大小、场次（星芒长短按它）、悬停抬起 */
interface Live {
  r: number;
  m: number;
  lift: number;
}

/** 离开概览再回来时接着上次的视角（换页回来不跳回正前方） */
let savedView: { az: number; el: number; zoom: number } | null = null;

export function forgetOverviewCamera() {
  savedView = null;
}

export interface SceneElements {
  viewport: HTMLElement;
  canvas: HTMLCanvasElement;
  keyCanvas: HTMLCanvasElement | null;
  labels: Map<string, HTMLElement>;
  badges: Map<string, HTMLElement>;
  chips: Array<HTMLElement | null>;
  sunLabel: HTMLElement | null;
  bridgeLabels: Map<string, HTMLElement>;
  readout: { az: HTMLElement | null; el: HTMLElement | null; zoom: HTMLElement | null; hint: HTMLElement | null };
  card: HTMLElement | null;
  /** 读数块、搜索框、缩放条：名字不往这些地方放 */
  hud: Array<HTMLElement | null>;
}

export interface SceneCallbacks {
  onHover: (id: string | null) => void;
  onEnteringChange: (entering: boolean) => void;
  /** 飞入结束：进这个项目的项目图 */
  onEnter: (projectId: string) => void;
  /** 点空白处时关面板 */
  onSelect: (id: string | null) => void;
}

export interface SceneData {
  layout: OverviewLayout;
  harbour: OverviewHarbour;
  folderCount: number;
}

export interface FoundResult {
  count: number;
}

interface Drag {
  x: number;
  y: number;
  lx: number;
  ly: number;
  t: number;
  moved: boolean;
  id: number;
}

export class OverviewScene {
  readonly cam: StarCamera = initialCamera();
  private readonly motion = { focusFade: 0 };
  private readonly tweens: Tween[] = [];
  private velocity = { az: 0, el: 0 };
  private drag: Drag | null = null;
  private raf = 0;
  private width = 0;
  private height = 0;
  private narrow = false;
  private dpr = 1;
  private fit = 1;
  private ctx: CanvasRenderingContext2D | null = null;
  private theme: StarTheme;
  private readonly backdrop: BackdropCache = { key: "", canvas: null };
  private data: SceneData | null = null;
  private readonly live = new Map<string, Live>();
  private readonly colors = new Map<string, Rgb>();
  private hover: IslandNode | null = null;
  /** 指针停在名字或琥珀数上悬停时，那个名字或琥珀数钉在原地（行星抬起时不跟着挪，免得从指针下溜走） */
  private pin: { id: string; via: "label" | "badge"; view: string } | null = null;
  private hoverDock = false;
  private peekId: string | null = null;
  private selectedId: string | null = null;
  private panelOpen = false;
  private found: { query: string; list: IslandNode[]; set: Set<string> } | null = null;
  private entering: IslandNode | null = null;
  private beforeEnter: { az: number; el: number; zoom: number; ox: number } | null = null;
  private frame: FrameBody[] = [];
  private readonly sizes = new Map<string, { w: number; h: number }>();
  private readonly badgeWidths = new Map<string, number>();
  private chipSizes: Array<{ w: number; h: number }> = [];
  private sunLabelSize = { w: 0, h: 0 };
  private hudRects: Rect[] | null = null;
  private readonly lastKey = new Map<string, string>();
  private readonly labelRects = new Map<string, Rect>();
  /** 上一帧的名字和琥珀数摆在哪：钉住时沿用 */
  private spots = new Map<string, PlacedLabel>();
  private badgeSpots = new Map<string, { x: number; y: number }>();
  private leaders: Array<{ x: number; y: number; nx: number; ny: number; r: number }> = [];
  private sun = { x: 0, y: 0, r: 0, gx: 0, gy: 0 };
  private readonly detach: () => void;

  constructor(
    private readonly el: SceneElements,
    private readonly callbacks: SceneCallbacks,
  ) {
    if (savedView) Object.assign(this.cam, savedView);
    this.ctx = el.canvas.getContext("2d");
    this.theme = readStarTheme();
    this.detach = this.listen();
  }

  destroy() {
    if (this.raf) cancelAnimationFrame(this.raf);
    this.raf = 0;
    this.detach();
    const view = this.beforeEnter ?? this.cam;
    savedView = { az: view.az, el: view.el, zoom: view.zoom };
  }

  // ---------------------------------------------------------------- 外面喂进来的

  setData(data: SceneData) {
    const animateSizes = this.data !== null && !prefersReducedMotion();
    this.data = data;
    const ids = new Set<string>();
    for (const node of data.layout.islands) {
      ids.add(node.id);
      this.colors.set(node.id, parseColor(node.data.color) ?? this.theme.muted);
      const target = { r: planetRadius(node.data.meetings), m: node.data.meetings };
      const current = this.live.get(node.id);
      if (!current) this.live.set(node.id, { ...target, lift: 0 });
      else if (current.r !== target.r || current.m !== target.m) {
        if (animateSizes) this.animate(current as unknown as Numeric, target, SIZE_MS);
        else Object.assign(current, target);
      }
    }
    for (const id of [...this.live.keys()]) if (!ids.has(id)) this.live.delete(id);
    // 每次取数都是新对象：悬停、飞入中的那颗按 id 接回来
    const byId = new Map(data.layout.islands.map((node) => [node.id, node]));
    if (this.hover && !byId.has(this.hover.id)) this.setHover(null);
    else if (this.hover) this.hover = byId.get(this.hover.id)!;
    if (this.entering) this.entering = byId.get(this.entering.id) ?? this.entering;
    this.drawKey();
    this.hudRects = null;
    this.requestRender();
  }

  setSelection(selectedId: string | null, panelOpen: boolean) {
    const changed = selectedId !== this.selectedId;
    this.selectedId = selectedId;
    this.panelOpen = panelOpen;
    // 选中一颗（深链、N 键、点琥珀数）：转到正前方，面板挡不住它
    if (changed && selectedId) {
      const node = this.data?.layout.islands.find((item) => item.id === selectedId);
      if (node) this.turnTo(node);
    }
    this.requestRender();
  }

  setPeek(id: string | null) {
    if (id === this.peekId) return;
    this.peekId = id;
    this.requestRender();
  }

  setTheme() {
    this.theme = readStarTheme();
    for (const node of this.data?.layout.islands ?? []) this.colors.set(node.id, parseColor(node.data.color) ?? this.theme.muted);
    this.drawKey();
    this.requestRender();
  }

  resize(width: number, height: number) {
    this.width = width;
    this.height = height;
    this.narrow = width > 0 && width < NARROW_STAGE;
    this.el.viewport.toggleAttribute("data-narrow", this.narrow);
    this.dpr = window.devicePixelRatio || 1;
    this.fit = fitScale(width, height, FAR_R);
    const { canvas } = this.el;
    canvas.width = Math.round(width * this.dpr);
    canvas.height = Math.round(height * this.dpr);
    this.hudRects = null;
    this.drawKey();
    this.requestRender();
  }

  /** 名字、琥珀数、圈名、太阳的字的尺寸：内容变了（换数据、换时间窗、字体到了）以后量一次 */
  measure() {
    this.sizes.clear();
    for (const [id, element] of this.el.labels) this.sizes.set(id, { w: element.offsetWidth, h: element.offsetHeight });
    this.badgeWidths.clear();
    for (const [id, element] of this.el.badges) this.badgeWidths.set(id, element.offsetWidth);
    this.chipSizes = this.el.chips.map((chip) => ({ w: chip?.offsetWidth ?? 0, h: chip?.offsetHeight ?? 0 }));
    this.sunLabelSize = { w: this.el.sunLabel?.offsetWidth ?? 0, h: this.el.sunLabel?.offsetHeight ?? 0 };
    this.hudRects = null;
    this.requestRender();
  }

  /** 比例尺：用和图上同一种光点画 1 / 中位 / 最多三档 */
  drawKey() {
    const canvas = this.el.keyCanvas;
    if (!canvas || !this.data || !this.width) return;
    const islands = this.data.layout.islands;
    const values = sizeKeyValues(Math.max(0, ...islands.map((node) => node.data.meetings)));
    drawSizeKey(canvas, this.theme, values, this.commonColor(), this.fit, this.dpr);
  }

  /** 项目里最常见的颜色（大多数项目用默认色），比例尺用它 */
  private commonColor(): Rgb {
    const counts = new Map<string, number>();
    for (const node of this.data?.layout.islands ?? []) counts.set(node.data.color, (counts.get(node.data.color) ?? 0) + 1);
    let best = "";
    let most = 0;
    for (const [color, count] of counts) {
      if (count > most) {
        best = color;
        most = count;
      }
    }
    return parseColor(best) ?? mix(this.theme.muted, this.theme.ink, 0.3);
  }

  // ---------------------------------------------------------------- 动画

  private animate(target: Numeric, to: Numeric, ms: number, done?: () => void) {
    // 同一个量的新动画接管旧的；旧动画的量全被接走了就整个作废，收尾回调也不再跑（飞入途中退回，就不该再进项目图）
    for (let i = this.tweens.length - 1; i >= 0; i -= 1) {
      const tween = this.tweens[i];
      if (tween.target !== target) continue;
      for (const key in to) delete tween.to[key];
      if (Object.keys(tween.to).length === 0) this.tweens.splice(i, 1);
    }
    if (prefersReducedMotion() || ms <= 0 || !this.width) {
      Object.assign(target, to);
      done?.();
      this.requestRender();
      return;
    }
    const from: Numeric = {};
    for (const key in to) from[key] = target[key];
    this.tweens.push({ target, from, to: { ...to }, t0: performance.now(), ms, done });
    this.requestRender();
  }

  private stepTweens(now: number): boolean {
    let running = false;
    for (let i = this.tweens.length - 1; i >= 0; i -= 1) {
      const tween = this.tweens[i];
      const t = Math.min(1, (now - tween.t0) / tween.ms);
      const e = EASE(t);
      for (const key in tween.to) tween.target[key] = tween.from[key] + (tween.to[key] - tween.from[key]) * e;
      if (t >= 1) {
        this.tweens.splice(i, 1);
        tween.done?.();
      } else running = true;
    }
    return running;
  }

  private stepInertia(): boolean {
    if (this.drag || prefersReducedMotion()) return false;
    if (Math.abs(this.velocity.az) < 0.02 && Math.abs(this.velocity.el) < 0.02) return false;
    this.cam.az += this.velocity.az;
    this.cam.el = clampEl(this.cam.el + this.velocity.el);
    this.velocity.az *= 0.92;
    this.velocity.el *= 0.85;
    return true;
  }

  requestRender() {
    if (!this.raf) this.raf = requestAnimationFrame(this.tick);
  }

  private readonly tick = (now: number) => {
    this.raf = 0;
    const animating = this.stepTweens(now);
    const coasting = this.stepInertia();
    this.render();
    if (animating || coasting) this.requestRender();
  };

  // ---------------------------------------------------------------- 镜头

  private camTarget() {
    return this.cam as unknown as Numeric;
  }

  turnTo(node: IslandNode) {
    this.velocity = { az: 0, el: 0 };
    this.animate(this.camTarget(), { az: this.cam.az + turnToFront(node.theta, this.cam.az) }, 640);
  }

  resetView() {
    this.velocity = { az: 0, el: 0 };
    this.animate(this.camTarget(), { az: resetAzimuth(this.cam.az), el: VIEW0.el, zoom: VIEW0.zoom }, 560, () => {
      this.cam.az = VIEW0.az;
      this.requestRender();
    });
  }

  zoomBy(factor: number) {
    this.animate(this.camTarget(), { zoom: clampZoom(this.cam.zoom * factor) }, 260);
  }

  rotateBy(daz: number, del: number) {
    this.animate(this.camTarget(), { az: this.cam.az + daz, el: clampEl(this.cam.el + del) }, 260);
  }

  /** 视角的指纹：钉住的名字只在视角没动时钉着，转盘、缩放一动就跟着行星走 */
  private viewKey() {
    const { az, el, zoom, ox } = this.cam;
    return [az, el, zoom, ox].map((value) => value.toFixed(2)).join();
  }

  /** 视角离开打开时的样子了（读数旁提示「按 0 复位」） */
  private moved(): boolean {
    return (
      Math.abs(this.cam.az - VIEW0.az) > 0.5 || Math.abs(this.cam.el - VIEW0.el) > 0.5 || Math.abs(this.cam.zoom - VIEW0.zoom) > 0.01
    );
  }

  // ---------------------------------------------------------------- 悬停、飞入、找

  /** via：从名字或琥珀数上悬停的（这时把它钉住），从画布上悬停的不填 */
  setHover(node: IslandNode | null, via?: "label" | "badge") {
    if (this.drag?.moved || this.entering) return;
    this.pin = node && via ? { id: node.id, via, view: this.viewKey() } : null;
    if (this.hover === node) return;
    const previous = this.hover;
    this.hover = node;
    if (previous) {
      const live = this.live.get(previous.id);
      if (live) this.animate(live as unknown as Numeric, { lift: 0 }, 220);
    }
    if (node) {
      const live = this.live.get(node.id);
      if (live) this.animate(live as unknown as Numeric, { lift: LIFT }, 280);
    }
    this.callbacks.onHover(node?.id ?? null);
    this.requestRender();
  }

  /** 指针离开、焦点移走：只在悬停的还是它时才清 */
  leaveHover(id: string) {
    if (this.hover?.id === id) this.setHover(null);
  }

  setHoverDock(on: boolean) {
    if (this.hoverDock === on) return;
    this.hoverDock = on;
    this.requestRender();
  }

  /** 点行星：镜头飞到它上面、展开它的三圈，落定后进项目图。减少动态、舞台还没大小时直接进 */
  enter(node: IslandNode) {
    if (this.entering) return;
    if (prefersReducedMotion() || !this.width) {
      this.callbacks.onEnter(node.data.id);
      return;
    }
    this.setHover(null);
    this.entering = node;
    this.beforeEnter = { az: this.cam.az, el: this.cam.el, zoom: this.cam.zoom, ox: this.cam.ox };
    this.callbacks.onEnteringChange(true);
    const ring = this.data!.layout.rings[node.ring];
    const R = ring.R + node.rj;
    const r = this.live.get(node.id)?.r ?? planetRadius(node.data.meetings);
    this.animate(
      this.camTarget(),
      { tx: R * Math.cos(node.theta), ty: 0, tz: R * Math.sin(node.theta), zoom: Math.min(4.2, 2.1 + 26 / Math.max(6, r)), ox: 0 },
      760,
      () => {
        if (this.entering === node) this.callbacks.onEnter(node.data.id);
      },
    );
    this.animate(this.motion, { focusFade: 1 }, 520);
  }

  /** 飞入途中按 Esc 或点一下：退回来，不进项目图 */
  cancelEnter() {
    if (!this.entering) return;
    const back = this.beforeEnter ?? { az: this.cam.az, el: this.cam.el, zoom: VIEW0.zoom, ox: 0 };
    this.animate(this.camTarget(), { tx: 0, ty: 0, tz: 0, zoom: back.zoom, ox: back.ox }, 640);
    this.animate(this.motion, { focusFade: 0 }, 520, () => {
      this.entering = null;
      this.beforeEnter = null;
      this.callbacks.onEnteringChange(false);
      this.requestRender();
    });
  }

  isEntering() {
    return this.entering !== null;
  }

  /** 在图上找：命中的亮起、其余变淡，转台把排第一的转到正前方 */
  find(query: string): FoundResult {
    const q = query.trim().toLowerCase();
    const islands = this.data?.layout.islands ?? [];
    if (!q) {
      this.found = null;
      this.requestRender();
      return { count: 0 };
    }
    const list = islands
      .filter((node) => node.data.name.toLowerCase().includes(q))
      .sort(
        (a, b) =>
          Number(b.waiting > 0) - Number(a.waiting > 0) ||
          b.data.meetings - a.data.meetings ||
          (a.age ?? 1e9) - (b.age ?? 1e9),
      );
    const first = this.found?.list[0]?.id;
    this.found = { query, list, set: new Set(list.map((node) => node.id)) };
    if (this.hover) this.setHover(null);
    if (list.length && list[0].id !== first) this.turnTo(list[0]);
    this.requestRender();
    return { count: list.length };
  }

  /** 回车：飞进排第一的那个 */
  enterFound(): boolean {
    const first = this.found?.list[0];
    if (!first) return false;
    this.enter(first);
    return true;
  }

  /** 这一帧里看得见名字的项目，屏幕坐标是名字的中心：方向键在名字之间走 */
  labelCenters(): Map<string, { x: number; y: number }> {
    const out = new Map<string, { x: number; y: number }>();
    for (const [id, rect] of this.labelRects) out.set(id, { x: (rect.x0 + rect.x1) / 2, y: (rect.y0 + rect.y1) / 2 });
    return out;
  }

  // ---------------------------------------------------------------- 指针

  private local(event: { clientX: number; clientY: number }) {
    const rect = this.el.canvas.getBoundingClientRect();
    return { x: event.clientX - rect.left, y: event.clientY - rect.top };
  }

  private hitBody(mx: number, my: number): FrameBody | null {
    let best: { body: FrameBody; d2: number } | null = null;
    const narrowed = this.found && this.found.list.length > 0;
    for (const body of this.frame) {
      if (narrowed && !this.found!.set.has(body.node.id)) continue;
      const reach = Math.max(body.rs + 4, body.node.belt ? 7 : 10);
      const ax = mx - body.hx;
      const ay = my - body.hy;
      const bx = mx - body.x;
      const by = my - body.y;
      const d2 = Math.min(ax * ax + ay * ay, bx * bx + by * by);
      if (d2 > reach * reach) continue;
      if (!best || d2 < best.d2 - 4 || (Math.abs(d2 - best.d2) <= 4 && body.d < best.body.d)) best = { body, d2 };
    }
    return best?.body ?? null;
  }

  private listen(): () => void {
    const { canvas, viewport } = this.el;
    const down = (event: PointerEvent) => {
      if (event.button !== 0) return;
      if (this.entering) {
        this.cancelEnter();
        return;
      }
      const { x, y } = this.local(event);
      this.drag = { x, y, lx: x, ly: y, t: performance.now(), moved: false, id: event.pointerId };
      this.velocity = { az: 0, el: 0 };
    };
    const move = (event: PointerEvent) => {
      const { x, y } = this.local(event);
      const drag = this.drag;
      if (drag) {
        const dx = x - drag.lx;
        const dy = y - drag.ly;
        if (!drag.moved && Math.hypot(x - drag.x, y - drag.y) > 4) {
          if (this.hover) this.setHover(null);
          drag.moved = true;
          canvas.setPointerCapture?.(drag.id);
          canvas.classList.add("is-dragging");
        }
        if (drag.moved) {
          const now = performance.now();
          const dt = Math.max(8, now - drag.t);
          this.cam.az += dx * DRAG_AZ;
          this.cam.el = clampEl(this.cam.el - dy * DRAG_EL);
          this.velocity = { az: ((dx * DRAG_AZ) / dt) * 16, el: ((-dy * DRAG_EL) / dt) * 16 };
          drag.t = now;
          drag.lx = x;
          drag.ly = y;
          this.requestRender();
          return;
        }
        drag.lx = x;
        drag.ly = y;
      }
      if (this.entering) return;
      const body = this.hitBody(x, y);
      canvas.classList.toggle("is-pointing", Boolean(body));
      this.setHover(body?.node ?? null);
    };
    const up = (event: PointerEvent) => {
      const drag = this.drag;
      this.drag = null;
      if (!drag) return;
      if (drag.moved) {
        canvas.classList.remove("is-dragging");
        if (prefersReducedMotion()) this.velocity = { az: 0, el: 0 };
        this.requestRender();
        return;
      }
      const { x, y } = this.local(event);
      const body = this.hitBody(x, y);
      // 点空白处关面板（港湾的小盘上盖着 DOM 按钮，点它不会到这里）
      if (body) this.enter(body.node);
      else if (this.panelOpen) this.callbacks.onSelect(null);
    };
    const leave = () => {
      if (!this.drag && this.hover) this.setHover(null);
      canvas.classList.remove("is-pointing");
    };
    const dblclick = (event: MouseEvent) => {
      const { x, y } = this.local(event);
      if (!this.hitBody(x, y)) this.resetView();
    };
    const wheel = (event: WheelEvent) => {
      if (this.entering) return;
      event.preventDefault();
      this.cam.zoom = clampZoom(this.cam.zoom * Math.exp(-event.deltaY * 0.0015));
      this.requestRender();
    };
    canvas.addEventListener("pointerdown", down);
    canvas.addEventListener("pointermove", move);
    canvas.addEventListener("pointerup", up);
    canvas.addEventListener("pointercancel", up);
    canvas.addEventListener("pointerleave", leave);
    canvas.addEventListener("dblclick", dblclick);
    viewport.addEventListener("wheel", wheel, { passive: false });
    return () => {
      canvas.removeEventListener("pointerdown", down);
      canvas.removeEventListener("pointermove", move);
      canvas.removeEventListener("pointerup", up);
      canvas.removeEventListener("pointercancel", up);
      canvas.removeEventListener("pointerleave", leave);
      canvas.removeEventListener("dblclick", dblclick);
      viewport.removeEventListener("wheel", wheel);
    };
  }

  // ---------------------------------------------------------------- 一帧

  private frameState(): FrameState {
    return {
      hover: this.hover,
      peekId: this.peekId,
      found: this.found?.set ?? null,
      entering: this.entering,
      focusFade: this.motion.focusFade,
      hoverDock: this.hoverDock,
    };
  }

  private render() {
    const { width: W, height: H } = this;
    if (!W || !H || !this.data) return;
    const { layout } = this.data;
    const view = makeView(this.cam, W, H, this.fit);
    const sunC = project(view, 0, SUN_Y, 0);
    const sunG = project(view, 0, 0, 0);
    this.sun = { x: sunC.x, y: sunC.y, r: SUN_R * sunC.k, gx: sunG.x, gy: sunG.y };

    this.frame = layout.islands.map((node) => {
      const live = this.live.get(node.id) ?? { r: planetRadius(node.data.meetings), m: node.data.meetings, lift: 0 };
      const R = layout.rings[node.ring].R + node.rj;
      const wx = R * Math.cos(node.theta);
      const wz = R * Math.sin(node.theta);
      const c = project(view, wx, live.lift, wz);
      const g = project(view, wx, 0, wz);
      const rest = live.lift > 0.01 ? g : c;
      const size = node.belt ? beltSize(live.m, c.k, view.fit) : glyphSize(live.r, c.k);
      return {
        node,
        color: this.colors.get(node.id) ?? this.theme.muted,
        x: c.x,
        y: c.y,
        hx: rest.x,
        hy: rest.y,
        gx: g.x,
        gy: g.y,
        rs: size.outer,
        core: size.core,
        k: c.k,
        zr: c.zr,
        d: c.d,
        f: Math.min(1, Math.max(0, (c.zr / FAR_R + 1) / 2)),
        r: live.r,
        m: live.m,
        lift: live.lift,
      };
    });

    const ctx = this.ctx;
    const state = this.frameState();
    if (ctx) {
      ctx.setTransform(this.dpr, 0, 0, this.dpr, 0, 0);
      ctx.clearRect(0, 0, W, H);
      drawBackdrop(ctx, this.backdrop, W, H, this.dpr, this.theme, this.sun);
      drawPlane(ctx, view, this.theme, state.focusFade, W, H);
      const back = this.frame.filter((body) => body.zr < 0).sort((a, b) => b.d - a.d);
      const front = this.frame.filter((body) => body.zr >= 0).sort((a, b) => b.d - a.d);
      // 一个项目都没有时只留盘面和两个角，不画空轨道
      const system = layout.islands.length > 0 || layout.belt !== null;
      if (system) drawOrbits(ctx, view, this.theme, layout.rings, "back", state);
      this.drawBridges(ctx, state);
      for (const body of back) drawBody(ctx, view, this.theme, body, state, layout.rings[body.node.ring].R);
      if (system) {
        drawSun(ctx, this.theme, this.sun, state.focusFade);
        drawOrbits(ctx, view, this.theme, layout.rings, "front", state);
      }
      for (const body of front) drawBody(ctx, view, this.theme, body, state, layout.rings[body.node.ring].R);
      if (!this.narrow) drawDocks(ctx, this.theme, W, view, this.data.harbour, this.data.folderCount, this.hoverDock, state.focusFade);
      drawVignette(ctx, this.theme, W, H);
    }
    this.placeDom(view);
    if (ctx) drawLeaders(ctx, this.theme, this.leaders);
  }

  /** 跨项目的线：不多时都画；超过 30 条只画悬停或选中的那颗连出去的 */
  private drawBridges(ctx: CanvasRenderingContext2D, state: FrameState) {
    const bridges = this.data!.layout.bridges;
    if (!bridges.length) return;
    const focus = this.hover?.id ?? this.selectedId;
    const byId = new Map(this.frame.map((body) => [body.node.id, body]));
    for (const bridge of bridges) {
      const lit = focus === bridge.a || focus === bridge.b;
      if (bridges.length > BRIDGE_ALWAYS_MAX && !lit) continue;
      const a = byId.get(bridge.a);
      const b = byId.get(bridge.b);
      if (a && b) drawBridge(ctx, this.theme, a, b, lit, 1 - state.focusFade);
    }
  }

  private readHud(): Rect[] {
    if (this.hudRects) return this.hudRects;
    const { viewport } = this.el;
    const base = viewport.getBoundingClientRect();
    const left = base.left + viewport.clientLeft;
    const top = base.top + viewport.clientTop;
    const rects: Rect[] = [];
    for (const element of this.el.hud) {
      if (!element) continue;
      const r = element.getBoundingClientRect();
      if (!r.width || !r.height) continue;
      rects.push({ x0: r.left - left - 6, y0: r.top - top - 4, x1: r.right - left + 6, y1: r.bottom - top + 6 });
    }
    if (!this.narrow) {
      const c = dockCenters(this.width);
      for (const center of [c.harbour, c.folder]) {
        rects.push({ x0: center.x - DOCK_R - 4, y0: center.y - 24, x1: center.x + DOCK_R + 4, y1: center.y + 22 });
      }
    }
    rects.push({ x0: 0, y0: this.height - 128, x1: 64, y1: this.height });
    this.hudRects = rects;
    return rects;
  }

  /** 第 k 圈的圈名在轨道上 off 度处的框（默认在圈头对面的空档里） */
  private chipRect(view: StarView, k: number, off: number) {
    const ring = this.data!.layout.rings[k];
    const t = ring.head + ring.chipAt + off * DEG;
    const q = project(view, ring.R * Math.cos(t), 0, ring.R * Math.sin(t));
    const size = this.chipSizes[k] ?? { w: 0, h: 0 };
    return { q, rect: { x0: q.x - size.w / 2, y0: q.y - size.h / 2, x1: q.x + size.w / 2, y1: q.y + size.h / 2 } };
  }

  private placeDom(view: StarView) {
    const layout = this.data!.layout;
    const silent = this.entering !== null;
    const sun = this.sun;
    const chips = layout.rings.map((_, k) => {
      const { q, rect } = this.chipRect(view, k, 0);
      return { rect, hidden: silent || (q.zr < 0 && Math.hypot(q.x - sun.x, q.y - sun.y) < sun.r + 10) };
    });
    const bodies: LabelBody[] = this.frame.map((body) => {
      const size = this.sizes.get(body.node.id) ?? { w: 0, h: 0 };
      return {
        id: body.node.id,
        x: body.x,
        y: body.y,
        rs: body.rs,
        zr: body.zr,
        ring: body.node.ring,
        ringPx: layout.rings[body.node.ring].R * body.k,
        belt: body.node.belt,
        waiting: body.node.waiting,
        meetings: body.node.data.meetings,
        age: body.node.age,
        w: size.w,
        h: size.h,
        badgeW: this.badgeWidths.get(body.node.id) ?? 0,
      };
    });
    const reveal = new Set<string>();
    if (this.hover) reveal.add(this.hover.id);
    if (this.peekId) reveal.add(this.peekId);
    if (this.pin && this.pin.view !== this.viewKey()) this.pin = null;
    const pinnedSpot = this.pin?.via === "label" ? this.spots.get(this.pin.id) : undefined;
    const out = placeLabels({
      width: this.width,
      height: this.height,
      rightInset: this.panelOpen ? PANEL_COVER : 0,
      sun,
      sunLabel: this.sunLabelSize,
      hud: this.readHud(),
      bodies,
      chips,
      chipAt: (k, off) => this.chipRect(view, k, off).rect,
      // 第一圈的圈名在太阳右后方，左右能挪 40°；外两圈的在正后方的空档里，最多挪到空档边上（空档一半减 3°）
      chipSpan: (k) => (k === 0 ? 40 : k === 1 ? 13 : 18),
      zoom: this.cam.zoom,
      found: this.found?.list.map((node) => node.id) ?? [],
      reveal,
      lastKey: this.lastKey,
      silent,
      pinned: pinnedSpot ? { id: this.pin!.id, spot: pinnedSpot } : null,
    });
    this.leaders = out.leaders;
    this.spots = out.labels;
    if (this.pin?.via === "badge") {
      const kept = this.badgeSpots.get(this.pin.id);
      if (kept) out.badges.set(this.pin.id, kept);
    }
    this.badgeSpots = out.badges;
    this.applyLabels(out.labels);

    for (const [id, element] of this.el.badges) {
      const at = out.badges.get(id);
      if (at) element.style.transform = `translate(${Math.round(at.x)}px, ${Math.round(at.y)}px)`;
      element.classList.toggle("is-gone", silent);
    }
    this.el.chips.forEach((chip, k) => {
      if (!chip) return;
      const at = out.chips[k];
      chip.style.transform = `translate(${Math.round(at.x0)}px, ${Math.round(at.y0)}px)`;
      chip.classList.toggle("is-hot", this.hover !== null && this.hover.ring === k);
      chip.classList.toggle("is-gone", chips[k].hidden);
    });
    const sunLabel = this.el.sunLabel;
    if (sunLabel) {
      sunLabel.style.transform = `translate(${Math.round(out.sunLabel.x0)}px, ${Math.round(out.sunLabel.y0)}px)`;
      sunLabel.classList.toggle("is-end", out.sunLabel.end);
      sunLabel.classList.toggle("is-gone", silent);
    }
    this.placeBridgeLabels();
    this.updateReadout();
    this.positionCard();
  }

  private applyLabels(placed: Map<string, PlacedLabel>) {
    const found = this.found;
    const top = found ? new Set(found.list.slice(0, FIND_NAMED_MAX).map((node) => node.id)) : null;
    const byId = new Map(this.frame.map((body) => [body.node.id, body]));
    this.labelRects.clear();
    for (const [id, element] of this.el.labels) {
      const spot = placed.get(id);
      const body = byId.get(id);
      if (!spot || !body) {
        element.classList.add("is-off");
        element.classList.remove("is-hot", "is-dim");
        continue;
      }
      const node = body.node;
      this.labelRects.set(id, spot.rect);
      this.lastKey.set(id, spot.key);
      element.classList.remove("is-off");
      element.style.transform = `translate(${Math.round(spot.x0)}px, ${Math.round(spot.y0)}px)`;
      if (element.dataset.align !== spot.align) element.dataset.align = spot.align;
      element.style.setProperty("--f", (0.25 + 0.75 * body.f).toFixed(3));
      element.classList.toggle("is-hot", this.hover === node || this.peekId === id || Boolean(top?.has(id)));
      element.classList.toggle(
        "is-dim",
        (this.hover !== null && this.hover !== node && this.hover.ring !== node.ring) || (found !== null && !found.set.has(id)),
      );
      element.classList.toggle("is-gone", this.entering !== null);
    }
  }

  private placeBridgeLabels() {
    if (!this.el.bridgeLabels.size) return;
    const bridges = this.data!.layout.bridges;
    const focus = this.hover?.id ?? this.selectedId;
    const byId = new Map(this.frame.map((body) => [body.node.id, body]));
    for (const bridge of bridges) {
      const element = this.el.bridgeLabels.get(bridge.id);
      if (!element) continue;
      const a = byId.get(bridge.a);
      const b = byId.get(bridge.b);
      const lit = focus === bridge.a || focus === bridge.b;
      const shown = Boolean(a && b) && (bridges.length <= BRIDGE_ALWAYS_MAX || lit) && !this.entering;
      element.classList.toggle("is-gone", !shown);
      if (a && b) element.style.transform = `translate(${Math.round((a.x + b.x) / 2)}px, ${Math.round((a.y + b.y) / 2)}px)`;
    }
  }

  private updateReadout() {
    const { az, el, zoom, hint } = this.el.readout;
    const heading = String(headingDegrees(this.cam.az)).padStart(3, "0");
    if (az && az.textContent !== `${heading}°`) az.textContent = `${heading}°`;
    const elevation = `${Math.round(this.cam.el)}°`;
    if (el && el.textContent !== elevation) el.textContent = elevation;
    const scale = `${this.cam.zoom.toFixed(2)}×`;
    if (zoom && zoom.textContent !== scale) zoom.textContent = scale;
    hint?.classList.toggle("is-on", this.moved() && !this.entering);
  }

  /** 悬停卡片朝外放：太阳左边的行星放左边，放不下再翻到另一侧；面板开着时不往面板底下放 */
  positionCard() {
    const card = this.el.card;
    if (!card || !this.hover) return;
    const body = this.frame.find((item) => item.node === this.hover);
    if (!body) return;
    const cw = card.offsetWidth;
    const ch = card.offsetHeight;
    const lr = this.labelRects.get(body.node.id) ?? { x0: body.x, x1: body.x, y0: body.y, y1: body.y };
    const left = Math.min(body.x - body.rs, lr.x0) - 14 - cw;
    const right = Math.max(body.x + body.rs, lr.x1) + 14;
    const limit = this.width - 10 - (this.panelOpen ? PANEL_COVER : 0);
    let x = body.x < this.sun.gx ? (left >= 10 ? left : right) : right + cw <= limit ? right : left;
    x = Math.max(10, Math.min(limit - cw, x));
    const y = Math.max(10, Math.min(this.height - ch - 10, body.y - ch / 2));
    card.style.left = `${Math.round(x)}px`;
    card.style.top = `${Math.round(y)}px`;
  }
}

