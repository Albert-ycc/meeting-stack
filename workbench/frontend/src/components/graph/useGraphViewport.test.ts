import { describe, expect, it } from "vitest";

import { calcFitZoom, VIEWPORT_MIN_ZOOM } from "./useGraphViewport";

// D3：全部项目概览默认视图和［复位］把第一行项目和「港湾」切在画布外。
// 根因是 fitView 里 Math.max(minFitZoom, 自然缩放) 强行把缩放顶到 minFitZoom（概览传
// 0.8，为了项目名读得清），但 31 个项目要装进可见区，需要的自然缩放比 0.8 还小，被顶高之后
// 内容必然溢出。calcFitZoom 把这段逻辑抽出来单测：minFitZoom 只在「不会因此裁切」时才生效，
// 装不下时让位给能完整装下的那个更小的缩放。
describe("calcFitZoom", () => {
  it("box 比可视区宽很多时（概览 31 个项目那种规模），不能被 minFitZoom 顶到会溢出的缩放", () => {
    const box = { w: 3000, h: 900 };
    const viewportW = 1200;
    const viewportH = 900;
    const minFitZoom = 0.8; // OverviewCanvas 传的 MIN_FIT_ZOOM
    const k = calcFitZoom(box, viewportW, viewportH, minFitZoom);
    // 旧逻辑会直接返回 0.8，装完整个 box 需要的宽度是 3000*0.8=2400，远超 1200 的视口——溢出。
    expect(k).toBeLessThan(minFitZoom);
    // 新逻辑：缩放后的宽度不能超过可视区（留出 32px*2 的内边距）。
    expect(box.w * k).toBeLessThanOrEqual(viewportW - 64 + 0.01);
  });

  it("box 本来就比可视区小得多时，minFitZoom 照常把画面拉近（不影响正常场景）", () => {
    const box = { w: 400, h: 300 };
    const k = calcFitZoom(box, 1200, 900, 0.8);
    expect(k).toBeGreaterThanOrEqual(0.8);
  });

  it("退无可退时不会低于全局缩放下限（画布本身的 scaleExtent 下限）", () => {
    const box = { w: 10000, h: 900 };
    const k = calcFitZoom(box, 1200, 900);
    expect(k).toBe(VIEWPORT_MIN_ZOOM);
  });

  it("适配时最多放大到 maxFitZoom（节点少时字不会大得离谱）", () => {
    const box = { w: 10, h: 10 };
    const k = calcFitZoom(box, 1200, 900, VIEWPORT_MIN_ZOOM, 1.25);
    expect(k).toBe(1.25);
  });
});
