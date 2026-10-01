import { describe, expect, it } from "vitest";

import { EMPTY_FLAG_LAYOUT, flagLeft, packFlags, sameFlagLayout, type FlagBox } from "./flagLayout";

/*
 * 时间锚照生产库逐字稿抄（和 poolFixtures、后端 requirement_pool_world 同一份）：
 * 京东科研仓对接那场会时长 3033387 ms，00:31:49（1909360）和后一句 00:31:55（1915910）只差 6.5 秒；
 * 云课堂那场会时长 747000 ms，00:09:36（576900）和后一句 00:09:41（581000）只差 4 秒。
 */
const JD_DURATION = 3033387;
const CVM_DURATION = 747000;
const percentOf = (anchorMs: number, durationMs: number) => (anchorMs / durationMs) * 100;

const WAVE_WIDTH = 1300; // 1440 宽的屏上波形大约这么宽
const ORIGIN_FLAG_WIDTH = 96; // 「00:31:49 提出」
const MERGED_FLAG_WIDTH = 196; // 「00:31:55 合并 · 京东仓签收凭证」

function box(id: number, anchorMs: number, durationMs: number, width: number): FlagBox {
  return { id, percent: percentOf(anchorMs, durationMs), width };
}

describe("时间签的位置", () => {
  it("锚点在录音头上，签的左沿对齐波形左边；在录音末尾，右沿对齐波形右边；在正中，签居中", () => {
    expect(flagLeft({ id: 1, percent: 0, width: 96 }, WAVE_WIDTH)).toBe(0);
    expect(flagLeft({ id: 1, percent: 100, width: 96 }, WAVE_WIDTH) + 96).toBe(WAVE_WIDTH);
    expect(flagLeft({ id: 1, percent: 50, width: 96 }, WAVE_WIDTH)).toBe((WAVE_WIDTH - 96) / 2);
  });

  it("锚点从头到尾扫一遍，签都不伸出波形的边", () => {
    for (let percent = 0; percent <= 100; percent += 2.5) {
      const left = flagLeft({ id: 1, percent, width: 196 }, WAVE_WIDTH);
      expect(left).toBeGreaterThanOrEqual(0);
      expect(left + 196).toBeLessThanOrEqual(WAVE_WIDTH);
    }
  });
});

describe("挨得近的时间签错开", () => {
  it("隔得远的放在同一行", () => {
    // 京东那场的 00:13:45 提出（825270）和 00:31:49 合并（1909360），差 18 分钟
    const layout = packFlags(
      [box(1, 825270, JD_DURATION, ORIGIN_FLAG_WIDTH), box(2, 1909360, JD_DURATION, MERGED_FLAG_WIDTH)],
      WAVE_WIDTH,
    );
    expect(layout.rows).toBe(1);
    expect(layout.rowOf.get(1)).toBe(0);
    expect(layout.rowOf.get(2)).toBe(0);
  });

  it("差几秒的两个锚点：后一个错开到上一行，两个签的占位不叠", () => {
    const boxes = [
      box(1, 1909360, JD_DURATION, ORIGIN_FLAG_WIDTH),
      box(2, 1915910, JD_DURATION, MERGED_FLAG_WIDTH),
    ];
    const layout = packFlags(boxes, WAVE_WIDTH);

    expect(layout.rows).toBe(2);
    expect(layout.rowOf.get(1)).toBe(0);
    expect(layout.rowOf.get(2)).toBe(1);
  });

  it("云课堂那场的 09:36 和 09:41（差 4 秒）同样错开；签的顺序按锚点先后，不按传进来的顺序", () => {
    const later = box(2, 581000, CVM_DURATION, MERGED_FLAG_WIDTH);
    const earlier = box(1, 576900, CVM_DURATION, ORIGIN_FLAG_WIDTH);
    const layout = packFlags([later, earlier], WAVE_WIDTH);

    expect(layout.rows).toBe(2);
    expect(layout.rowOf.get(1)).toBe(0);
    expect(layout.rowOf.get(2)).toBe(1);
  });

  it("同一行里签与签之间留一道缝：刚好叠上或贴上都算放不下", () => {
    // 两个宽 100 的签，左沿相距 100：贴着、没有缝，要换行；相距 106 才放得下
    const tight = packFlags(
      [
        { id: 1, percent: 0, width: 100 },
        { id: 2, percent: 10, width: 100 },
      ],
      1100,
    );
    expect(tight.rowOf.get(2)).toBe(1);
    const roomy = packFlags(
      [
        { id: 1, percent: 0, width: 100 },
        { id: 2, percent: 10.6, width: 100 },
      ],
      1100,
    );
    expect(roomy.rowOf.get(2)).toBe(0);
  });

  it("三个挤在一起的签排成三行，后面隔得远的又回到第一行", () => {
    const layout = packFlags(
      [
        box(1, 387880, CVM_DURATION, 96),
        box(2, 389820, CVM_DURATION, 150),
        box(3, 391400, CVM_DURATION, 120),
        box(4, 568390, CVM_DURATION, 96),
      ],
      WAVE_WIDTH,
    );
    expect([1, 2, 3, 4].map((id) => layout.rowOf.get(id))).toEqual([0, 1, 2, 0]);
    expect(layout.rows).toBe(3);
  });

  it("贴着录音头尾的两个签也不叠：头上一个、末尾一个，宽的那个不会伸出去", () => {
    const layout = packFlags(
      [
        { id: 1, percent: 0, width: 96 },
        { id: 2, percent: 100, width: 196 },
      ],
      WAVE_WIDTH,
    );
    expect(layout.rows).toBe(1);
  });

  it("容器宽度量不出来（0）时不排，全放第一行；没有签也只占一行", () => {
    expect(packFlags([box(1, 825270, JD_DURATION, 0), box(2, 827350, JD_DURATION, 0)], 0)).toBe(EMPTY_FLAG_LAYOUT);
    expect(packFlags([], WAVE_WIDTH)).toBe(EMPTY_FLAG_LAYOUT);
  });

  it("比较两次排版：行数和每个签的行一样才算同一份", () => {
    const boxes = [box(1, 1909360, JD_DURATION, 96), box(2, 1915910, JD_DURATION, 196)];
    expect(sameFlagLayout(packFlags(boxes, WAVE_WIDTH), packFlags(boxes, WAVE_WIDTH))).toBe(true);
    expect(sameFlagLayout(packFlags(boxes, WAVE_WIDTH), EMPTY_FLAG_LAYOUT)).toBe(false);
  });
});
