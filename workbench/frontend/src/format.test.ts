import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { formatBeijingMonthDay, formatMonthDay, formatMonthDayClock, formatSpeakerLabel } from "./format";

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

describe("formatBeijingMonthDay（D12：已完成、已搁置海报底栏的日子按北京日历）", () => {
  // 用例进程的时区钉在上海；这里临时换到太平洋时区（声档实际跑在那儿），确认不跟着本机时区走
  const env = (globalThis as unknown as { process: { env: Record<string, string | undefined> } }).process.env;
  let saved: string | undefined;
  beforeEach(() => {
    saved = env.TZ;
    env.TZ = "America/Los_Angeles";
  });
  afterEach(() => {
    env.TZ = saved;
  });

  it("北京 10-09 凌晨（太平洋还是 10-08）写 10-09", () => {
    const value = "2026-10-08T17:30:00+00:00";
    // 换时区生效了：本机日历上它还是 10-08
    expect(formatMonthDay(value)).toBe("10-08");
    expect(formatBeijingMonthDay(value)).toBe("10-09");
  });

  it("没有日期显示「—」，解析不了的原样截取月日", () => {
    expect(formatBeijingMonthDay(null)).toBe("—");
    expect(formatBeijingMonthDay("2026-09-08 坏数据")).toBe("09-08");
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
