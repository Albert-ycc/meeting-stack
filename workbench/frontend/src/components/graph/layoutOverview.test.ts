import { describe, expect, it } from "vitest";

import {
  FOLDER_MAX,
  FOLDERS_HINT_ID,
  FOLDERS_MORE_ID,
  GHOST_MAX,
  GHOSTS_MORE_ID,
  ISLANDS_MORE_ID,
  folderNodeId,
  islandSize,
  layoutOverview,
  overlappingPairs,
  overviewAttention,
  type OverviewLayout,
} from "./layoutOverview";
import { foldersPayload, island, overviewPayload, suggested, unclaimed } from "./overviewFixtures";
import type { GraphOverview } from "./overviewTypes";

function positions(layout: OverviewLayout, filter: (id: string) => boolean = () => true) {
  return Object.fromEntries(
    layout.nodes.filter((node) => filter(node.id)).map((node) => [node.id, { x: node.x, y: node.y, box: node.box }]),
  );
}

/** 项目多、名字长、线多、名字和文件夹都超出上限的一份 */
function crowded(): GraphOverview {
  const names = [
    "云图AI",
    "数据中台二期建设与运营保障项目",
    "北辰仓",
    "A",
    "供应链金融风控平台升级改造",
    "云图看板",
    "品牌官网",
    "客服知识库",
    "Roadmap 2026",
    "华东区域仓储自动化一期工程",
    "财务共享",
    "会员体系",
    "直播电商",
  ];
  return overviewPayload({
    islands: names.map((name, index) =>
      island(`p${index}`, name, {
        meetings: [0, 3, 12][index % 3],
        waiting: { review: index % 2, doorstep: index % 3, tasks: 1 },
        stopped_cards: index % 4 === 0 ? 1 : 0,
      }),
    ),
    islands_more: { count: 5, project_ids: ["x1", "x2", "x3", "x4", "x5"] },
    suggested_projects: Array.from({ length: 9 }, (_, index) => suggested(index, { name: `一个很长的新项目名字${index}` })),
    bridges: [
      { a: "p0", b: "p1", count: 2 },
      { a: "p0", b: "p12", count: 1 },
      { a: "p3", b: "missing", count: 4 },
    ],
  });
}

describe("layoutOverview", () => {
  it("同样的数据得到同样的坐标", () => {
    const first = layoutOverview(crowded(), foldersPayload());
    const second = layoutOverview(structuredClone(crowded()), structuredClone(foldersPayload()));
    expect(positions(second)).toEqual(positions(first));
    expect(second.bridges).toEqual(first.bridges);
    expect(second.bounds).toEqual(first.bounds);
  });

  it("节点互不重叠（名字长、各区都满、文件夹超出上限）", () => {
    const folders = foldersPayload({
      folders: Array.from({ length: 15 }, (_, index) => unclaimed(`文件夹${index}`)),
      more: 20,
    });
    for (const layout of [
      layoutOverview(crowded(), folders),
      layoutOverview(crowded(), null),
      layoutOverview(overviewPayload(), foldersPayload()),
      layoutOverview(overviewPayload({ islands: [] }), foldersPayload({ state: "unset", folders: [], parent: null })),
    ]) {
      expect(overlappingPairs(layout)).toEqual([]);
    }
  });

  it("新项目只占末尾的空格，别的岛不动；窗口内会数变了只换大小不换位置", () => {
    const before = overviewPayload();
    const after = overviewPayload({ islands: [...before.islands, island("d", "新项目")] });
    const old = layoutOverview(before, null);
    const next = layoutOverview(after, null);
    for (const id of ["p:a", "p:b", "p:c", "harbour", "np:name1"]) {
      expect(next.byId.get(id)?.x).toBe(old.byId.get(id)?.x);
      expect(next.byId.get(id)?.y).toBe(old.byId.get(id)?.y);
    }
    const added = next.byId.get("p:d");
    expect(added?.kind === "island" && added.slot).toBe(3);

    const busier = overviewPayload({
      islands: before.islands.map((item) => ({ ...item, meetings: item.meetings + 10 })),
    });
    const grown = layoutOverview(busier, null);
    for (const id of ["p:a", "p:b", "p:c"]) {
      expect(grown.byId.get(id)?.x).toBe(old.byId.get(id)?.x);
      expect(grown.byId.get(id)?.y).toBe(old.byId.get(id)?.y);
    }
    expect([islandSize(0), islandSize(1), islandSize(5), islandSize(6)]).toEqual([0, 1, 1, 2]);
  });

  it("文件夹晚到时别的岛不动，只多出右手那一列", () => {
    const data = crowded();
    const early = layoutOverview(data, null);
    const late = layoutOverview(data, foldersPayload());
    expect(positions(late, (id) => !id.startsWith("fd:") && !id.startsWith("folders:"))).toEqual(positions(early));
    const folderNodes = late.nodes.filter((node) => node.region === "folders");
    expect(folderNodes).toHaveLength(2);
    const islandRight = Math.max(
      ...late.nodes.filter((node) => node.region === "islands").map((node) => node.box.x + node.box.w),
    );
    for (const node of folderNodes) expect(node.box.x).toBeGreaterThan(islandRight);
  });

  it("20 个像新项目的名字只画 6 个，其余收成「还有 14 个像新项目的名字」", () => {
    const layout = layoutOverview(
      overviewPayload({ suggested_projects: Array.from({ length: 20 }, (_, index) => suggested(index)) }),
      null,
    );
    const ghosts = layout.nodes.filter((node) => node.kind === "ghost");
    expect(ghosts).toHaveLength(GHOST_MAX);
    expect(ghosts.map((node) => node.id)).toEqual(["np:name0", "np:name1", "np:name2", "np:name3", "np:name4", "np:name5"]);
    const more = layout.byId.get(GHOSTS_MORE_ID);
    expect(more?.label).toBe("还有 14 个像新项目的名字");
    expect(overlappingPairs(layout)).toEqual([]);
  });

  it("文件夹最多 12 个，其余「还有 N 个」；总文件夹没设时是提示岛；id 不带中文路径", () => {
    const layout = layoutOverview(
      overviewPayload(),
      foldersPayload({ folders: Array.from({ length: 12 }, (_, index) => unclaimed(`文件夹${index}`)), more: 3 }),
    );
    const folders = layout.nodes.filter((node) => node.kind === "folder");
    expect(folders).toHaveLength(FOLDER_MAX);
    for (const node of folders) expect(node.id).toMatch(/^fd:[0-9a-z]+$/);
    expect(layout.byId.get(FOLDERS_MORE_ID)?.label).toBe("还有 3 个文件夹没挂到项目");
    expect(folderNodeId("/Volumes/资料盘/项目/云图AI")).toBe(folderNodeId("/Volumes/资料盘/项目/云图AI"));
    expect(folderNodeId("/Volumes/资料盘/项目/云图AI")).not.toBe(folderNodeId("/Volumes/资料盘/项目/云图AI二期"));

    const unset = layoutOverview(overviewPayload(), foldersPayload({ state: "unset", parent: null, folders: [] }));
    expect(unset.byId.get(FOLDERS_HINT_ID)?.label).toBe("设项目总文件夹后，这里会列出还没挂的文件夹");
  });

  it("跨项目的线两头都画出来才画；没项目时是空状态；超过 40 个项目折成一个节点", () => {
    const layout = layoutOverview(crowded(), null);
    expect(layout.bridges.map((bridge) => bridge.id)).toEqual(["br:p0:p1", "br:p0:p12"]);
    expect(layout.byId.get(ISLANDS_MORE_ID)?.label).toBe("其余 5 个项目（窗口内没有会）");

    const empty = layoutOverview(overviewPayload({ islands: [], bridges: [] }), null);
    expect(empty.nodes.map((node) => node.kind)).toContain("empty");
  });

  it("N 键顺序：有在等你或停了的项目岛，再是港湾，再是幽灵岛", () => {
    const layout = layoutOverview(overviewPayload(), null);
    expect(overviewAttention(layout)).toEqual(["p:a", "p:c", "harbour", "np:name1"]);
  });
});
