import { describe, expect, it } from "vitest";

import { failureStageLabel, formatTaskEventBody, serviceStateLabel, versionKindLabel } from "./format";

describe("界面不露内部状态码", () => {
  it("任务事件里的状态流转翻成中文，其他正文原样保留", () => {
    expect(formatTaskEventBody("confirmed → done")).toBe("已确认 → 已完成");
    expect(formatTaskEventBody("pending_confirm → expired")).toBe("待确认 → 已过期");
    expect(formatTaskEventBody("登记交付物：link https://example.com/done")).toBe(
      "登记交付物：link https://example.com/done",
    );
    expect(formatTaskEventBody("foo → bar")).toBe("foo → bar");
  });

  it("服务状态翻成中文，未知值原样返回", () => {
    expect(serviceStateLabel("degraded")).toBe("部分降级");
    expect(serviceStateLabel("unavailable")).toBe("连不上");
    expect(serviceStateLabel("brand_new_state")).toBe("brand_new_state");
  });

  it("逐字稿与纪要版本类型翻成中文", () => {
    expect(versionKindLabel("generated")).toBe("AI 生成");
    expect(versionKindLabel("funasr")).toBe("FunASR 主稿");
    expect(versionKindLabel("draft")).toBe("人工修改");
    // 数据里对照稿的 kind 实际是 whisper_reference / qwen_reference
    expect(versionKindLabel("whisper_reference")).toBe("Whisper 对照稿");
    expect(versionKindLabel("qwen_reference")).toBe("Qwen 对照稿");
  });

  it("转写任务失败阶段不露阶段码", () => {
    expect(failureStageLabel("pending_archive")).toBe("放进会议文件夹");
    expect(failureStageLabel("codex_callback")).toBe("纪要生成");
    expect(failureStageLabel("brand_new_stage")).toBe("处理");
  });
});
