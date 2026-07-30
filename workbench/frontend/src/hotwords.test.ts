import { describe, expect, it } from "vitest";

import { parseHotwordsInput, validateHotwordsInput } from "./hotwords";

describe("task hotwords", () => {
  it("normalizes NFKC, trims, splits and deduplicates case-insensitively", () => {
    expect(parseHotwordsInput(" ＡＣＭＥ, acme\n云图，MDT ")).toEqual(["ACME", "云图", "MDT"]);
    expect(parseHotwordsInput("Straße,STRASSE")).toEqual(["Straße"]);
  });

  it("gives a local hint above twenty terms without replacing backend validation", () => {
    expect(validateHotwordsInput(Array.from({ length: 21 }, (_, index) => `词${index}`).join("\n"))).toBe("本场热词最多 20 个");
    expect(validateHotwordsInput("ACME\n云图")).toBe("");
  });

  it("matches backend length and control-character limits", () => {
    expect(validateHotwordsInput("词".repeat(81))).toBe("每个热词最多 80 个字符");
    expect(validateHotwordsInput("ACME\u0000MDT")).toBe("热词不能包含控制字符");
    expect(validateHotwordsInput("词".repeat(80))).toBe("");
  });

  it("rejects every Unicode category C code point inside a term after separators are removed", () => {
    expect(validateHotwordsInput("普通中文\nACME")).toBe("");
    expect(validateHotwordsInput("普通\u200B中文")).toBe("热词不能包含控制字符");
    expect(validateHotwordsInput("私用\uE000字符")).toBe("热词不能包含控制字符");
    expect(validateHotwordsInput("代理\uD800字符")).toBe("热词不能包含控制字符");
    expect(validateHotwordsInput("未分配\u0378字符")).toBe("热词不能包含控制字符");
  });
});
