import { describe, expect, it } from "vitest";

import type { LocalGraph, LocalNode, TracePayload } from "./graphTypes";
import { COL_W, ROW_Y, layoutLocal } from "./localLayout";
import { overlaps } from "./layout";

function at(dayOfMonth: number, hour = 10) {
  return new Date(2026, 8, dayOfMonth, hour, 0, 0).toISOString();
}

function mapPayload(nodes: LocalNode[]): LocalGraph {
  return {
    center: { id: "file:812", kind: "file", file_id: 812, name: "报价单 v3.xlsx", at: at(18) },
    nodes,
    edges: nodes.map((node) => ({ id: `e:${node.id}`, kind: "mentioned", from: node.id, to: "file:812", label: "" })),
    hidden: [],
    hidden_count: 0,
  };
}

describe("layoutLocal", () => {
  it("中心在 x = 0，决议在它的会那一列，一列 4 个、共 13 个节点不重叠，结果确定", () => {
    const nodes: LocalNode[] = [
      ...[1, 2, 3, 4].map((index) => ({ id: `m:a${index}`, kind: "meeting" as const, title: `周会${index}`, at: at(21, 9 + index) })),
      { id: "dec:d1", kind: "decision", text: "总价下调 5%", meeting_id: "a1", at: at(21, 11) },
      { id: "dec:d2", kind: "decision", text: "总价下调 3%", meeting_id: "a2", at: at(21, 12) },
      { id: "task:t1", kind: "task", title: "写一版方案", meeting_id: "a1", at: at(21, 10) },
      { id: "task:t2", kind: "task", title: "整理清单", meeting_id: "a3", at: at(21, 13) },
      { id: "m:old", kind: "meeting", title: "报价沟通", at: at(9) },
      { id: "file:790", kind: "file", file_id: 790, name: "报价单 v2.xlsx", at: at(9) },
      { id: "file:791", kind: "file", file_id: 791, name: "报价单 v1.xlsx", at: at(2) },
      { id: "r:r-2", kind: "requirement", title: "能耗看板" },
    ];
    const payload = mapPayload(nodes);
    const layout = layoutLocal(payload);
    expect(layout.nodes).toHaveLength(13);
    expect(layout.byId.get("file:812")).toMatchObject({ x: 0, y: ROW_Y.file, center: true });
    expect(layout.byId.get("dec:d1")!.x).toBe(layout.byId.get("m:a1")!.x);
    expect(layout.byId.get("task:t2")!.x).toBe(layout.byId.get("m:a3")!.x);
    expect(layout.byId.get("m:a1")!.x).toBe(COL_W);
    expect(layout.byId.get("file:790")!.x).toBe(-COL_W);
    expect(layout.byId.get("r:r-2")).toMatchObject({ x: 0, y: ROW_Y.requirement });
    for (const a of layout.nodes) {
      for (const b of layout.nodes) {
        if (a.id !== b.id) expect(overlaps(a.box, b.box), `${a.id} × ${b.id}`).toBe(false);
      }
    }
    expect(layoutLocal(payload)).toEqual(layout);
    expect(layout.viewKey).toBe("local:file:812");
  });

  it("来龙去脉的列跟 chain", () => {
    const trace: TracePayload = {
      center: { id: "file:812", kind: "file", file_id: 812, name: "报价单 v3.xlsx", at: at(18) },
      nodes: [
        { id: "file:790", kind: "file", file_id: 790, name: "报价单 v2.xlsx", at: at(9) },
        { id: "m:m-77", kind: "meeting", title: "周会", at: at(14) },
        { id: "task:t-19", kind: "task", title: "写一版方案", meeting_id: "m-77", at: at(14, 11) },
        { id: "dec:d", kind: "decision", text: "总价下调 5%", meeting_id: "m-81", at: at(21) },
      ],
      edges: [],
      chain: ["file:790", "m:m-77", "task:t-19", "file:812", "dec:d"],
      center_index: 3,
      cut: { back: false, forward: false },
    };
    const layout = layoutLocal(trace);
    expect(trace.chain.map((id) => layout.byId.get(id)!.x)).toEqual([-3, -2, -1, 0, 1].map((step) => step * COL_W));
    expect(layout.viewKey).toBe("local:trace:file:812");
  });
});
