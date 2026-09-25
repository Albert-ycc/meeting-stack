import { describe, expect, it } from "vitest";

import { describeAttention } from "./OverviewPage";

describe("工作台失败提示按阶段说清楚", () => {
  it("转写失败和纪要失败分开说，不笼统说纪要会自动重试", () => {
    const text = describeAttention({ transcription: 3, minutes: 1, archive: 0, other: 0 }, 4);

    expect(text).toBe("3 个转写失败、1 个纪要没生成，到资料库「需要处理」里查看。");
    expect(text).not.toContain("自动重试");
  });

  it("没有分阶段数据时退回总数", () => {
    expect(describeAttention(null, 2)).toBe("2 个录音处理失败，到资料库「需要处理」里查看。");
  });

  it("没有需要处理的录音时给常规空态文案", () => {
    expect(describeAttention({ transcription: 0, minutes: 0, archive: 0, other: 0 }, 0)).toBe(
      "新录音出现后会显示在这里。",
    );
  });
});
