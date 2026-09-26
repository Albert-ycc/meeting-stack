import { describe, expect, it } from "vitest";

import { BAR_W, FOCUS_DECISIONS_MAX, FOCUS_TASKS_MAX, layoutMeetingFocus } from "./focusLayout";
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
