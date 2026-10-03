import { describe, expect, it } from "vitest";

import {
  BELT_ID,
  BELT_MIN,
  DOCK_FOLDER_MAX,
  DOCK_GHOST_MAX,
  FOLDERS_HINT_ID,
  FOLDERS_MORE_ID,
  FOLDERS_STATUS_ID,
  GHOSTS_MORE_ID,
  HARBOUR_ID,
  RING_NAMES,
  folderNodeId,
  ghostText,
  islandAge,
  islandCountText,
  layoutOverview,
  monthDay,
  overviewAttention,
  planetRadius,
  ringIndex,
  sunCountText,
  type IslandNode,
  type OverviewLayout,
} from "./layoutOverview";
import { TODAY, crowdedOverview, daysAgo, foldersPayload, island, overviewPayload, suggested, unclaimed } from "./overviewFixtures";
import { DEG } from "./overviewProjection";
import type { GraphOverview } from "./overviewTypes";

/** 每颗行星落在哪：圈、名次、轨道角、径向抖动、在不在带子里 */
function placement(layout: OverviewLayout) {
  return Object.fromEntries(layout.islands.map((node) => [node.id, [node.ring, node.rank, node.theta, node.rj, node.belt]]));
}

function planet(layout: OverviewLayout, id: string): IslandNode {
  const node = layout.byId.get(id);
  if (node?.kind !== "island") throw new Error(`${id} 不是项目`);
  return node;
}

/** count 个都在 28 天以前开过会的项目（全落在最外圈），一个比一个旧 */
function farOnly(count: number, overrides: Partial<GraphOverview> = {}): GraphOverview {
  return overviewPayload({
    islands: Array.from({ length: count }, (_, index) =>
      island(`f${index}`, `远处的项目${index}`, { last_day: daysAgo(30 + index), meetings: 0 }),
    ),
    suggested_projects: [],
    bridges: [],
    ...overrides,
  });
}

const degrees = (theta: number) => theta / DEG;

describe("layoutOverview", () => {
  it("按最近一次会离今天多久落圈：0–6 天第一圈，7–27 天第二圈，28 天以上或没开过会第三圈", () => {
    expect([0, 6, 7, 27, 28, 400].map(ringIndex)).toEqual([0, 0, 1, 1, 2, 2]);
    expect(ringIndex(null)).toBe(2);
    expect(islandAge({ last_day: "2026-09-20" }, "2026-09-27")).toBe(7);
    expect(islandAge({ last_day: null }, TODAY)).toBeNull();
    expect(islandAge({ last_day: "不是日期" }, TODAY)).toBeNull();

    const ages = [0, 6, 7, 27, 28, null];
    const layout = layoutOverview(
      overviewPayload({
        islands: ages.map((age, index) => island(`p${index}`, `项目${index}`, { last_day: age === null ? null : daysAgo(age) })),
        bridges: [],
      }),
      null,
    );
    expect(ages.map((_, index) => planet(layout, `p:p${index}`).ring)).toEqual([0, 0, 1, 1, 2, 2]);
    expect(layout.rings.map((ring) => [ring.name, ring.count])).toEqual([
      [RING_NAMES[0], 2],
      [RING_NAMES[1], 2],
      [RING_NAMES[2], 2],
    ]);
  });

  it("同一圈里最近开过会的在圈头（第二圈的圈头是正前方），往两侧交替逐个变旧；一样新的场次总数多的在前，再按名字", () => {
    const layout = layoutOverview(
      overviewPayload({
        islands: [
          island("yi", "乙", { last_day: daysAgo(10), meetings_total: 5 }),
          island("old", "最旧", { last_day: daysAgo(20) }),
          island("jia", "甲", { last_day: daysAgo(10), meetings_total: 9 }),
          island("mid", "中间", { last_day: daysAgo(12) }),
          island("bing", "丙", { last_day: daysAgo(10), meetings_total: 5 }),
        ],
        bridges: [],
      }),
      null,
    );
    const order = [...layout.islands].sort((a, b) => a.rank - b.rank).map((node) => node.id);
    // 甲 场次最多；乙、丙 一样新一样多，按名字（丙 bing 在 乙 yi 前）
    expect(order).toEqual(["p:jia", "p:bing", "p:yi", "p:mid", "p:old"]);
    // 五个项目、正后方留 32° 写圈名：相邻两个隔 82°，圈头 90° 是正前方，1 号在右手（+），2 号在左手（−）
    const at = (id: string) => degrees(planet(layout, id).theta);
    expect(at("p:jia")).toBeCloseTo(90);
    expect(at("p:bing")).toBeCloseTo(90 + 82);
    expect(at("p:yi")).toBeCloseTo(90 - 82);
    expect(at("p:mid")).toBeCloseTo(90 + 164);
    expect(at("p:old")).toBeCloseTo(90 - 164);

    // 第一圈的圈头偏右前 18°，相邻最多隔 52°
    const inner = layoutOverview(
      overviewPayload({
        islands: [island("a", "前天", { last_day: daysAgo(2) }), island("b", "昨天", { last_day: daysAgo(1) })],
        bridges: [],
      }),
      null,
    );
    expect(degrees(planet(inner, "p:b").theta)).toBeCloseTo(72);
    expect(degrees(planet(inner, "p:a").theta)).toBeCloseTo(72 + 52);
  });

  it("角度只看最近一次会：换时间窗（窗口里的场次变了）圈、名次、角度都不变", () => {
    const data = crowdedOverview();
    const before = layoutOverview(data, null);
    const busier = layoutOverview(
      { ...data, window: { effective: "90d", days: 90 }, islands: data.islands.map((item) => ({ ...item, meetings: item.meetings + 7 })) },
      null,
    );
    expect(placement(busier)).toEqual(placement(before));
  });

  it("同样的数据每次得到完全一样的结果，小行星带的抖动也一样", () => {
    const first = layoutOverview(crowdedOverview(), foldersPayload());
    const second = layoutOverview(structuredClone(crowdedOverview()), structuredClone(foldersPayload()));
    expect(placement(second)).toEqual(placement(first));
    expect([...second.byId.keys()]).toEqual([...first.byId.keys()]);
    expect(second.rings).toEqual(first.rings);
  });

  it(`最外圈 ${BELT_MIN} 个以下不成带，${BELT_MIN + 1} 个成带；在等你的不进带子，仍是行星；带子的径向抖动在 ±24 以内`, () => {
    expect(BELT_MIN).toBe(12);
    const twelve = layoutOverview(farOnly(12), null);
    expect(twelve.rings[2].belt).toBe(false);
    expect(twelve.belt).toBeNull();
    expect(twelve.islands.some((node) => node.belt)).toBe(false);
    expect(twelve.islands.every((node) => node.rj === 0)).toBe(true);

    const data = farOnly(13);
    data.islands[4] = { ...data.islands[4], waiting: { review: 1, doorstep: 0, tasks: 0 } };
    const thirteen = layoutOverview(data, null);
    expect(thirteen.rings[2].belt).toBe(true);
    expect(thirteen.belt).toMatchObject({ id: BELT_ID, label: "更早或没开过会的 13 个项目，打开列表", data: { count: 13, folded: [] } });
    expect(thirteen.belt?.data.projects.map((item) => item.id)).toEqual(data.islands.map((item) => item.id));
    const waiting = planet(thirteen, "p:f4");
    expect([waiting.belt, waiting.rj]).toEqual([false, 0]);
    const particles = thirteen.islands.filter((node) => node.belt);
    expect(particles).toHaveLength(12);
    for (const node of particles) expect(Math.abs(node.rj)).toBeLessThanOrEqual(24);
    expect(new Set(particles.map((node) => node.rj)).size).toBeGreaterThan(1);
  });

  it("服务器折起来的项目（islands_more）并进小行星带：数量加进标签和项目数；折了就一定有带子", () => {
    const layout = layoutOverview(farOnly(2, { islands_more: { count: 5, project_ids: ["x1", "x2", "x3", "x4", "x5"] } }), null);
    expect(layout.belt).toMatchObject({ label: "更早或没开过会的 7 个项目，打开列表", data: { count: 7, folded: ["x1", "x2", "x3", "x4", "x5"] } });
    expect(layout.rings[2]).toMatchObject({ count: 7, belt: true });
    expect(layout.projectCount).toBe(7);
  });

  it("太阳上的场次、最近一次会、项目数", () => {
    const layout = layoutOverview(overviewPayload(), null);
    expect(layout.meetings).toBe(9);
    expect(layout.latestAge).toBe(2);
    expect(layout.projectCount).toBe(3);
    const never = layoutOverview(overviewPayload({ islands: [island("n", "没开过", { last_day: null, meetings: 0 })] }), null);
    expect(never.latestAge).toBeNull();
    const empty = layoutOverview(overviewPayload({ islands: [], bridges: [] }), null);
    expect([empty.islands, empty.belt, empty.meetings, empty.projectCount]).toEqual([[], null, 0, 0]);
  });

  it("读屏的句子和几处写法：窗口里的场次、在等你几件、停了几张卡片", () => {
    const layout = layoutOverview(overviewPayload(), null);
    expect(planet(layout, "p:a").label).toBe("项目：云图AI，28 天 7 场，2 件在等你");
    expect(planet(layout, "p:b").label).toBe("项目：数据中台，28 天 2 场");
    expect(planet(layout, "p:c").label).toBe("项目：北辰仓，28 天 0 场，2 张卡片停了");
    const all = layoutOverview(overviewPayload({ window: { effective: "all", days: null } }), null);
    expect(planet(all, "p:a").label).toBe("项目：云图AI，全部 7 场，2 件在等你");

    expect([islandCountText(28, 7), islandCountText(null, 7)]).toEqual(["28 天 7 场", "全部 7 场"]);
    expect([sunCountText(7, 2), sunCountText(null, 61)]).toEqual(["7 天 2 场会", "全部 61 场会"]);
    expect(monthDay("2026-09-03")).toBe("9月3日");
    expect(ghostText(suggested(1, { name: "云图看板", meeting_count: 2 }))).toBe("像是新项目『云图看板』· 2 场会");
    // 光点大小：窗口里没会的最小，有会的按场次的平方根长
    expect(planetRadius(0)).toBeLessThan(planetRadius(1));
    expect(planetRadius(4) - planetRadius(1)).toBeCloseTo(3.9);
  });

  it(`港湾一定有；像新项目的名字最多列 ${DOCK_GHOST_MAX} 个，其余收成「还有 N 个」`, () => {
    const layout = layoutOverview(
      overviewPayload({ suggested_projects: Array.from({ length: 20 }, (_, index) => suggested(index)) }),
      null,
    );
    expect(layout.byId.get(HARBOUR_ID)?.label).toBe("港湾：9 场没归项目的会");
    const ghosts = layout.nodes.filter((node) => node.kind === "ghost");
    expect(ghosts.map((node) => node.id)).toEqual(["np:name0", "np:name1", "np:name2"]);
    expect(layout.byId.get(GHOSTS_MORE_ID)?.label).toBe("还有 17 个像新项目的名字");

    const noAi = layoutOverview(
      overviewPayload({ harbour: { ...overviewPayload().harbour, ai_configured: false } }),
      null,
    );
    expect(noAi.byId.get(HARBOUR_ID)?.label).toBe("港湾：没配置 AI，9 场会要你自己选项目");
  });

  it(`没挂的文件夹最多列 ${DOCK_FOLDER_MAX} 个，其余「还有 N 个」；没设总文件夹是提示；在看、没连上、冲突时是一句状态`, () => {
    const many = layoutOverview(
      overviewPayload(),
      foldersPayload({ folders: Array.from({ length: 12 }, (_, index) => unclaimed(`文件夹${index}`)), more: 3 }),
    );
    const listed = many.nodes.filter((node) => node.kind === "folder");
    expect(listed.map((node) => node.label)).toEqual([0, 1, 2, 3].map((index) => `没挂到项目的文件夹：文件夹${index}`));
    for (const node of listed) expect(node.id).toMatch(/^fd:[0-9a-z]+$/);
    expect(many.byId.get(FOLDERS_MORE_ID)?.label).toBe("还有 11 个文件夹没挂到项目");
    expect(folderNodeId("/Volumes/资料盘/项目/云图AI")).toBe(folderNodeId("/Volumes/资料盘/项目/云图AI"));
    expect(folderNodeId("/Volumes/资料盘/项目/云图AI")).not.toBe(folderNodeId("/Volumes/资料盘/项目/云图AI二期"));

    const statusOf = (folders: Parameters<typeof foldersPayload>[0]) =>
      layoutOverview(overviewPayload(), foldersPayload({ folders: [], ...folders })).byId.get(FOLDERS_STATUS_ID)?.label;
    expect(
      layoutOverview(overviewPayload(), foldersPayload({ state: "unset", parent: null, folders: [] })).byId.get(FOLDERS_HINT_ID)?.label,
    ).toBe("设项目总文件夹后，这里会列出还没挂的文件夹");
    expect(statusOf({ state: "checking" })).toBe("正在看项目总文件夹下面有哪些文件夹…");
    expect(statusOf({ state: "offline", parent: { path: "/x", state: "missing", reason: null } })).toBe("找不到项目总文件夹了");
    expect(statusOf({ state: "offline", parent: { path: "/x", state: "unreadable", reason: null } })).toBe("读不了项目总文件夹");
    expect(statusOf({ state: "offline", parent: { path: "/x", state: "offline", reason: null } })).toBe(
      "资料盘未连接，插上后再列下面的文件夹",
    );
    expect(statusOf({ state: "conflict", parent: { path: "/x", state: "conflict", reason: "和别的项目的文件夹重了" } })).toBe(
      "和别的项目的文件夹重了",
    );
    expect(statusOf({ state: "conflict", parent: null })).toBe("项目总文件夹现在用不了");
    // 都挂好了：右上角一个节点都不出
    expect(layoutOverview(overviewPayload(), foldersPayload({ folders: [] })).nodes.some((node) => node.region === "folders")).toBe(false);
  });

  it("文件夹晚到时只多出右上角那几个节点，盘面上什么都不动", () => {
    const early = layoutOverview(crowdedOverview(), null);
    const late = layoutOverview(crowdedOverview(), foldersPayload());
    expect(early.nodes.some((node) => node.region === "folders")).toBe(false);
    expect(late.nodes.filter((node) => node.region === "folders")).toHaveLength(2);
    expect(placement(late)).toEqual(placement(early));
  });

  it("跨项目的线两头都在图上才有", () => {
    const layout = layoutOverview(
      overviewPayload({
        bridges: [
          { a: "a", b: "b", count: 2 },
          { a: "a", b: "missing", count: 4 },
        ],
      }),
      null,
    );
    expect(layout.bridges).toEqual([{ id: "br:a:b", a: "p:a", b: "p:b", count: 2 }]);
  });

  it("N 键顺序：在等你或停了卡片的项目（里圈往外、同一圈按名次），再是有待你选、像新项目的港湾，再是像新项目的名字", () => {
    // 三个项目同一天开会、场次总数一样，按名字排：北辰仓（停了卡片）、数据中台、云图AI（在等你）
    expect(overviewAttention(layoutOverview(overviewPayload(), null))).toEqual(["p:c", "p:a", HARBOUR_ID, "np:name1"]);

    const layout = layoutOverview(
      overviewPayload({
        islands: [
          island("far", "很久以前", { last_day: daysAgo(40), waiting: { review: 1, doorstep: 0, tasks: 0 } }),
          island("near", "这周", { last_day: daysAgo(1), waiting: { review: 0, doorstep: 0, tasks: 2 } }),
          island("quiet", "没事的", { last_day: daysAgo(3) }),
        ],
        harbour: { ...overviewPayload().harbour, counts: { ai_pending: 2, needs_review: 0, none: 7, new_project: 0 } },
        suggested_projects: [],
        bridges: [],
      }),
      null,
    );
    expect(overviewAttention(layout)).toEqual(["p:near", "p:far"]);
  });

  it("150 个项目：三圈各 2、32、116 个，最外圈成带；带子里是没有在等你的那些", () => {
    const layout = layoutOverview(crowdedOverview(), null);
    expect(layout.rings.map((ring) => ring.count)).toEqual([2, 32, 116]);
    expect(layout.belt?.data.count).toBe(116);
    const particles = layout.islands.filter((node) => node.belt);
    expect(particles).toHaveLength(116);
    expect(particles.every((node) => node.waiting === 0 && node.ring === 2)).toBe(true);
    expect(layout.projectCount).toBe(150);
  });
});
