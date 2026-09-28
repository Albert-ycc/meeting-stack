import { describe, expect, it } from "vitest";

import type { RelatedWindow } from "../../api";
import { itemsAt, nearestTimes, slotStart, summaryLine, windowsAt } from "./relatedWindows";

function item(key: string) {
  return { content_key: key, ordinal: 0, loc: null, start_ms: null, text: key, words: ["驻场"], at_ms: 0 };
}

function win(start: number, keys: string[]): RelatedWindow {
  return { start_ms: start, end_ms: start + 90_000, items: keys.map(item) };
}

const WINDOWS = [win(0, ["a"]), win(45_000, ["b", "a"]), win(90_000, ["c", "d"]), win(450_000, ["e"]), win(900_000, ["f"])];

describe("relatedWindows", () => {
  it("取覆盖这个时刻的两个窗", () => {
    expect(windowsAt(WINDOWS, 60_000).map((window) => window.start_ms)).toEqual([0, 45_000]);
    expect(windowsAt(WINDOWS, 100_000).map((window) => window.start_ms)).toEqual([45_000, 90_000]);
  });

  it("合并、按内容去重、最多 3 条；同一个 45 秒里结果不变", () => {
    expect(itemsAt(WINDOWS, 60_000).map((row) => row.content_key)).toEqual(["a", "b"]);
    expect(itemsAt(WINDOWS, 100_000).map((row) => row.content_key)).toEqual(["b", "c", "a"]);
    expect(slotStart(91_000)).toBe(slotStart(134_999));
    expect(summaryLine(720_500, 2)).toBe("相关材料（12:00 前后）2 份");
    expect(summaryLine(720_500, 0)).toBe("相关材料（12:00 前后）这一段没有");
  });

  it("别的时间有：最近的时间小签，按时间排，最多 8 个", () => {
    expect(nearestTimes(WINDOWS, 600_000)).toEqual([0, 45_000, 90_000, 450_000, 900_000]);
    expect(nearestTimes(WINDOWS, 600_000, 2)).toEqual([450_000, 900_000]);
    const many = Array.from({ length: 12 }, (_, index) => win(index * 45_000 + 2_000_000, ["x"]));
    expect(nearestTimes(many, 0)).toHaveLength(8);
  });
});
