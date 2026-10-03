import { describe, expect, it } from "vitest";

import {
  DEG,
  EL_MAX,
  EL_MIN,
  VIEW0,
  ZOOM_MAX,
  ZOOM_MIN,
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
} from "./overviewProjection";

const W = 1162;
const H = 716;
const FAR = 452;

function viewAt(overrides: Partial<StarCamera> = {}) {
  const cam = { ...initialCamera(), ...overrides };
  return makeView(cam, W, H, fitScale(W, H, FAR));
}

describe("overviewProjection", () => {
  it("打开时的视角：方位 0°、仰角 32°、缩放 1；太阳落在舞台横向正中、纵向 48.8% 处", () => {
    expect(initialCamera()).toEqual({ az: 0, el: 32, zoom: 1, tx: 0, ty: 0, tz: 0, ox: 0 });
    const sun = project(viewAt(), 0, 0, 0);
    expect(sun.x).toBeCloseTo(W / 2);
    expect(sun.y).toBeCloseTo(H * 0.488);
  });

  it("倾斜的盘面：正前方的点在太阳下面、离镜头近、更大；正后方的在上面、更小；左右对称", () => {
    const view = viewAt();
    const front = project(view, 0, 0, FAR);
    const back = project(view, 0, 0, -FAR);
    const right = project(view, FAR, 0, 0);
    const left = project(view, -FAR, 0, 0);
    expect(front.y).toBeGreaterThan(view.cy);
    expect(back.y).toBeLessThan(view.cy);
    expect(front.zr).toBeGreaterThan(0);
    expect(back.zr).toBeLessThan(0);
    expect(front.k).toBeGreaterThan(back.k);
    expect(front.d).toBeLessThan(back.d);
    expect(right.x - view.cx).toBeCloseTo(view.cx - left.x);
    // 俯视得越多，前后拉得越开
    const steeper = viewAt({ el: EL_MAX });
    expect(project(steeper, 0, 0, FAR).y - project(steeper, 0, 0, -FAR).y).toBeGreaterThan(front.y - back.y);
  });

  it("缩放、横向平移、镜头盯着的点都会改变投影", () => {
    const base = project(viewAt(), FAR, 0, 0);
    const zoomed = project(viewAt({ zoom: 2 }), FAR, 0, 0);
    expect(zoomed.k).toBeCloseTo(base.k * 2);
    expect(project(viewAt({ ox: -120 }), FAR, 0, 0).x).toBeCloseTo(base.x - 120);
    // 镜头盯着一颗行星时，它落在舞台正中
    const target = project(viewAt({ tx: FAR, tz: 0 }), FAR, 0, 0);
    expect(target.x).toBeCloseTo(W / 2);
    expect(target.y).toBeCloseTo(H * 0.488);
  });

  it("转到正前方：任何轨道角、任何起始方位，转完都在正前方正中，走的是近的那一边", () => {
    for (const thetaDeg of [0, 45, 90, 135, 200, 270, 359]) {
      for (const az of [0, 90, -170, 725]) {
        const theta = thetaDeg * DEG;
        const delta = turnToFront(theta, az);
        expect(Math.abs(delta)).toBeLessThanOrEqual(180 + 1e-9);
        const view = viewAt({ az: az + delta });
        const at = project(view, FAR * Math.cos(theta), 0, FAR * Math.sin(theta));
        expect(at.x).toBeCloseTo(view.cx, 6);
        expect(at.zr).toBeCloseTo(FAR, 6);
      }
    }
    // 已经在正前方就不转
    expect(turnToFront(90 * DEG, 0)).toBe(0);
  });

  it("复位转回最近的一整圈；读数里的方位角在 0–359", () => {
    expect([370, -350, 179, 181, -181].map(resetAzimuth)).toEqual([360, -360, 0, 360, -360]);
    expect([0, -10, 359.6, 725, -725].map(headingDegrees)).toEqual([0, 350, 0, 5, 355]);
  });

  it("仰角、缩放有上下限", () => {
    expect([clampEl(0), clampEl(40), clampEl(90)]).toEqual([EL_MIN, 40, EL_MAX]);
    expect([clampZoom(0.1), clampZoom(1.3), clampZoom(9)]).toEqual([ZOOM_MIN, 1.3, ZOOM_MAX]);
    expect(VIEW0.el).toBeGreaterThanOrEqual(EL_MIN);
    expect(VIEW0.el).toBeLessThanOrEqual(EL_MAX);
  });

  it("适配比例：1440×900 下的舞台按宽算（两个系数在这里相等），又宽又矮的舞台按高算，盘面不出上下边", () => {
    expect(fitScale(W, H, FAR)).toBeCloseTo((W * 0.368) / FAR, 2);
    expect(fitScale(W, H, FAR)).toBeCloseTo((H * 0.598) / FAR, 2);
    expect(fitScale(2400, H, FAR)).toBeCloseTo((H * 0.598) / FAR);
    expect(fitScale(800, H, FAR)).toBeCloseTo((800 * 0.368) / FAR);
  });
});
