import { describe, expect, it } from "vitest";

import { EDGE_DRAW_MAX, FOCUS_EDGE_MAX, drawableEdges, drawnEdges, drawnNeighbours, edgeStyleKind } from "./drawnEdges";
import type { GraphEdge } from "./graphTypes";

function edge(id: string, kind: string, from: string, to: string, extra: Partial<GraphEdge> = {}): GraphEdge {
  return { id, kind: kind as GraphEdge["kind"], from, to, label: id, ...extra };
}

describe("drawnEdges", () => {
  it("碰到焦点的线最多 12 条，按 15 级顺序取，多的给面板", () => {
    const edges: GraphEdge[] = [
      edge("e:folder:1", "folder", "file:1", "r:x"),
      edge("e:cue:1", "cue", "cue:1", "file:1", { count: 2 }),
      edge("e:cue:2", "cue", "cue:2", "file:1", { count: 9 }),
      edge("e:disc:1", "discussion", "m:a", "file:1"),
      edge("e:dlv:1", "deliverable", "r:1", "file:1"),
      edge("e:rel:1", "related", "m:b", "file:1"),
      edge("e:aff:1", "affects", "m:c", "file:1", { state: "ask" }),
      edge("e:prod:1", "produced", "r:2", "file:1", { state: "ask" }),
      edge("e:file:1:a", "mentioned", "m:a", "file:1"),
      edge("e:file:1:b", "mentioned", "m:b", "file:1", { relation_id: 7 }),
      edge("e:attr:x", "attribution", "file:1", "project", { state: "review" }),
      edge("e:nr:x", "suggested", "m:d", "file:1"),
      edge("e:write:x", "write", "file:1", "cards"),
      edge("e:cross:x", "cross", "file:1", "b:q"),
      edge("e:attr:y", "attribution", "file:1", "project"),
    ];
    const { drawn, hidden } = drawnEdges(edges, "file:1", null);
    const order = [
      "e:aff:1",
      "e:prod:1",
      "e:attr:x",
      "e:nr:x",
      "e:file:1:a",
      "e:file:1:b",
      "e:dlv:1",
      "e:disc:1",
      "e:attr:y",
      "e:rel:1",
      "e:write:x",
      "e:cue:2",
    ];
    expect(drawn.map((item) => item.id).sort()).toEqual([...order].sort());
    expect(drawn).toHaveLength(FOCUS_EDGE_MAX);
    expect(hidden.map((item) => item.id)).toEqual(["e:cue:1", "e:cross:x", "e:folder:1"]);
  });

  it("选中的那条线排第一，同级的会议一端新的先", () => {
    const edges = Array.from({ length: 14 }, (_, index) =>
      edge(`e:file:1:m${index}`, "mentioned", `m:m${index}`, "file:1", { meeting_id: `m${index}` }),
    );
    const ages = new Map(edges.map((item, index) => [item.meeting_id!, 14 - index]));
    const { drawn, hidden } = drawnEdges(edges, "file:1", "e:file:1:m0", { meetingAge: ages });
    expect(drawn.map((item) => item.id)).toContain("e:file:1:m0");
    expect(hidden.map((item) => item.id)).toEqual(["e:file:1:m2", "e:file:1:m1"]);
  });

  it("neighbours 只看画出来的线", () => {
    const edges = Array.from({ length: 15 }, (_, index) => edge(`e:disc:${index}`, "discussion", `m:${index}`, "r:1"));
    const { drawn } = drawnEdges(edges, "r:1", null);
    const near = drawnNeighbours(drawn, "r:1");
    expect(near.size).toBe(1 + FOCUS_EDGE_MAX);
    // 按边 id 排：e:disc:7、8、9 排在最后没画
    expect(near.has("m:9")).toBe(false);
    expect(near.has("m:14")).toBe(true);
  });

  it("40 个节点、每类都满、相关开着：一直画的线不超过 150 条，先去相关、讨论、放宽的提到", () => {
    const edges: GraphEdge[] = [];
    for (let index = 0; index < 18; index += 1) {
      for (let slot = 0; slot < 3; slot += 1) {
        edges.push(edge(`e:file:${slot}:${index}`, "mentioned", `m:${index}`, `file:${slot + index}`, slot === 2 ? { relation_id: index } : {}));
      }
      for (let slot = 0; slot < 2; slot += 1) edges.push(edge(`e:rel:${index}-${slot}`, "related", `m:${index}`, `file:${index + 20 + slot}`));
    }
    for (let index = 0; index < 30; index += 1) edges.push(edge(`e:disc:${index}`, "discussion", `m:${index % 18}`, `r:${index % 10}`));
    for (let index = 0; index < 8; index += 1) edges.push(edge(`e:aff:${index}`, "affects", `m:${index}`, `file:${40 + index}`, { state: "ask" }));
    for (let index = 0; index < 20; index += 1) edges.push(edge(`e:dlv:${index}`, "deliverable", `r:${index % 10}`, `file:${index}`));
    for (let index = 0; index < 8; index += 1) edges.push(edge(`e:folder:${index}`, "folder", `r:${index}`, `rf:${index}`));
    for (let index = 0; index < 18; index += 1) edges.push(edge(`e:attr:${index}`, "attribution", `m:${index}`, "project"));
    const { drawn } = drawnEdges(edges, null, null);
    expect(drawn.length).toBeLessThanOrEqual(EDGE_DRAW_MAX);
    // 156 条：只去掉 6 条相关（会议旧的先去），讨论、放宽的提到不动
    expect(drawn).toHaveLength(EDGE_DRAW_MAX);
    expect(drawn.filter((item) => item.kind === "related")).toHaveLength(30);
    expect(drawn.filter((item) => item.kind === "discussion")).toHaveLength(30);
    // 在问的线和交付物一条不少
    expect(drawn.filter((item) => item.kind === "affects")).toHaveLength(8);
    expect(drawn.filter((item) => item.kind === "deliverable")).toHaveLength(20);
  });

  it("认不出的种类按讨论线处理；两端不在图上的、［提到］关着时的提到线先滤掉", () => {
    expect(edgeStyleKind("future_kind")).toBe("discussion");
    const edges = [
      edge("e:file:1:a", "mentioned", "m:a", "file:1"),
      edge("e:dlv:1", "deliverable", "r:1", "file:9"),
      edge("e:x:1", "future_kind", "m:a", "r:1"),
    ];
    const has = (id: string) => id !== "file:9";
    expect(drawableEdges(edges, has).map((item) => item.id)).toEqual(["e:file:1:a", "e:x:1"]);
    expect(drawableEdges(edges, has, true).map((item) => item.id)).toEqual(["e:x:1"]);
  });
});
