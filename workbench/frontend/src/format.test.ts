import { describe, expect, it } from "vitest";

import { formatMonthDay, formatMonthDayClock, formatSpeakerLabel } from "./format";

describe("formatMonthDay / formatMonthDayClock", () => {
  // 用本地时间构造输入，断言不受测试机时区影响
  const local = new Date(2026, 8, 8, 14, 5).toISOString();

  it("按浏览器时区给出 MM-DD 与 MM-DD HH:mm", () => {
    expect(formatMonthDay(local)).toBe("09-08");
    expect(formatMonthDayClock(local)).toBe("09-08 14:05");
  });

  it("没有日期显示「—」，解析不了的原样截取月日", () => {
    expect(formatMonthDay(null)).toBe("—");
    expect(formatMonthDayClock(undefined)).toBe("—");
    expect(formatMonthDay("2026-09-08 坏数据")).toBe("09-08");
  });
});

describe("formatSpeakerLabel", () => {
  it("converts a plain FunASR label to a 1-based display name", () => {
    expect(formatSpeakerLabel("SPEAKER_00")).toBe("说话人 1");
    expect(formatSpeakerLabel("SPEAKER_03")).toBe("说话人 4");
  });

  it("keeps the chunk dimension visible for a chunked meeting's label", () => {
    expect(formatSpeakerLabel("C2_SPEAKER_00")).toBe("片段2·说话人1");
    expect(formatSpeakerLabel("C10_SPEAKER_05")).toBe("片段10·说话人6");
  });

  it("passes through labels that match neither known shape, and empty values", () => {
    expect(formatSpeakerLabel("旁听嘉宾")).toBe("旁听嘉宾");
    expect(formatSpeakerLabel(null)).toBe("");
    expect(formatSpeakerLabel(undefined)).toBe("");
    expect(formatSpeakerLabel("")).toBe("");
  });
});
