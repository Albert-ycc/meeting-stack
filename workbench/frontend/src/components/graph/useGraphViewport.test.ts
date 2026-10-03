import { describe, expect, it } from "vitest";

import { calcFitZoom, VIEWPORT_MIN_ZOOM } from "./useGraphViewport";

describe("calcFitZoom", () => {
  it("退无可退时不会低于全局缩放下限（画布本身的 scaleExtent 下限）", () => {
    const box = { w: 10000, h: 900 };
    const k = calcFitZoom(box, 1200, 900);
    expect(k).toBe(VIEWPORT_MIN_ZOOM);
  });

  it("适配时最多放大到 maxFitZoom（节点少时字不会大得离谱）", () => {
    const box = { w: 10, h: 10 };
    const k = calcFitZoom(box, 1200, 900, 1.25);
    expect(k).toBe(1.25);
  });
});
