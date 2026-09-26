import { describe, expect, it } from "vitest";

import type { GraphPayload } from "./graphTypes";
import { RING_GUIDES, attentionOrder, layoutStarMap, overlaps, textWidth, fitText, nearestInDirection } from "./layout";
import { day, meeting, payload, requirement } from "./testFixtures";

/** 「7 天内 6 个需求、10 场会」，外加门口、折叠、满额的文件夹和线索词、两个信标 */
function pressure(): GraphPayload {
  const meetings = Array.from({ length: 10 }, (_, index) =>
    meeting(`m${index}`, Math.floor(index * 0.7), {
      title: `很长很长的会议标题用来测试标签会不会互相压住 ${index}`,
      ring: index < 8 ? "inner" : "middle",
    }),
  );
  return payload({
    meetings,
    collapsed: [
      { id: "c:older", kind: "older", label: "更早 12 场", count: 12, from: "2026-05-01", to: "2026-08-28", meeting_ids: [], ring: "outer" },
    ],
    doorstep: [0, 1, 2].map((index) => ({
      id: `d:door${index}`,
      meeting_id: `door${index}`,
      title: `门口的会 ${index}`,
      date: day(index),
      age_days: index,
      candidates: [
        { project_id: "p", project_name: "云图AI", project_color: "#2c8d83", count: 2 },
        { project_id: "q", project_name: "数据中台", project_color: "#7a5af8", count: 1 },
      ],
      reason: "",
    })),
    doorstep_more: 2,
    requirements: Array.from({ length: 6 }, (_, index) =>
      requirement(`q${index}`, { title: `七天内活跃的需求名字挺长 ${index}`, priority: "P0" }),
    ),
    requirements_more: { id: "r:more", count: 3, requirement_ids: ["x", "y", "z"] },
    folders: [
      { id: "root:1", kind: "root", name: "云图AI项目资料总目录", path: "/a", ring: "inner", root_id: 1 },
      { id: "cards", kind: "cards", name: "声档会议记录/", path: "/a/b", ring: "inner", written: 8, stopped: 1, waiting: 0, enabled: true },
      ...Array.from({ length: 6 }, (_, index) => ({
        id: `rf:${index}`,
        kind: "requirement_folder" as const,
        name: `需求文件夹的名字也可能很长 ${index}`,
        path: `/a/${index}`,
        ring: "middle" as const,
        folder_id: index,
        requirement_id: `q${index}`,
      })),
    ],
    folders_more: { id: "f:more", count: 2, paths: ["/x", "/y"] },
    cues: ["初审规则", "驻场排班", "白名单", "能耗看板", "数理学会", "报价单", "月度汇报", "巡检"].map((text, index) => ({
      id: `cue:${index}`,
      text,
      source: "term" as const,
      term_id: `t${index}`,
      total: 12 - index,
      meetings: [],
    })),
    beacons: [
      { id: "b:q", project_id: "q", project_name: "数据中台", project_color: "#7a5af8", count: 2, label: "→ 数据中台 ×2", items: [] },
      { id: "b:x", project_id: "x", project_name: "其他项目", project_color: "#f79009", count: 1, label: "→ 其他项目 ×1", items: [] },
    ],
  });
}

function inside(x: number, y: number, rx: number, ry: number) {
  return (x / rx) ** 2 + (y / ry) ** 2 <= 1;
}

describe("layoutStarMap", () => {
  it("同样的输入两次坐标完全相同", () => {
    const first = layoutStarMap(pressure());
    const second = layoutStarMap(pressure());
    expect(first.nodes.map(({ id, x, y }) => [id, x, y])).toEqual(second.nodes.map(({ id, x, y }) => [id, x, y]));
  });

  it("每个节点落在自己的方向和圈内", () => {
    const layout = layoutStarMap(pressure());
    const [inner, middle] = RING_GUIDES;
    for (const node of layout.nodes) {
      if (node.kind === "meeting" || node.kind === "collapsed" || node.kind === "doorstep") expect(node.x).toBeLessThan(0);
      if (node.kind === "folder" || node.kind === "loose" || node.kind === "folder_more") expect(node.x).toBeGreaterThan(0);
      if (node.kind === "requirement" || node.kind === "requirement_more") expect(node.box.y + node.box.h).toBeLessThan(-44);
      if (node.kind === "cue" || node.kind === "beacon") expect(node.box.y).toBeGreaterThan(44);
      if ((node.kind === "meeting" || node.kind === "folder") && node.ring === "inner") {
        expect(inside(node.x, node.y, inner.rx, inner.ry)).toBe(true);
      }
      if ((node.kind === "meeting" || node.kind === "folder") && node.ring === "middle") {
        expect(inside(node.x, node.y, inner.rx, inner.ry)).toBe(false);
        expect(inside(node.x, node.y, middle.rx, middle.ry)).toBe(true);
      }
      if (node.kind === "collapsed") expect(inside(node.x, node.y, middle.rx, middle.ry)).toBe(false);
    }
  });

  it("任意两个节点连同外伸的标签互不重叠", () => {
    for (const graph of [payload(), pressure()]) {
      const { nodes } = layoutStarMap(graph);
      for (let i = 0; i < nodes.length; i += 1) {
        for (let j = i + 1; j < nodes.length; j += 1) {
          if (overlaps(nodes[i].box, nodes[j].box)) {
            throw new Error(`${nodes[i].id} 和 ${nodes[j].id} 重叠`);
          }
        }
      }
    }
  });

  it("压力夹具里 6 个需求第一排放 4 个、其余顺延到外一排，全部上图", () => {
    const layout = layoutStarMap(pressure());
    const requirements = layout.nodes.filter((node) => node.kind === "requirement");
    expect(requirements).toHaveLength(6);
    expect(requirements.filter((node) => node.ring === "inner")).toHaveLength(4);
    expect(requirements.filter((node) => node.ring === "middle")).toHaveLength(2);
    expect(layout.byId.get("r:more")?.ring).toBe("outer");
    expect(layout.nodes.filter((node) => node.kind === "meeting")).toHaveLength(10);
  });

  it("往会议那一侧加一场会，其他方向不动，别的会也不挪", () => {
    const before = layoutStarMap(pressure());
    const graph = pressure();
    graph.meetings = [...graph.meetings.slice(0, 7), meeting("new", 6), ...graph.meetings.slice(7)];
    const after = layoutStarMap(graph);
    for (const node of before.nodes) {
      if (node.kind === "meeting") continue;
      const moved = after.byId.get(node.id);
      expect([moved?.x, moved?.y]).toEqual([node.x, node.y]);
    }
    const small = layoutStarMap(payload());
    const more = payload();
    more.meetings = [...more.meetings, meeting("d", 4)];
    const bigger = layoutStarMap(more);
    for (const id of ["m:a", "m:b", "m:c"]) {
      expect(bigger.byId.get(id)?.y).toBe(small.byId.get(id)?.y);
    }
  });

  it("最新的会在最上面；可见区只框住中心、内圈、中圈、门口和线索词", () => {
    const layout = layoutStarMap(pressure());
    const inner = layout.nodes.filter((node) => node.kind === "meeting" && node.ring === "inner");
    const ages = [...inner].sort((a, b) => a.y - b.y).map((node) => (node.kind === "meeting" ? node.data.age_days : -1));
    expect(ages).toEqual([...ages].sort((a, b) => a - b));
    const older = layout.byId.get("c:older")!;
    expect(older.box.x).toBeLessThan(layout.focusBounds.x);
  });

  it("按字宽截断会议标题，方向键找屏幕上最近的节点", () => {
    expect(textWidth("初审ab")).toBe(26 + 15);
    expect(fitText("一二三四五六七八九十", 60)).toBe("一二三四…");
    const layout = layoutStarMap(payload());
    const center = layout.byId.get("project")!;
    expect(nearestInDirection(layout.nodes, center, "ArrowLeft")?.kind).toBe("meeting");
    expect(nearestInDirection(layout.nodes, center, "ArrowRight")?.kind).toBe("folder");
    expect(nearestInDirection(layout.nodes, center, "ArrowUp")!.y).toBeLessThan(0);
    const top = layout.byId.get("r:r1")!;
    expect(nearestInDirection(layout.nodes, top, "ArrowUp")).toBeNull();
    expect(nearestInDirection(layout.nodes, center, "ArrowDown")?.kind).toBe("cue");
  });

  it("改走的会留下的残影占着原来的槽位，别的会不挪；窗口外的不画", () => {
    const before = layoutStarMap(pressure());
    const graph = pressure();
    const [moved, ...rest] = graph.meetings;
    const after = layoutStarMap({
      ...graph,
      meetings: rest,
      moved_out: [
        { meeting_id: moved.meeting_id, title: moved.title, date: moved.date, age_days: moved.age_days, to_project_id: "q", to_project_name: "数据中台", undo_until: "2099-01-01T00:00:00Z" },
        { meeting_id: "old", title: "很早的会", date: day(40), age_days: 40, to_project_id: "q", to_project_name: "数据中台", undo_until: "2099-01-01T00:00:00Z" },
      ],
    });
    const ghost = after.byId.get(`g:${moved.meeting_id}`)!;
    expect(ghost.kind).toBe("ghost");
    expect([ghost.x, ghost.y]).toEqual([before.byId.get(moved.id)!.x, before.byId.get(moved.id)!.y]);
    for (const other of rest) {
      expect([after.byId.get(other.id)!.x, after.byId.get(other.id)!.y]).toEqual([before.byId.get(other.id)!.x, before.byId.get(other.id)!.y]);
    }
    expect(after.byId.has("g:old")).toBe(false);
    // 残影连同「点一下撤销」也不压别的节点
    for (const node of after.nodes) {
      if (node.id !== ghost.id && node.direction === "left") expect(overlaps(node.box, ghost.box)).toBe(false);
    }
  });

  it("N 的顺序：门口、待复核或待确认或卡片停了的会、有待确认任务的需求", () => {
    const graph = payload({
      meetings: [
        meeting("a", 0),
        meeting("b", 1, { pending_tasks: 2 }),
        meeting("c", 3, { card: "stopped" }),
        meeting("d", 9, { ring: "middle", state: "needs_review" }),
      ],
      doorstep: pressure().doorstep.slice(0, 1),
      requirements: [requirement("r1", { pending_tasks: 1 }), requirement("r2")],
    });
    const order = attentionOrder(layoutStarMap(graph));
    expect(order[0]).toBe("d:door0");
    expect(order.slice(1, 4).sort()).toEqual(["m:b", "m:c", "m:d"]);
    expect(order.slice(4)).toEqual(["r:r1"]);
  });

  it("最近改过的子文件夹排在外圈最后，资料盘状态晚到也不挤动别的材料", () => {
    const base = payload({ loose: { id: "loose", kind: "loose", name: "散放文件", ring: "outer", count: 3 } });
    const before = layoutStarMap(base);
    const withSub = layoutStarMap({
      ...base,
      folders: [
        ...base.folders,
        { id: "sub:1:初审规则", kind: "subfolder", name: "初审规则", path: "/材料/云图AI/初审规则", ring: "outer", root_id: 1, dir: "初审规则" },
      ],
    });
    const sub = withSub.byId.get("sub:1:初审规则")!;
    expect(sub.direction).toBe("right");
    expect(sub.ring).toBe("outer");
    for (const node of before.nodes) {
      expect([withSub.byId.get(node.id)!.x, withSub.byId.get(node.id)!.y]).toEqual([node.x, node.y]);
    }
  });
});

