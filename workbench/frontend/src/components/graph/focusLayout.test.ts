import { describe, expect, it } from "vitest";

import { BAR_W, FOCUS_DECISIONS_MAX, FOCUS_TASKS_MAX, decisionNodeId, layoutMeetingFocus } from "./focusLayout";
import { overlaps } from "./layout";
import { focusPayload, focusTask as task } from "./testFixtures";

describe("layoutMeetingFocus", () => {
  it("决议在条上方、任务在下方，按时间点对齐到录音条", () => {
    const layout = layoutMeetingFocus(focusPayload());
    const decision = layout.items.find((item) => item.id === "dec:0")!;
    const pending = layout.items.find((item) => item.id === "task:t1")!;
    expect(decision.anchorX).toBe(BAR_W * 0.1);
    expect(decision.y).toBeLessThan(0);
    expect(pending.anchorX).toBe(BAR_W * 0.5);
    expect(pending.y).toBeGreaterThan(0);
    expect(layout.durationKnown).toBe(true);
    expect(layout.ticks[0]).toMatchObject({ x: 0, label: "00:00" });
    expect(layout.ticks.at(-1)).toMatchObject({ x: BAR_W, label: "10:00" });
  });

  it("没时间点的另排在最外面一排，并写明「没有时间点」", () => {
    const layout = layoutMeetingFocus(focusPayload());
    const untimedDecision = layout.items.find((item) => item.id === "dec:1")!;
    const untimedTask = layout.items.find((item) => item.id === "task:t2")!;
    expect(untimedDecision.anchorX).toBeNull();
    expect(untimedDecision.y).toBeLessThan(layout.items.find((item) => item.id === "dec:0")!.y);
    expect(untimedTask.y).toBeGreaterThan(layout.items.find((item) => item.id === "task:t1")!.y);
    expect(layout.untimedLabels.map((label) => label.side).sort()).toEqual([-1, 1]);
  });

  it("决议带台账 id（4a）时节点 id 是 dec:<决议 id>，调了顺序也不变；台账落后时 id 为 null，退回下标", () => {
    const decisions = [
      { id: "dec-3f2a9c0b1d4e5f60", text: "初审规则按新口径执行", start_ms: 60_000, end_ms: 95_000 },
      { id: null, text: "台账还没轮到的决议", start_ms: 120_000 },
      { id: "dec-00aa11bb22cc33dd", text: "没写时间的决议", start_ms: null },
    ];
    const layout = layoutMeetingFocus(focusPayload({ decisions }));
    expect(layout.items.filter((item) => item.kind === "decision").map((item) => item.id).sort()).toEqual([
      "dec:1",
      "dec:dec-00aa11bb22cc33dd",
      "dec:dec-3f2a9c0b1d4e5f60",
    ]);
    const anchored = layout.items.find((item) => item.id === "dec:dec-3f2a9c0b1d4e5f60")!;
    expect(anchored.anchorX).toBe(BAR_W * 0.1);

    // 改稿调了顺序：同一条决议的节点 id 不变
    const reordered = layoutMeetingFocus(focusPayload({ decisions: [decisions[2], decisions[0], decisions[1]] }));
    expect(reordered.items.find((item) => item.id === "dec:dec-3f2a9c0b1d4e5f60")?.text).toBe("初审规则按新口径执行");
    expect(decisionNodeId({ id: undefined }, 3)).toBe("dec:3");
  });

  it("决议最多 4 个、任务最多 6 个，其余进「+N」；任务先挑没做完的", () => {
    const decisions = Array.from({ length: 7 }, (_, index) => ({ text: `决议 ${index}`, start_ms: (index + 1) * 60_000 }));
    const tasks = [
      ...Array.from({ length: 3 }, (_, index) => task(`done${index}`, 10_000 * (index + 1), { status: "done" })),
      ...Array.from({ length: 6 }, (_, index) => task(`open${index}`, 200_000 + index * 10_000)),
    ];
    const layout = layoutMeetingFocus(focusPayload({ decisions, tasks, tasks_more: 2 }));
    expect(layout.items.filter((item) => item.kind === "decision")).toHaveLength(FOCUS_DECISIONS_MAX);
    const shownTasks = layout.items.filter((item) => item.kind === "task");
    expect(shownTasks).toHaveLength(FOCUS_TASKS_MAX);
    expect(shownTasks.every((item) => item.status !== "done")).toBe(true);
    expect(layout.more.map((item) => [item.id, item.count, item.text])).toEqual([
      ["more:decisions", 3, "+3 条决议"],
      ["more:tasks", 5, "+5 条任务"],
    ]);
  });

  it("时间点挨得近的卡片错开成几排，不互相压住", () => {
    const tasks = Array.from({ length: 6 }, (_, index) => task(`t${index}`, 100_000 + index * 2_000));
    const layout = layoutMeetingFocus(focusPayload({ decisions: [], tasks }));
    const boxes = layout.items.map((item) => item.box);
    for (let i = 0; i < boxes.length; i += 1) {
      for (let j = i + 1; j < boxes.length; j += 1) expect(overlaps(boxes[i], boxes[j])).toBe(false);
    }
    expect(new Set(layout.items.map((item) => item.y)).size).toBeGreaterThan(1);
  });

  it("不知道录音多长时按最晚的时间点估，同样的数据得到同样的坐标", () => {
    const data = focusPayload({ meeting: { ...focusPayload().meeting, duration_ms: null } });
    const layout = layoutMeetingFocus(data);
    expect(layout.durationKnown).toBe(false);
    expect(layout.durationMs).toBeGreaterThanOrEqual(300_000);
    expect(layoutMeetingFocus(data)).toEqual(layout);

    // 长度和时间点都没有：条上不画刻度，免得看着像真的
    const bare = layoutMeetingFocus(
      focusPayload({ meeting: data.meeting, decisions: [{ text: "没写时间", start_ms: null }], tasks: [] }),
    );
    expect(bare.hasAnchors).toBe(false);
    expect(bare.ticks).toEqual([]);
  });
});

describe("交付物小签（3g）", () => {
  const file = (id: number, name: string, extra: Partial<{ gone: boolean; file_id: number | null }> = {}) => ({
    id,
    kind: "file",
    url: `/材料/云图AI/交付/${name}`,
    title: "",
    file_id: id,
    name,
    gone: false,
    ...extra,
  });

  it("每条任务最多画 1 个（最近标的），多的写 +N；找不到的、没有 file_id 的、链接类不画", () => {
    const tasks = [
      task("t1", 100_000, {
        deliverables: [file(1, "旧稿.pdf"), file(2, "定稿非常非常长的文件名称v3.pdf"), { id: 3, kind: "figma", url: "https://figma.com/x", title: "稿" }],
      }),
      task("t2", 300_000, { deliverables: [file(4, "没了.pdf", { gone: true }), file(5, "手填.pdf", { file_id: null })] }),
    ];
    const layout = layoutMeetingFocus(focusPayload({ tasks }));
    const first = layout.items.find((item) => item.id === "task:t1")!;
    expect(first.tag).toMatchObject({ fileId: 2, ext: "pdf", more: 1 });
    expect(first.tag!.box.w).toBeLessThanOrEqual(80);
    expect(first.tag!.box.x).toBeGreaterThan(first.box.x + first.box.w);
    expect(first.tag!.label.endsWith("…")).toBe(true);
    expect(layout.items.find((item) => item.id === "task:t2")!.tag).toBeUndefined();
  });

  it("小签算进任务卡的占位宽度：同一排里不重叠，同样的数据每次一样", () => {
    const tasks = [0, 1, 2, 3, 4, 5].map((index) =>
      task(`t${index}`, index < 3 ? 200_000 + index * 20_000 : null, { deliverables: [file(index + 1, `交付物${index}.docx`)] }),
    );
    const one = layoutMeetingFocus(focusPayload({ tasks }));
    const two = layoutMeetingFocus(focusPayload({ tasks }));
    expect(two).toEqual(one);
    const boxes = one.items.flatMap((item) => [item.box, ...(item.tag ? [item.tag.box] : [])]);
    for (let a = 0; a < boxes.length; a += 1) {
      for (let b = a + 1; b < boxes.length; b += 1) {
        expect(overlaps(boxes[a], boxes[b])).toBe(false);
      }
    }
    // 范围包进了小签
    const right = Math.max(...boxes.map((box) => box.x + box.w));
    expect(one.bounds.x + one.bounds.w).toBeGreaterThanOrEqual(right);
  });
});

describe("交付物小签的在问的产出（4e）", () => {
  const file = (id: number, name: string) => ({
    id,
    kind: "file",
    url: `/材料/云图AI/交付/${name}`,
    title: "",
    file_id: id,
    name,
    gone: false,
  });

  it("有 asks 的任务先显示在问的那一份：tag.ask 为真，文件名前加「?」，+N 数已登记的", () => {
    const tasks = [
      task("t1", 100_000, {
        deliverables: [file(1, "旧稿.pdf"), file(2, "定稿.pdf")],
        asks: [{ relation_id: 61, file_id: 930, name: "方案.key", ext: "key" }],
      }),
      task("t2", 300_000, { asks: [{ relation_id: 62, file_id: 931, name: "排期.xlsx", ext: "xlsx" }] }),
    ];
    const layout = layoutMeetingFocus(focusPayload({ tasks }), BAR_W, { asks: true });
    const first = layout.items.find((item) => item.id === "task:t1")!;
    expect(first.tag).toMatchObject({ fileId: 930, name: "方案.key", ext: "key", more: 2, ask: true });
    expect(first.tag!.label.startsWith("?")).toBe(true);
    const second = layout.items.find((item) => item.id === "task:t2")!;
    expect(second.tag).toMatchObject({ fileId: 931, more: 0, ask: true });
  });

  it("没有 linksFlags（旧后台）时不画在问的那一份，小签照旧是已登记的", () => {
    const tasks = [
      task("t1", 100_000, {
        deliverables: [file(1, "旧稿.pdf"), file(2, "定稿.pdf")],
        asks: [{ relation_id: 61, file_id: 930, name: "方案.key", ext: "key" }],
      }),
    ];
    for (const layout of [
      layoutMeetingFocus(focusPayload({ tasks })),
      layoutMeetingFocus(focusPayload({ tasks }), BAR_W, { asks: false }),
    ]) {
      const tag = layout.items.find((item) => item.id === "task:t1")!.tag!;
      expect(tag).toMatchObject({ fileId: 2, name: "定稿.pdf", more: 1 });
      expect(tag.ask).toBeUndefined();
    }
  });

  it("没有 asks（旧后台、样本）时小签不变", () => {
    const tasks = [task("t1", 100_000, { deliverables: [file(1, "定稿.pdf")] })];
    const tag = layoutMeetingFocus(focusPayload({ tasks })).items.find((item) => item.id === "task:t1")!.tag!;
    expect(tag.ask).toBeUndefined();
    expect(tag.label.startsWith("?")).toBe(false);
  });
});
