import { describe, expect, it } from "vitest";

import {
  RECENT_MAX,
  RELATED_DRAW_MAX,
  RELATED_FILES_MAX,
  VISIBLE_BUDGET,
  fileMark,
  visibleCount,
  withRecentFiles,
  withRelatedEdges,
  type PinnedFile,
} from "./graphFiles";
import type { GraphFile, GraphFolder, GraphPayload, GraphRootsPayload, RecentFile, RelatedEdges } from "./graphTypes";
import { layoutStarMap } from "./layout";
import { meeting as meetingNode, payload } from "./testFixtures";

function recent(fileId: number, name = `文件${fileId}.docx`, state: RecentFile["state"] = "done"): RecentFile {
  return { file_id: fileId, name, ext: "docx", dir_rel: "方案", mtime: "2026-09-25T10:00:00+00:00", state };
}

function rootsWith(rootFiles: RecentFile[], folderFiles: Record<string, RecentFile[]> = {}): GraphRootsPayload {
  return {
    roots: [
      { id: "root:1", root_id: 1, path: "/材料/云图AI", state: "online", loose_count: 0, checked_at: null, recent_files: rootFiles },
    ],
    folders: Object.entries(folderFiles).map(([id, files], index) => ({
      id,
      folder_id: index + 1,
      requirement_id: `r${index + 1}`,
      path: `/材料/云图AI/需求${index + 1}`,
      state: "online" as const,
      root_id: 1,
      recent_files: files,
    })),
    loose: { count: 0, recent: [] },
    checking: false,
  };
}

function requirementFolder(id: string): GraphFolder {
  return { id, kind: "requirement_folder", name: id, path: `/材料/云图AI/${id}`, ring: "middle", requirement_id: "r1", folder_id: 1 };
}

function mentioned(fileId: number): GraphFile {
  return { id: `file:${fileId}`, kind: "file", file_id: fileId, name: `提到${fileId}.xlsx`, ext: "xlsx", rel_path: "x", root_id: 1, folder: "root:1" };
}

const recentNames = (graph: GraphPayload) => (graph.files ?? []).filter((file) => file.recent).map((file) => file.name);

describe("withRecentFiles", () => {
  it("每个文件夹从候选里取前 3 个，按文件夹轮流取，挂一条细线回文件夹", () => {
    const graph = payload({ folders: [...payload().folders, requirementFolder("rf:1")] });
    const roots = rootsWith([recent(1), recent(2), recent(3), recent(4)], { "rf:1": [recent(11), recent(12)] });

    const next = withRecentFiles(graph, roots, null);

    expect(recentNames(next)).toEqual(["文件1.docx", "文件11.docx", "文件2.docx", "文件12.docx", "文件3.docx"]);
    const edge = next.edges.find((item) => item.to === "file:11")!;
    expect(edge).toMatchObject({ kind: "folder", from: "rf:1", label: "" });
    expect(next.files?.find((file) => file.file_id === 11)).toMatchObject({ folder: "rf:1", rel_path: "方案/文件11.docx", recent: true });
  });

  it("画布上已有的文件（会上提到的、从简报补出来的）不再补，也不占名额", () => {
    const graph = payload({ files: [mentioned(1), { ...mentioned(2), extra: true }] });
    const next = withRecentFiles(graph, rootsWith([recent(1), recent(2), recent(3), recent(4), recent(5)]), null);
    expect(recentNames(next)).toEqual(["文件3.docx", "文件4.docx", "文件5.docx"]);
    expect((next.files ?? []).filter((file) => file.file_id === 1)).toHaveLength(1);
  });

  it("全图最多 12 个；8 个文件夹、12 个被提到的文件时可见节点不超过 40", () => {
    const folders = Array.from({ length: 8 }, (_, index) => requirementFolder(`rf:${index + 1}`));
    const perFolder = Object.fromEntries(
      folders.map((folder, index) => [folder.id, [1, 2, 3].map((n) => recent(100 * (index + 1) + n))]),
    );
    const roomy = payload({ folders: [...payload().folders, ...folders] });
    const many = withRecentFiles(roomy, rootsWith([], perFolder), null);
    expect(recentNames(many)).toHaveLength(Math.min(RECENT_MAX, VISIBLE_BUDGET - visibleCount(roomy)));
    expect(visibleCount(many)).toBeLessThanOrEqual(VISIBLE_BUDGET);

    const crowded = payload({
      folders: [...payload().folders, ...folders],
      files: Array.from({ length: 12 }, (_, index) => mentioned(900 + index)),
    });
    const next = withRecentFiles(crowded, rootsWith([], perFolder), null);
    expect(visibleCount(next)).toBeLessThanOrEqual(VISIBLE_BUDGET);
    // 轮流取：先每个文件夹 1 个
    const firsts = recentNames(next).slice(0, 8);
    expect(new Set(firsts.map((name) => Math.floor(Number(name.replace(/\D/g, "")) / 100))).size).toBe(firsts.length);
  });

  it("名额为 0 时不补；没有资料盘状态时只补点出来的那一个", () => {
    const full = payload({ files: Array.from({ length: VISIBLE_BUDGET }, (_, index) => mentioned(500 + index)) });
    expect(withRecentFiles(full, rootsWith([recent(1)]), null)).toBe(full);

    const pinned: PinnedFile = { file_id: 42, name: "定稿.pdf", rel_path: "交付/定稿.pdf", root_id: 1, folder: "root:1" };
    const next = withRecentFiles(payload(), null, pinned);
    expect(next.files).toEqual([
      expect.objectContaining({ id: "file:42", pinned: true, ext: "pdf", folder: "root:1" }),
    ]);
  });

  it("点出来的文件先放，挤掉一个最近的文件；它本来就是最近的文件时只画一次，带上状态", () => {
    // 只剩 2 个名额
    const graph = payload({ files: Array.from({ length: 29 }, (_, index) => mentioned(500 + index)) });
    const room = VISIBLE_BUDGET - visibleCount(graph);
    expect(room).toBe(2);
    const candidates = Array.from({ length: 6 }, (_, index) => recent(index + 1));
    const roots = rootsWith(candidates);
    expect(recentNames(withRecentFiles(graph, roots, null))).toHaveLength(Math.min(3, room));

    const pinned: PinnedFile = { file_id: 2, name: "文件2.docx", rel_path: "方案/文件2.docx", root_id: 1, folder: "root:1" };
    const next = withRecentFiles(graph, rootsWith(candidates.map((item) => (item.file_id === 2 ? { ...item, state: "waiting" } : item))), pinned);
    const drawn = (next.files ?? []).filter((file) => file.file_id === 2);
    expect(drawn).toHaveLength(1);
    expect(drawn[0]).toMatchObject({ pinned: true, state: "waiting" });
    expect(recentNames(next)).toHaveLength(Math.min(3, room - 1));
  });
});

describe("最近的文件落位", () => {
  it("补出最近的文件不挪已有节点，读屏名是「最近改过的文件」，补出来的文件优先占空槽", () => {
    const graph = payload({ files: [mentioned(7)] });
    const before = layoutStarMap(graph);
    const withRecent = withRecentFiles(graph, rootsWith([recent(1), recent(2), recent(3)]), null);
    const after = layoutStarMap(withRecent);
    for (const node of before.nodes) {
      const moved = after.byId.get(node.id)!;
      expect({ id: node.id, x: moved.x, y: moved.y }).toEqual({ id: node.id, x: node.x, y: node.y });
    }
    expect(after.byId.get("file:1")?.label).toBe("最近改过的文件：文件1.docx");

    // 选中会议时从简报补出来的文件先占空槽，最近的文件让位
    const extra = { ...mentioned(8), extra: true as const };
    const both = layoutStarMap({ ...withRecent, files: [...(withRecent.files ?? []), extra] });
    const extraAlone = layoutStarMap({ ...graph, files: [...(graph.files ?? []), extra] });
    expect({ x: both.byId.get("file:8")!.x, y: both.byId.get("file:8")!.y }).toEqual({
      x: extraAlone.byId.get("file:8")!.x,
      y: extraAlone.byId.get("file:8")!.y,
    });
  });

  it("点出来的文件排在最近的文件之前放", () => {
    const pinned: PinnedFile = { file_id: 42, name: "定稿.pdf", rel_path: "交付/定稿.pdf", root_id: 1, folder: "root:1" };
    const layout = layoutStarMap(withRecentFiles(payload(), rootsWith([recent(1)]), pinned));
    const pinnedNode = layout.byId.get("file:42")!;
    const recentNode = layout.byId.get("file:1")!;
    expect(layout.nodes.indexOf(pinnedNode)).toBeLessThan(layout.nodes.indexOf(recentNode));
    expect(pinnedNode.label).toBe("文件：定稿.pdf");
  });
});

describe("fileMark", () => {
  it("在等系统画灰色 ◷，在等你画琥珀色 ◷，读不了画 ⊘，读完了不画", () => {
    expect(fileMark("pending")).toMatchObject({ symbol: "◷", tone: "system", text: "还没读到" });
    expect(fileMark("pending", true)).toMatchObject({ symbol: "◷", tone: "system", text: "资料盘未连接" });
    expect(fileMark("waiting")).toMatchObject({ symbol: "◷", tone: "you" });
    expect(fileMark("unreadable")).toMatchObject({ symbol: "⊘", tone: "unreadable" });
    expect(fileMark("done")).toBeNull();
    expect(fileMark("names_only")).toBeNull();
    expect(fileMark(undefined)).toBeNull();
  });
});

describe("withRelatedEdges（4f）", () => {
  function related(edges: Array<Partial<RelatedEdges["edges"][number]> & { meeting_id: string; file_id: number }>): RelatedEdges {
    const files: RelatedEdges["files"] = {};
    for (const item of edges) {
      files[String(item.file_id)] = { file_id: item.file_id, name: `相关${item.file_id}.docx`, ext: "docx", rel_path: "x", root_id: 1 };
    }
    return {
      rev: 1,
      files,
      edges: edges.map((item, index) => ({
        id: `e:rel:${index + 1}`,
        relation_id: index + 1,
        rank: index + 1,
        words: ["报价单", "驻场"],
        at_ms: 1000,
        quote: "报价单再看一下",
        passage: { content_key: "k", ordinal: 0, loc: "第 1 页" },
        ...item,
      })),
    };
  }

  it("有提到线的一对跳过；新文件带 related；线上写共同词", () => {
    const graph = payload({
      files: [mentioned(1)],
      edges: [{ id: "e:file:1:a", kind: "mentioned", from: "m:a", to: "file:1", label: "" }],
    });
    const next = withRelatedEdges(graph, related([{ meeting_id: "a", file_id: 1 }, { meeting_id: "a", file_id: 2 }]));
    const lines = next.edges.filter((edge) => edge.kind === "related");
    expect(lines.map((edge) => edge.to)).toEqual(["file:2"]);
    expect(lines[0]).toMatchObject({ id: "e:rel:2", from: "m:a", label: "共同词：报价单、驻场", relation_id: 2 });
    expect(next.files?.find((file) => file.file_id === 2)).toMatchObject({ related: true, folder: "root:1" });
  });

  it("每个节点（含折叠组）最多 3 条，折叠的会对到组；全图最多 36 条；新文件最多 6 个且不超过空位", () => {
    const graph = payload({
      files: Array.from({ length: 10 }, (_, index) => mentioned(100 + index)),
      collapsed: [{ id: "c:older", kind: "older", label: "更早 2 场", count: 2, from: "2026-01-01", to: "2026-02-01", meeting_ids: ["x", "y"], ring: "outer" }],
    });
    const rows = [
      ...Array.from({ length: 5 }, (_, index) => ({ meeting_id: "a", file_id: 100 + index })),
      ...Array.from({ length: 4 }, (_, index) => ({ meeting_id: index % 2 ? "x" : "y", file_id: 105 + index })),
      ...Array.from({ length: 10 }, (_, index) => ({ meeting_id: "b", file_id: 500 + index })),
    ];
    const next = withRelatedEdges(graph, related(rows));
    const lines = next.edges.filter((edge) => edge.kind === "related");
    const per = (id: string) => lines.filter((edge) => edge.from === id || edge.to === id).length;
    expect(per("m:a")).toBe(3);
    expect(per("c:older")).toBe(3);
    expect(per("m:b")).toBe(3);
    expect(lines.length).toBeLessThanOrEqual(RELATED_DRAW_MAX);
    const added = (next.files ?? []).filter((file) => file.related);
    expect(added.length).toBeLessThanOrEqual(RELATED_FILES_MAX);
    expect(visibleCount(next)).toBeLessThanOrEqual(VISIBLE_BUDGET);
    expect(withRelatedEdges(graph, null)).toBe(graph);
  });

  it("全图最多 36 条", () => {
    const meetings = Array.from({ length: 16 }, (_, index) => meetingNode(`q${index}`, index));
    const graph = payload({ meetings, files: Array.from({ length: 20 }, (_, index) => mentioned(700 + index)) });
    const rows = meetings.flatMap((item, index) =>
      [0, 1, 2].map((slot) => ({ meeting_id: item.meeting_id, file_id: 700 + ((index + slot * 7) % 20) })),
    );
    const next = withRelatedEdges(graph, related(rows));
    expect(next.edges.filter((edge) => edge.kind === "related").length).toBeLessThanOrEqual(RELATED_DRAW_MAX);
  });
});
