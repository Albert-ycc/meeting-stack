import { describe, expect, it } from "vitest";

import {
  BELT_NAME_ZOOM,
  NAME_BUDGET,
  badgeAt,
  breakName,
  nameBudget,
  placeLabels,
  rectOverlap,
  type LabelBody,
  type LabelInput,
  type LabelOutput,
  type Rect,
} from "./overviewLabels";

const W = 1162;
const H = 716;

function body(id: string, x: number, y: number, overrides: Partial<LabelBody> = {}): LabelBody {
  return {
    id,
    x,
    y,
    rs: 8,
    zr: 0,
    ring: 1,
    ringPx: 300,
    belt: false,
    waiting: 0,
    meetings: 1,
    age: 10,
    w: 64,
    h: 18,
    badgeW: 0,
    ...overrides,
  };
}

function input(bodies: LabelBody[], overrides: Partial<LabelInput> = {}): LabelInput {
  return {
    width: W,
    height: H,
    sun: { x: W / 2, y: H * 0.45, gx: W / 2 },
    sunLabel: { w: 90, h: 34 },
    hud: [],
    bodies,
    chips: [],
    chipAt: () => ({ x0: 0, y0: 0, x1: 0, y1: 0 }),
    chipSpan: () => 0,
    zoom: 1,
    found: [],
    reveal: new Set(),
    lastKey: new Map(),
    silent: false,
    ...overrides,
  };
}

/** 摆出来的名字两两不重叠，也不压圈名、琥珀数，全在舞台里 */
function expectClean(out: LabelOutput, data: LabelInput) {
  const rects = [...out.labels.values()].map((spot) => spot.rect);
  for (let i = 0; i < rects.length; i += 1) {
    for (let j = i + 1; j < rects.length; j += 1) expect(rectOverlap(rects[i], rects[j])).toBe(0);
  }
  const right = data.width - (data.rightInset ?? 0) - 6;
  for (const rect of rects) {
    expect(rect.x0).toBeGreaterThanOrEqual(6);
    expect(rect.y0).toBeGreaterThanOrEqual(6);
    expect(rect.x1).toBeLessThanOrEqual(right);
    expect(rect.y1).toBeLessThanOrEqual(data.height - 6);
  }
  for (const chip of data.chips.filter((item) => !item.hidden)) {
    for (const rect of rects) expect(rectOverlap(rect, chip.rect)).toBe(0);
  }
  for (const item of data.bodies.filter((b) => b.badgeW)) {
    const at = out.badges.get(item.id)!;
    const badge: Rect = { x0: at.x, y0: at.y, x1: at.x + item.badgeW, y1: at.y + 18 };
    for (const rect of rects) expect(rectOverlap(rect, badge)).toBe(0);
  }
}

/** 固定种子的伪随机：密集但每次一样的一堆行星 */
function crowd(count: number, seed: number): LabelBody[] {
  let s = seed;
  const random = () => {
    s = (s * 16807) % 2147483647;
    return s / 2147483647;
  };
  return Array.from({ length: count }, (_, index) =>
    body(`b${index}`, 120 + random() * (W - 240), 90 + random() * (H - 180), {
      w: 40 + random() * 70,
      h: random() < 0.4 ? 36 : 18,
      zr: random() * 600 - 300,
      ring: Math.floor(random() * 3),
      meetings: Math.floor(random() * 6),
      age: Math.floor(random() * 60),
      badgeW: random() < 0.1 ? 18 : 0,
      waiting: random() < 0.1 ? 1 : 0,
    }),
  );
}

describe("名字断行", () => {
  it("短名字一行；长的断成两行，两行差不多长，断在常见的尾词前、西文词后", () => {
    expect(breakName("医米科研用药")).toEqual(["医米科研用药"]);
    expect(breakName("CVM 云讲堂")).toEqual(["CVM 云讲堂"]);
    expect(breakName("Keep")).toEqual(["Keep"]);
    // 和原型截图里的断法一致
    expect(breakName("北京西苑患者管理项目")).toEqual(["北京西苑", "患者管理项目"]);
    expect(breakName("口服药到店领取配置方案")).toEqual(["口服药到店", "领取配置方案"]);
    expect(breakName("供应商录音质检账号开通")).toEqual(["供应商录音质检", "账号开通"]);
    expect(breakName("项目复制与名单一键转移")).toEqual(["项目复制与", "名单一键转移"]);
    expect(breakName("医朵云SFE方案整合")).toEqual(["医朵云SFE", "方案整合"]);
    expect(breakName("医米EDC新项目配置")).toEqual(["医米EDC", "新项目配置"]);
    expect(breakName("礼邦RWS项目迁移")).toEqual(["礼邦RWS", "项目迁移"]);
    expect(breakName("生物信号小程序二类证")).toEqual(["生物信号小程序", "二类证"]);
  });

  it("西文长词不从中间拆开", () => {
    const lines = breakName("Roadmap Planning 2026 年度复盘");
    expect(lines).toHaveLength(2);
    for (const line of lines) expect(line).not.toMatch(/^[a-z]/);
    expect(lines.join(" ")).toBe("Roadmap Planning 2026 年度复盘");
  });
});

describe("名字预算", () => {
  it(`全图大约 ${NAME_BUDGET} 个，放大时按缩放的 2.2 次方增长`, () => {
    expect(nameBudget(1)).toBe(40);
    expect(nameBudget(0.6)).toBe(Math.round(40 * 0.6 ** 2.2));
    expect(nameBudget(1.6)).toBe(Math.round(40 * 1.6 ** 2.2));
    expect(nameBudget(0.6)).toBeLessThan(nameBudget(1));
    expect(nameBudget(2.2)).toBeGreaterThan(nameBudget(1.6));
  });

  it("预算用完时按优先级取：有在等你的 > 窗口里场次多 > 越近开过会；预算外的悬停、在面板里指着时照样写", () => {
    // 45 颗散得很开（不会互压），预算 40：最后 5 颗没有名字
    const bodies: LabelBody[] = [];
    for (let index = 0; index < 45; index += 1) {
      const x = 120 + (index % 9) * 320;
      const y = 80 + Math.floor(index / 9) * 160;
      bodies.push(
        body(`b${index}`, x, y, {
          waiting: index < 3 ? 1 : 0,
          meetings: index < 3 ? 0 : index < 40 ? 40 - index : 0,
          age: index < 40 ? 100 : [5, 50, 100, 200, null][index - 40],
        }),
      );
    }
    const wide = { width: 3200, height: 1000 };
    const out = placeLabels(input(bodies, wide));
    expect([...out.labels.keys()].sort()).toEqual(bodies.slice(0, 40).map((item) => item.id).sort());
    // 在等你的哪怕窗口里一场都没有也有名字；预算外的 5 颗里越近开过会的越靠前，但都没进预算
    expect(out.labels.has("b0")).toBe(true);
    const revealed = placeLabels(input(bodies, { ...wide, reveal: new Set(["b44"]) }));
    expect(revealed.labels.has("b44")).toBe(true);
    expect(revealed.labels.size).toBe(41);
  });

  it(`小行星带里的名字放大到 ${BELT_NAME_ZOOM}× 以上才开始放`, () => {
    const bodies = [body("star", 300, 300, { meetings: 1 }), body("dust", 800, 300, { belt: true, meetings: 9, ring: 2 })];
    expect([...placeLabels(input(bodies, { zoom: 1 })).labels.keys()]).toEqual(["star"]);
    expect([...placeLabels(input(bodies, { zoom: BELT_NAME_ZOOM })).labels.keys()].sort()).toEqual(["dust", "star"]);
  });

  it("在图上找到的最先放、一定写名字，排在最前；飞入时一个名字都不写", () => {
    const bodies = crowd(80, 7);
    const out = placeLabels(input(bodies, { found: ["b79"] }));
    expect([...out.labels.keys()][0]).toBe("b79");
    expect(placeLabels(input(bodies, { silent: true })).labels.size).toBe(0);
  });
});

describe("避让", () => {
  it("一堆挤在一起的行星：摆出来的名字两两不重叠、不压圈名、琥珀数、太阳的字和读数块，都在舞台里", () => {
    for (const seed of [1, 2, 3, 4, 5]) {
      const bodies = crowd(90, seed);
      const chips = [
        { rect: { x0: 520, y0: 140, x1: 640, y1: 162 }, hidden: false },
        { rect: { x0: 700, y0: 300, x1: 790, y1: 322 }, hidden: false },
      ];
      const hud = [
        { x0: 430, y0: 8, x1: 740, y1: 48 },
        { x0: 0, y0: H - 128, x1: 64, y1: H },
      ];
      const data = input(bodies, { chips, hud });
      const out = placeLabels(data);
      expect(out.labels.size).toBeGreaterThan(10);
      expectClean(out, data);
      const sun = { x0: out.sunLabel.x0, y0: out.sunLabel.y0, x1: out.sunLabel.x0 + 90, y1: out.sunLabel.y0 + 34 };
      for (const spot of out.labels.values()) {
        expect(rectOverlap(spot.rect, sun)).toBe(0);
        for (const rect of hud) expect(rectOverlap(spot.rect, rect)).toBe(0);
      }
    }
  });

  it("面板盖住右边时名字不往面板底下放", () => {
    const data = input(crowd(60, 11), { rightInset: 412 });
    const out = placeLabels(data);
    expectClean(out, data);
    for (const spot of out.labels.values()) expect(spot.rect.x1).toBeLessThanOrEqual(W - 412 - 6);
  });

  it("名字写在朝外的一侧：圈的右手写在右边，左手写在左边，前排写在下面", () => {
    const sun = { x: 581, y: 300, gx: 581 };
    const out = placeLabels(
      input(
        [
          body("right", 1000, 330, { ringPx: 400 }),
          body("left", 160, 330, { ringPx: 400 }),
          body("front", 600, 600, { ringPx: 400, zr: 300 }),
        ],
        { sun },
      ),
    );
    expect(out.labels.get("right")?.key).toBe("R");
    expect(out.labels.get("right")?.align).toBe("start");
    expect(out.labels.get("left")?.key).toBe("L");
    expect(out.labels.get("left")?.align).toBe("end");
    expect(out.labels.get("front")?.key).toBe("B");
    expect(out.labels.get("front")?.align).toBe("center");
  });

  it("上一帧选的位置略占便宜（拖动时标签不来回翻边）", () => {
    const one = [body("x", 1000, 330, { ringPx: 400 })];
    expect(placeLabels(input(one)).labels.get("x")?.key).toBe("R");
    expect(placeLabels(input(one, { lastKey: new Map([["x", "Ru"]]) })).labels.get("x")?.key).toBe("Ru");
  });

  it("指针停在一个名字上时它钉在原地，别的名字给它让", () => {
    const bodies = [body("a", 500, 300), body("b", 520, 306)];
    const spot = { x0: 480, y0: 320, align: "center" as const, key: "B", rect: { x0: 480, y0: 320, x1: 544, y1: 338 } };
    const out = placeLabels(input(bodies, { pinned: { id: "a", spot } }));
    expect(out.labels.get("a")).toMatchObject({ x0: 480, y0: 320, key: "B" });
    const b = out.labels.get("b");
    if (b) expect(rectOverlap(b.rect, spot.rect)).toBe(0);
  });

  it("圈名压到名字时沿轨道滑开；太阳的字放在它旁边没东西的地方", () => {
    const bodies = [body("a", 600, 200, { ringPx: 500, zr: -200, ring: 2 })];
    const first = placeLabels(input(bodies));
    const label = first.labels.get("a")!.rect;
    const chipAt = (_k: number, off: number) => ({ x0: label.x0 + off * 3, y0: label.y0, x1: label.x0 + off * 3 + 80, y1: label.y0 + 20 });
    const out = placeLabels(input(bodies, { chips: [{ rect: chipAt(0, 0), hidden: false }], chipAt, chipSpan: () => 40 }));
    const chip = out.chips[0];
    const chipRect = { x0: chip.x0, y0: chip.y0, x1: chip.x0 + 80, y1: chip.y0 + 20 };
    for (const spot of out.labels.values()) expect(rectOverlap(spot.rect, chipRect)).toBe(0);

    const sunOnly = placeLabels(input([]));
    expect(sunOnly.sunLabel).toEqual({ x0: W / 2 + 20, y0: H * 0.45 - 34 - 6, end: false });
  });

  it("琥珀数挂在本体右上角；同样的输入每次摆得一样", () => {
    expect(badgeAt({ x: 100, y: 100, rs: 10 })).toEqual({ x: 102.2, y: 80.8 });
    const data = input(crowd(70, 3));
    expect(placeLabels(data)).toEqual(placeLabels(input(crowd(70, 3))));
  });
});
