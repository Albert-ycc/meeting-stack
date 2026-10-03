// 全部项目星图的镜头与投影：纯函数，不碰 DOM。
//
// 世界坐标的单位约等于缩放为 1 时的像素：太阳在原点，轨道平面是 XZ，+Z 朝镜头（正前方），+Y 朝上。
// 镜头绕太阳转：先按方位角绕 Y 轴转，再按仰角绕 X 轴俯下来，最后做透视除法（视距 VIEW_DISTANCE）。
// 没有 three.js：Canvas2D 画的每个点都先过一遍 project，DOM 标签锚在投影出来的屏幕坐标上，所以字永远正对屏幕。

export const DEG = Math.PI / 180;
export const TAU = Math.PI * 2;
/** 视距：越大透视越弱。2000 时最外圈前后两端的大小差约三成，够读出纵深又不至于把后排压得太小 */
const VIEW_DISTANCE = 2000;

export interface StarCamera {
  /** 方位角（度），转台转了多少 */
  az: number;
  /** 仰角（度），镜头高出轨道平面多少 */
  el: number;
  zoom: number;
  /** 镜头盯着的世界点：平时是太阳，飞入时移到行星上 */
  tx: number;
  ty: number;
  tz: number;
  /** 整个画面的横向平移（像素） */
  ox: number;
}

/** 打开时和复位后的视角 */
export const VIEW0 = { az: 0, el: 32, zoom: 1 } as const;
export const EL_MIN = 18;
export const EL_MAX = 52;
export const ZOOM_MIN = 0.6;
export const ZOOM_MAX = 2.2;

export function initialCamera(): StarCamera {
  return { az: VIEW0.az, el: VIEW0.el, zoom: VIEW0.zoom, tx: 0, ty: 0, tz: 0, ox: 0 };
}

export const clampEl = (el: number) => Math.max(EL_MIN, Math.min(EL_MAX, el));
export const clampZoom = (zoom: number) => Math.max(ZOOM_MIN, Math.min(ZOOM_MAX, zoom));

export interface Projected {
  x: number;
  y: number;
  /** 这一点的缩放（含透视）：世界长度 × k = 屏幕像素 */
  k: number;
  /** 转过方位角后的前后坐标：> 0 在太阳前面（离镜头更近） */
  zr: number;
  /** 离镜头的距离，画家算法按它从远到近画 */
  d: number;
}

/** 一帧里投影要用的全部量：每帧算一次，之后 project 只做乘加 */
export interface StarView {
  ca: number;
  sa: number;
  ce: number;
  se: number;
  cx: number;
  cy: number;
  /** 缩放为 1 时世界单位到像素的比例，跟着舞台大小走 */
  fit: number;
  cam: StarCamera;
}

/**
 * 舞台的适配比例：最外圈（farR）占舞台宽的 36.8%；舞台又宽又矮时改按高度算，盘面不出上下边。
 * 两个系数在 1440×900 的舞台上相等，那个尺寸下和原型一模一样。
 */
export function fitScale(width: number, height: number, farR: number): number {
  return Math.min(width * 0.368, height * 0.598) / farR;
}

export function makeView(cam: StarCamera, width: number, height: number, fit: number): StarView {
  const a = cam.az * DEG;
  const e = cam.el * DEG;
  return {
    ca: Math.cos(a),
    sa: Math.sin(a),
    ce: Math.cos(e),
    se: Math.sin(e),
    cx: width * 0.5 + cam.ox,
    cy: height * 0.488,
    fit,
    cam,
  };
}

/** 世界 → 屏幕 */
export function project(view: StarView, wx: number, wy: number, wz: number): Projected {
  const { cam } = view;
  const x = wx - cam.tx;
  const y = wy - cam.ty;
  const z = wz - cam.tz;
  const xr = x * view.ca - z * view.sa;
  const zr = x * view.sa + z * view.ca;
  const yc = y * view.ce - zr * view.se;
  const d = VIEW_DISTANCE - y * view.se - zr * view.ce;
  const k = (VIEW_DISTANCE / d) * cam.zoom * view.fit;
  return { x: view.cx + xr * k, y: view.cy - yc * k, k, zr, d };
}

/**
 * 把轨道角 theta（弧度）上的行星转到正前方要转多少度方位角：走近的那一边，结果在 [-180, 180)。
 * 正前方是 theta + 方位角 = 90°。
 */
export function turnToFront(theta: number, az: number): number {
  const target = 90 - theta / DEG;
  return (((target - az) % 360) + 540) % 360 - 180;
}

/** 复位时方位角回到最近的一整圈，不倒着转好几圈 */
export function resetAzimuth(az: number): number {
  return Math.round(az / 360) * 360 + VIEW0.az;
}

/** 读数里的方位角：0–359 */
export function headingDegrees(az: number): number {
  return ((Math.round(az) % 360) + 360) % 360;
}
