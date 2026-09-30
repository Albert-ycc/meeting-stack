import { describe, expect, it } from "vitest";
import { fitWrongChips } from "./GlossaryTermList";

// 箭头 14px，后面空 8px，所以块从 22 开始；「+N」一位数占 20，两位数占 27
describe("fitWrongChips：一格放几块错写", () => {
  it("全放得下就全放，最后一块后面不用给「+N」留位", () => {
    expect(fitWrongChips(152, 14, [40, 40, 40])).toBe(3);
  });

  it("放不下的收起来，并给「+N」留出位置", () => {
    // 22+40 再留「+2」的 20 → 82；第二块 67+40 再留「+1」→ 127 > 120
    expect(fitWrongChips(120, 14, [40, 40, 40])).toBe(1);
    expect(fitWrongChips(151, 14, [40, 40, 40])).toBe(2);
  });

  it("收起十块以上时「+N」按两位数留位", () => {
    const twelve = Array.from({ length: 12 }, () => 30);
    // 放两块：22+30+5+30 再留「+10」的 27 → 114；按一位数留位的话 107 就够
    expect(fitWrongChips(114, 14, twelve)).toBe(2);
    expect(fitWrongChips(113, 14, twelve)).toBe(1);
  });

  it("第一块都放不全时，还能露出一个字就压着放", () => {
    // 22 + 外壳 14 + 一个字 13 + 「+1」20 = 69
    expect(fitWrongChips(80, 14, [100, 40])).toBe(1);
    expect(fitWrongChips(69, 14, [100, 40])).toBe(1);
  });

  it("再窄就一块不放，只留「← +N」", () => {
    expect(fitWrongChips(68, 14, [100, 40])).toBe(0);
  });

  it("只有一块时总是放它，没有错写时是 0", () => {
    expect(fitWrongChips(10, 14, [100])).toBe(1);
    expect(fitWrongChips(300, 14, [])).toBe(0);
  });
});
