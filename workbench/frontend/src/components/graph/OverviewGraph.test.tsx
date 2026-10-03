import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { api, type ApiClient } from "../../api";
import type { Project } from "../../types";
import { layoutOverview, type IslandNode } from "./layoutOverview";
import { NAME_BUDGET, rectOverlap, type Rect } from "./overviewLabels";
import { OverviewGraph, forgetOverviewCache } from "./OverviewGraph";
import { crowdedOverview, daysAgo, foldersPayload, island, overviewPayload } from "./overviewFixtures";
import { headingDegrees, turnToFront } from "./overviewProjection";
import type { GraphOverview, GraphOverviewFetch, OverviewFolders } from "./overviewTypes";

const PROJECTS: Project[] = [
  { id: "a", name: "云图AI", color: "#2c8d83" },
  { id: "b", name: "数据中台", color: "#7a5af8" },
  { id: "c", name: "北辰仓", color: "#c2410c" },
] as Project[];

const ETAG = 'W/"o-1"';

function makeClient(
  overview: GraphOverview = overviewPayload(),
  folders: OverviewFolders = foldersPayload(),
  overrides: Record<string, unknown> = {},
) {
  const undoUntil = new Date(Date.now() + 10 * 60_000).toISOString();
  return {
    getGraphOverview: vi.fn(async (): Promise<GraphOverviewFetch> => ({ overview, etag: ETAG })),
    getOverviewFolders: vi.fn(async () => folders),
    meetings: vi.fn(async () => ({
      items: [
        {
          id: "m1",
          title: "门口的会",
          project_id: null,
          recording_date: "2026-09-25T10:00:00",
          created_at: "2026-09-25T10:00:00",
          candidates: [
            { project_id: "a", project_name: "云图AI", count: 2, llm: true, current: false },
            { project_id: "b", project_name: "数据中台", count: 1, llm: false, current: false },
          ],
        },
      ],
      total: 1,
      limit: 100,
      offset: 0,
    })),
    tasks: vi.fn(async () => ({
      items: [{ id: "t1", title: "补报价单", meeting_title: "周会", status: "pending_confirm" }],
      total: 1,
      limit: 50,
      offset: 0,
      counts: {},
    })),
    updateMeeting: vi.fn(async () => ({ effects: { tasks_moved: 0, tasks_left: [], undo_until: undoUntil } })),
    undoMeetingProject: vi.fn(async () => ({})),
    confirmMeetingProject: vi.fn(async () => ({})),
    confirmTask: vi.fn(async () => ({})),
    rejectTask: vi.fn(async () => ({})),
    claimFolders: vi.fn(async () => ({
      items: [{ path: "/Volumes/资料盘/项目/云图看板", ok: true, project_name: "云图看板", cards_written: 0 }],
      needs_review: 0,
    })),
    declineFolder: vi.fn(async () => ({ ok: true })),
    undeclineFolder: vi.fn(async () => ({ ok: true })),
    ...overrides,
  } as unknown as ApiClient & Record<string, ReturnType<typeof vi.fn>>;
}

interface Handlers {
  onOpenProjectGraph?: (projectId: string) => void;
  onOpenLibrary?: (filter: "none" | "new_project") => void;
}

function Harness({
  apiClient,
  initial = null,
  handlers = {},
}: {
  apiClient: ApiClient;
  initial?: string | null;
  handlers?: Handlers;
}) {
  const [selection, setSelection] = useState<string | null>(initial);
  return (
    <>
      <OverviewGraph
        apiClient={apiClient}
        onOpenLibrary={handlers.onOpenLibrary ?? (() => {})}
        onOpenMeeting={() => {}}
        onOpenProject={() => {}}
        onOpenProjectGraph={handlers.onOpenProjectGraph ?? (() => {})}
        onOpenRequirement={() => {}}
        onSelectionChange={setSelection}
        projects={PROJECTS}
        selection={selection}
      />
      <span data-testid="selection">{selection ?? ""}</span>
    </>
  );
}

function panel() {
  return screen.findByRole("complementary", { name: "详情面板" });
}

// ---------- jsdom 不排版：让星图真的摆一遍名字 ----------

const CJK = /[⺀-鿿＀-￯　-〿]/;

function textWidth(text: string, size: number) {
  let width = 0;
  for (const ch of text) width += CJK.test(ch) ? size : /[A-Z]/.test(ch) ? size * 0.64 : /\s/.test(ch) ? size * 0.3 : size * 0.55;
  return width;
}

/** 星图里几种元素的尺寸，按字数估（名字 13px、说明 12px，行高 1.32） */
function estimate(element: HTMLElement): { w: number; h: number } {
  if (element.classList.contains("star-label")) {
    const html = element.querySelector(".star-label__name")?.innerHTML ?? "";
    const lines = html.split(/<br\s*\/?>/i).map((line) => line.replace(/<[^>]+>/g, ""));
    return { w: Math.max(...lines.map((line) => textWidth(line, 13))) + 11, h: lines.length * 17.2 + 2 };
  }
  if (element.classList.contains("star-badge")) return { w: Math.max(18, 10 + 7.2 * (element.textContent ?? "").length), h: 18 };
  if (element.classList.contains("star-chip")) return { w: textWidth(element.textContent ?? "", 12) + 32, h: 22 };
  if (element.classList.contains("star-sun")) return { w: 92, h: 34 };
  if (element.classList.contains("star-card")) return { w: 236, h: 150 };
  return { w: 0, h: 0 };
}

/** 舞台 1162×716（1440×900 下的大小），名字、琥珀数、圈名按字数给尺寸；返回还原的函数 */
function stageSize() {
  const stubs = [
    vi.spyOn(Element.prototype, "clientWidth", "get").mockImplementation(function (this: Element) {
      return this.classList.contains("overview-viewport") ? 1162 : 0;
    }),
    vi.spyOn(Element.prototype, "clientHeight", "get").mockImplementation(function (this: Element) {
      return this.classList.contains("overview-viewport") ? 716 : 0;
    }),
    vi.spyOn(HTMLElement.prototype, "offsetWidth", "get").mockImplementation(function (this: HTMLElement) {
      return estimate(this).w;
    }),
    vi.spyOn(HTMLElement.prototype, "offsetHeight", "get").mockImplementation(function (this: HTMLElement) {
      return estimate(this).h;
    }),
  ];
  return () => stubs.forEach((stub) => stub.mockRestore());
}

/** 系统设了「减少动态」：转盘、飞入都不放动画 */
function reduceMotion() {
  const original = window.matchMedia;
  window.matchMedia = ((query: string) => ({
    matches: query.includes("prefers-reduced-motion"),
    media: query,
    onchange: null,
    addEventListener: () => {},
    removeEventListener: () => {},
    addListener: () => {},
    removeListener: () => {},
    dispatchEvent: () => false,
  })) as unknown as typeof window.matchMedia;
  return () => {
    window.matchMedia = original;
  };
}

/** 一个元素在舞台里的框：场景写进 transform 的位置 + 估的尺寸 */
function boxOf(element: HTMLElement): Rect {
  const match = /translate\(([-\d.]+)px, ([-\d.]+)px\)/.exec(element.style.transform);
  const x = match ? Number(match[1]) : 0;
  const y = match ? Number(match[2]) : 0;
  const { w, h } = estimate(element);
  return { x0: x, y0: y, x1: x + w, y1: y + h };
}

const shownLabels = (root: HTMLElement) =>
  Array.from(root.querySelectorAll<HTMLElement>(".star-label")).filter((element) => !element.classList.contains("is-off"));

/** 看得见的名字两两不重叠，也不压圈名、琥珀数、太阳的字；都在舞台里 */
function expectNoOverlap(root: HTMLElement) {
  const labels = shownLabels(root).map(boxOf);
  const others = [
    ...Array.from(root.querySelectorAll<HTMLElement>(".star-chip:not(.is-gone)")),
    ...Array.from(root.querySelectorAll<HTMLElement>(".star-badge:not(.is-gone)")),
    ...Array.from(root.querySelectorAll<HTMLElement>(".star-sun:not(.is-gone)")),
  ].map(boxOf);
  labels.forEach((box, index) => {
    for (const other of labels.slice(index + 1)) expect(rectOverlap(box, other)).toBe(0);
    for (const other of others) expect(rectOverlap(box, other)).toBe(0);
    expect(box.x0).toBeGreaterThanOrEqual(0);
    expect(box.y0).toBeGreaterThanOrEqual(0);
    expect(box.x1).toBeLessThanOrEqual(1162);
    expect(box.y1).toBeLessThanOrEqual(716);
  });
}

/** 舞台里每一处字都不小于 12px（读的是样式表算出来的字号） */
function expectReadableText(viewport: HTMLElement) {
  for (const element of Array.from(viewport.querySelectorAll<HTMLElement>("*"))) {
    const own = Array.from(element.childNodes).some((node) => node.nodeType === Node.TEXT_NODE && node.textContent?.trim());
    if (!own) continue;
    const size = parseFloat(getComputedStyle(element).fontSize || "16");
    expect(size, `${element.className}「${element.textContent?.slice(0, 12)}」`).toBeGreaterThanOrEqual(12);
  }
}

const readout = (root: HTMLElement) => root.querySelector(".star-readout")?.textContent ?? "";

beforeEach(() => {
  forgetOverviewCache();
  window.localStorage.clear();
});

afterEach(() => {
  vi.useRealTimers();
});

describe("OverviewGraph", () => {
  it("先说正在画，再画出港湾、项目、琥珀数、像新项目的名字、没挂的文件夹和跨项目的线", async () => {
    const apiClient = makeClient();
    const { container } = render(<Harness apiClient={apiClient} />);
    expect(screen.getByText("正在画关系图…")).toBeInTheDocument();

    const canvas = await screen.findByRole("application", { name: "全部项目关系图" });
    expect(apiClient.getGraphOverview).toHaveBeenCalledWith("28d", null);
    // 星图：太阳写窗口内项目上的会；三圈都画、各有圈名（三个项目都是这周开过会的，落在第一圈）
    expect(within(canvas).getByRole("note", { name: "全部项目：28 天 9 场会" })).toHaveTextContent("全部项目28 天 9 场会");
    expect(Array.from(container.querySelectorAll(".star-chip")).map((chip) => chip.textContent)).toEqual([
      "R17 天内",
      "R228 天内",
      "R3更早或没开过会",
    ]);
    expect(within(canvas).getByRole("button", { name: "项目：云图AI，28 天 7 场，2 件在等你" })).toBeInTheDocument();
    expect(within(canvas).getByRole("button", { name: "云图AI：2 件在等你，打开面板" })).toHaveAttribute(
      "title",
      "1 场可能是这个项目的、1 条任务待确认",
    );
    // 窗口里没会的项目写几天前开过会；停了卡片的名字后面挂 ⊘
    const quiet = within(canvas).getByRole("button", { name: /^项目：北辰仓，28 天 0 场/ });
    expect(quiet).toHaveTextContent("北辰仓2 天前⊘");
    expect(within(quiet).getByText("⊘")).toHaveAttribute("title", "2 张卡片停了");

    const harbour = within(canvas).getByRole("button", { name: "港湾：9 场没归项目的会" });
    expect(harbour).toHaveTextContent("等 AI 判断 2 · 待你选 3 · AI 没认出 2 · 像新项目 2");
    // 像新项目的名字在港湾下面；［建成项目］［建成需求］在点开的面板里
    await userEvent.click(within(canvas).getByRole("button", { name: "像是新项目『云图看板』· 2 场会" }));
    const side = await panel();
    expect(within(side).getByRole("button", { name: "建成项目" })).toBeInTheDocument();
    expect(within(side).getByRole("button", { name: "建成需求" })).toBeInTheDocument();

    expect(await within(canvas).findByRole("button", { name: "没挂到项目的文件夹：旧资料" })).toBeInTheDocument();
    expect(container.querySelectorAll(".star-bridge")).toHaveLength(1);
    expect(within(canvas).getByText("×2")).toBeInTheDocument();
    // 时间窗
    expect(screen.getByRole("button", { name: "28 天" })).toHaveAttribute("aria-pressed", "true");
  });

  it("每 30 秒带 etag 对一次：304 时照旧画手上的那份，变了就换", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const first = overviewPayload();
    const changed = overviewPayload({ islands: [...first.islands, island("d", "新来的项目")] });
    const getGraphOverview = vi
      .fn()
      .mockResolvedValueOnce({ overview: first, etag: ETAG })
      .mockResolvedValueOnce({ overview: null, etag: ETAG })
      .mockResolvedValueOnce({ overview: changed, etag: 'W/"o-2"' });
    const apiClient = makeClient(first, foldersPayload(), { getGraphOverview });
    render(<Harness apiClient={apiClient} />);
    await screen.findByRole("button", { name: /^项目：云图AI/ });

    await act(async () => {
      await vi.advanceTimersByTimeAsync(30_000);
    });
    expect(getGraphOverview).toHaveBeenCalledTimes(2);
    expect(getGraphOverview).toHaveBeenLastCalledWith("28d", ETAG);
    expect(screen.getByRole("button", { name: /^项目：云图AI/ })).toBeInTheDocument();
    expect(screen.queryByText("正在画关系图…")).not.toBeInTheDocument();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(30_000);
    });
    expect(await screen.findByRole("button", { name: /^项目：新来的项目/ })).toBeInTheDocument();
  });

  it("换时间窗记住选择，按新窗口取", async () => {
    const apiClient = makeClient();
    render(<Harness apiClient={apiClient} />);
    await screen.findByRole("application", { name: "全部项目关系图" });
    await userEvent.click(screen.getByRole("button", { name: "90 天" }));
    await waitFor(() => expect(apiClient.getGraphOverview).toHaveBeenLastCalledWith("90d", null));
    expect(window.localStorage.getItem("meeting-workbench:graph:window.overview")).toBe("90d");
  });

  it("项目岛面板：门口的会［归这里］、待确认任务［确认］，作答后那一行消失、重取概览", async () => {
    const apiClient = makeClient();
    render(<Harness apiClient={apiClient} />);
    await userEvent.click(await screen.findByRole("button", { name: "云图AI：2 件在等你，打开面板" }));
    const side = await panel();
    expect(within(side).getByRole("heading", { name: "云图AI" })).toBeInTheDocument();
    expect(screen.getByTestId("selection")).toHaveTextContent("p:a");

    const row = await within(side).findByRole("group", { name: "门口的会 归哪个项目" });
    expect(within(row).getByRole("button", { name: "归 数据中台" })).toBeInTheDocument();
    await userEvent.click(within(row).getByRole("button", { name: "归这里" }));
    expect(apiClient.updateMeeting).toHaveBeenCalledWith("m1", { project_id: "a" });
    await waitFor(() => expect(within(side).queryByRole("group", { name: "门口的会 归哪个项目" })).not.toBeInTheDocument());
    expect(await screen.findByText("已归到 云图AI")).toBeInTheDocument();
    await waitFor(() => expect(vi.mocked(apiClient.getGraphOverview).mock.calls.length).toBeGreaterThanOrEqual(2));

    await userEvent.click(within(side).getByRole("button", { name: "确认" }));
    expect(apiClient.confirmTask).toHaveBeenCalledWith("t1");
    await waitFor(() => expect(within(side).queryByText("补报价单")).not.toBeInTheDocument());
    expect(await screen.findByText("已确认「补报价单」")).toBeInTheDocument();
  });

  it("项目岛面板：全库待复核的会超过 100 场时，这个项目的待复核和门口的会一场不少", async () => {
    // 按后端的口径造库：/api/meetings 按 project_id、attribution 过滤，按日期倒序，limit 默认 100
    type Row = { id: string; title: string; project_id: string | null; recording_date: string; created_at: string; candidates: unknown[] };
    const day = (n: number) => `2026-09-2${n}T10:00:00`;
    const rows: Row[] = [];
    // 别的项目 150 场，日期都比云图AI 的新
    for (let i = 0; i < 150; i += 1) rows.push({ id: `b${i}`, title: `数据中台的会${i}`, project_id: "b", recording_date: "2026-09-26T12:00:00", created_at: "2026-09-26T12:00:00", candidates: [] });
    for (let i = 0; i < 3; i += 1) rows.push({ id: `ar${i}`, title: `云图待复核${i}`, project_id: "a", recording_date: day(i), created_at: day(i), candidates: [] });
    const doorCandidate = { project_id: "a", project_name: "云图AI", count: 1, llm: false, current: false };
    for (let i = 0; i < 2; i += 1) rows.push({ id: `ad${i}`, title: `云图门口${i}`, project_id: null, recording_date: day(i), created_at: day(i), candidates: [doorCandidate] });
    const meetings = vi.fn(async (filters: { project_id?: string; limit?: number } = {}) => {
      const hit = rows
        .filter((row) => !filters.project_id || (filters.project_id === "none" ? row.project_id === null : row.project_id === filters.project_id))
        .sort((x, y) => y.recording_date.localeCompare(x.recording_date));
      const limit = Math.min(filters.limit ?? 100, 500);
      return { items: hit.slice(0, limit), total: hit.length, limit, offset: 0 };
    });
    const overview = overviewPayload({
      islands: [island("a", "云图AI", { meetings: 7, waiting: { review: 3, doorstep: 2, tasks: 0 } }), island("b", "数据中台", { color: "#7a5af8", meetings: 2 })],
    });
    render(<Harness apiClient={makeClient(overview, foldersPayload(), { meetings })} />);
    await userEvent.click(await screen.findByRole("button", { name: "云图AI：5 件在等你，打开面板" }));
    const side = await panel();

    for (const title of ["云图待复核0", "云图待复核1", "云图待复核2", "云图门口0", "云图门口1"]) {
      expect(await within(side).findByRole("group", { name: `${title} 归哪个项目` })).toBeInTheDocument();
    }
    expect(within(side).queryByText("都处理好了")).not.toBeInTheDocument();
  });

  it("港湾面板：待你选那一行用资料库的组件，选了那一行消失；底部进资料库", async () => {
    const onOpenLibrary = vi.fn();
    const apiClient = makeClient();
    render(<Harness apiClient={apiClient} handlers={{ onOpenLibrary }} />);
    await userEvent.click(await screen.findByRole("button", { name: "港湾：9 场没归项目的会" }));
    const side = await panel();
    const strip = within(side).getByRole("group", { name: "没归项目的会 h1 选项目" });
    await userEvent.click(within(strip).getByRole("button", { name: "数据中台" }));
    expect(apiClient.updateMeeting).toHaveBeenCalledWith("h1", { project_id: "b" });
    await waitFor(() => expect(within(side).queryByRole("group", { name: "没归项目的会 h1 选项目" })).not.toBeInTheDocument());
    expect(within(side).getByText(/没归项目的会 h2/)).toBeInTheDocument();

    await userEvent.click(within(side).getByRole("button", { name: "在资料库里看全部" }));
    expect(onOpenLibrary).toHaveBeenCalledWith("none");
  });

  it("灰色岛：［不是项目］走 2a 的接口，岛从画布上消失，提示里能撤销；［建成项目］走认领", async () => {
    const apiClient = makeClient();
    render(<Harness apiClient={apiClient} />);
    await userEvent.click(await screen.findByRole("button", { name: "没挂到项目的文件夹：旧资料" }));
    let side = await panel();
    expect(within(side).getByText("/Volumes/资料盘/项目/旧资料")).toBeInTheDocument();
    await userEvent.click(within(side).getByRole("button", { name: "不是项目" }));
    expect(apiClient.declineFolder).toHaveBeenCalledWith("/Volumes/资料盘/项目/旧资料");
    await waitFor(() =>
      expect(screen.queryByRole("button", { name: "没挂到项目的文件夹：旧资料" })).not.toBeInTheDocument(),
    );
    expect(screen.queryByRole("complementary", { name: "详情面板" })).not.toBeInTheDocument();
    const notice = screen.getByText("『旧资料』不算项目，以后不再列出").closest(".action-banner") as HTMLElement;
    await userEvent.click(within(notice).getByRole("button", { name: "撤销" }));
    expect(apiClient.undeclineFolder).toHaveBeenCalledWith("/Volumes/资料盘/项目/旧资料");
    expect(await screen.findByRole("button", { name: "没挂到项目的文件夹：旧资料" })).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "没挂到项目的文件夹：云图看板" }));
    side = await panel();
    await userEvent.click(within(side).getByRole("button", { name: "建成项目" }));
    expect(apiClient.claimFolders).toHaveBeenCalledWith([{ path: "/Volumes/资料盘/项目/云图看板", action: "create" }]);
    expect(await screen.findByText("已建成项目『云图看板』，挂上了 /Volumes/资料盘/项目/云图看板")).toBeInTheDocument();
  });

  it("聚焦过的文件夹被藏起以后，这一列的 Tab 停靠点退回到剩下的第一个", async () => {
    render(<Harness apiClient={makeClient()} />);
    await userEvent.click(await screen.findByRole("button", { name: "没挂到项目的文件夹：旧资料" }));
    await userEvent.click(within(await panel()).getByRole("button", { name: "不是项目" }));
    await waitFor(() =>
      expect(screen.queryByRole("button", { name: "没挂到项目的文件夹：旧资料" })).not.toBeInTheDocument(),
    );
    const column = screen.getByRole("group", { name: "没挂到项目的文件夹" });
    const stops = Array.from(column.querySelectorAll<HTMLElement>("[tabindex]")).filter((element) => element.tabIndex === 0);
    expect(stops).toHaveLength(1);
  });

  it("深链：选中的项目打开面板；服务器折起来的项目选中小行星带；不在图上的说一声", async () => {
    const apiClient = makeClient(
      overviewPayload({ islands_more: { count: 2, project_ids: ["x1", "x2"] } }),
    );
    const { unmount } = render(<Harness apiClient={apiClient} initial="p:b" />);
    expect(within(await panel()).getByRole("heading", { name: "数据中台" })).toBeInTheDocument();
    unmount();

    const folded = render(<Harness apiClient={apiClient} initial="p:x2" />);
    await waitFor(() => expect(screen.getByTestId("selection")).toHaveTextContent("belt"));
    expect(within(await panel()).getByRole("heading", { name: "2 个项目" })).toBeInTheDocument();
    folded.unmount();

    render(<Harness apiClient={apiClient} initial="p:gone" />);
    expect(await screen.findByText("要看的节点不在当前的图上，换个时间窗试试")).toBeInTheDocument();
    expect(screen.getByTestId("selection")).toHaveTextContent("");
  });

  it("文件夹的深链等文件夹那一列到了再认", async () => {
    let resolveFolders: (value: OverviewFolders) => void = () => {};
    const apiClient = makeClient(overviewPayload(), foldersPayload(), {
      getOverviewFolders: vi.fn(() => new Promise<OverviewFolders>((resolve) => (resolveFolders = resolve))),
    });
    const { container } = render(<Harness apiClient={apiClient} />);
    await screen.findByRole("application", { name: "全部项目关系图" });
    expect(container.querySelector(".overview-node--folder")).toBeNull();
    await act(async () => resolveFolders(foldersPayload()));
    const button = await screen.findByRole("button", { name: "没挂到项目的文件夹：云图看板" });
    const id = button.getAttribute("data-node-id") ?? "";
    expect(id).toMatch(/^fd:[0-9a-z]+$/);
  });

  it("点项目行星直接进项目图、不开面板；N 键跳到在等你的；「还有 N 个像新项目的名字」进资料库", async () => {
    const onOpenProjectGraph = vi.fn();
    const onOpenLibrary = vi.fn();
    const apiClient = makeClient(
      overviewPayload({
        suggested_projects: Array.from({ length: 8 }, (_, index) => ({
          key: `k${index}`,
          name: `名字${index}`,
          meeting_count: 1,
          meeting_ids: [`n${index}`],
          last_at: "2026-09-20T10:00:00",
        })),
      }),
    );
    render(<Harness apiClient={apiClient} handlers={{ onOpenLibrary, onOpenProjectGraph }} />);
    const yuntu = await screen.findByRole("button", { name: /^项目：云图AI/ });
    await userEvent.click(yuntu);
    expect(onOpenProjectGraph).toHaveBeenCalledTimes(1);
    expect(onOpenProjectGraph).toHaveBeenCalledWith("a");
    expect(screen.getByTestId("selection")).toHaveTextContent("");
    expect(screen.queryByRole("complementary", { name: "详情面板" })).toBeNull();

    // 三个项目同一天开会、场次总数一样，同一圈里按名字排：北辰仓（停了卡片）在云图AI（在等你）前面
    fireEvent.keyDown(screen.getByRole("button", { name: /^项目：数据中台/ }), { key: "n" });
    await waitFor(() => expect(screen.getByTestId("selection")).toHaveTextContent("p:c"));
    fireEvent.keyDown(yuntu, { key: "n" });
    await waitFor(() => expect(screen.getByTestId("selection")).toHaveTextContent("p:a"));
    fireEvent.keyDown(yuntu, { key: "Escape" });
    await waitFor(() => expect(screen.getByTestId("selection")).toHaveTextContent(""));

    expect(screen.getAllByRole("button", { name: /^像是新项目/ })).toHaveLength(3);
    await userEvent.click(screen.getByRole("button", { name: "还有 5 个像新项目的名字" }));
    expect(onOpenLibrary).toHaveBeenCalledWith("new_project");
  });

  it("跨项目的线超过 30 条时只在悬停或选中时画", async () => {
    const ids = Array.from({ length: 9 }, (_, index) => `q${index}`);
    const bridges = ids.flatMap((a, i) => ids.slice(i + 1).map((b) => ({ a, b, count: 1 })));
    expect(bridges.length).toBeGreaterThan(30);
    const apiClient = makeClient(
      overviewPayload({ islands: ids.map((id) => island(id, `项目${id}`)), bridges }),
    );
    const restore = stageSize();
    try {
      const { container } = render(<Harness apiClient={apiClient} />);
      const first = await screen.findByRole("button", { name: /^项目：项目q0/ });
      const drawn = () => container.querySelectorAll(".star-bridge:not(.is-gone)").length;
      await waitFor(() => expect(first.style.transform).not.toBe(""));
      expect(drawn()).toBe(0);
      await userEvent.hover(first);
      await waitFor(() => expect(drawn()).toBe(8));
      await userEvent.unhover(first);
      await waitFor(() => expect(drawn()).toBe(0));
    } finally {
      restore();
    }
  });

  it("一个项目都没有、总文件夹也没设时：空状态和提示岛都给［选项目总文件夹…］", async () => {
    const apiClient = makeClient(
      overviewPayload({ islands: [], bridges: [] }),
      foldersPayload({ state: "unset", parent: null, folders: [], suggested: { path: "/Volumes/资料盘/项目", count: 3, total: 4 } }),
    );
    render(<Harness apiClient={apiClient} />);
    expect(
      await screen.findByText("还没有项目。设好项目总文件夹后，下面的文件夹可以直接建成项目"),
    ).toBeInTheDocument();
    await waitFor(() => expect(screen.getAllByRole("button", { name: "选项目总文件夹…" })).toHaveLength(2));
    await userEvent.click(screen.getByRole("button", { name: "设项目总文件夹后，这里会列出还没挂的文件夹" }));
    const side = await panel();
    expect(within(side).getByText(/你挂过的 4 个项目文件夹里有 3 个在/)).toBeInTheDocument();
  });

  it("点面板里的字之后焦点落在页面上：Esc 照样关面板", async () => {
    render(<Harness apiClient={makeClient()} />);
    await userEvent.click(await screen.findByRole("button", { name: "云图AI：2 件在等你，打开面板" }));
    const side = await panel();
    await userEvent.click(within(side).getByRole("heading", { name: "云图AI" }));
    expect(document.activeElement).toBe(document.body);
    expect(screen.getByTestId("selection")).toHaveTextContent("p:a");

    await userEvent.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("complementary", { name: "详情面板" })).toBeNull());
    expect(screen.getByTestId("selection")).toHaveTextContent("");
  });

  it("面板开着时，输入法组合中的 Esc 不关", async () => {
    render(<Harness apiClient={makeClient()} />);
    await userEvent.click(await screen.findByRole("button", { name: "云图AI：2 件在等你，打开面板" }));
    await panel();

    fireEvent.keyDown(document.body, { key: "Escape", isComposing: true });
    fireEvent.keyDown(document.body, { key: "Escape", keyCode: 229 });
    expect(screen.getByRole("complementary", { name: "详情面板" })).toBeInTheDocument();
  });

  it("面板里再开取径器：Esc 只关取径器、面板还在；再按一次才关面板", async () => {
    const apiClient = makeClient(
      overviewPayload({ islands: [], bridges: [] }),
      foldersPayload({ state: "unset", parent: null, folders: [], suggested: { path: "/Volumes/资料盘/项目", count: 3, total: 4 } }),
      {
        browseMaterials: vi.fn(async () => ({
          base: "/Volumes/资料盘",
          path: "/Volumes/资料盘",
          parent: null,
          breadcrumbs: [{ name: "资料盘", path: "/Volumes/资料盘" }],
          dirs: [{ name: "项目", path: "/Volumes/资料盘/项目" }],
        })),
      },
    );
    render(<Harness apiClient={apiClient} />);
    await userEvent.click(await screen.findByRole("button", { name: "设项目总文件夹后，这里会列出还没挂的文件夹" }));
    const side = await panel();
    await userEvent.click(within(side).getByRole("button", { name: "选项目总文件夹…" }));
    const dialog = await screen.findByRole("dialog");
    await waitFor(() => expect(dialog.contains(document.activeElement)).toBe(true));

    await userEvent.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(screen.getByRole("complementary", { name: "详情面板" })).toBeInTheDocument();
    expect(screen.getByTestId("selection")).toHaveTextContent("folders:hint");

    // 取径器关了，焦点回到面板里打开它的那个按钮；也有焦点掉到页面上的情形，都一样
    act(() => (document.activeElement as HTMLElement | null)?.blur());
    await userEvent.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("complementary", { name: "详情面板" })).toBeNull());
  });

  it("点名字：镜头先飞过去，落定以后才进项目图；飞的时候读数、搜索框这些先藏起来", async () => {
    const onOpenProjectGraph = vi.fn();
    const restore = stageSize();
    vi.useFakeTimers({ toFake: ["requestAnimationFrame", "cancelAnimationFrame", "performance"] });
    try {
      render(<Harness apiClient={makeClient()} handlers={{ onOpenProjectGraph }} />);
      const name = await screen.findByRole("button", { name: /^项目：云图AI/ });
      act(() => vi.advanceTimersByTime(50));
      fireEvent.click(name);
      act(() => vi.advanceTimersByTime(400));
      expect(onOpenProjectGraph).not.toHaveBeenCalled();
      expect(screen.getByRole("application", { name: "全部项目关系图" })).toHaveClass("is-entering");
      act(() => vi.advanceTimersByTime(500));
      expect(onOpenProjectGraph).toHaveBeenCalledTimes(1);
      expect(onOpenProjectGraph).toHaveBeenCalledWith("a");
    } finally {
      vi.useRealTimers();
      restore();
    }
  });

  it("系统设了减少动态：点名字不飞，直接进项目图", async () => {
    const onOpenProjectGraph = vi.fn();
    const restoreSize = stageSize();
    const restoreMotion = reduceMotion();
    try {
      render(<Harness apiClient={makeClient()} handlers={{ onOpenProjectGraph }} />);
      fireEvent.click(await screen.findByRole("button", { name: /^项目：数据中台/ }));
      expect(onOpenProjectGraph).toHaveBeenCalledWith("b");
    } finally {
      restoreMotion();
      restoreSize();
    }
  });

  it("点琥珀数开这个项目的面板（琥珀数按下）；再点一下收起", async () => {
    render(<Harness apiClient={makeClient()} />);
    const amber = await screen.findByRole("button", { name: "云图AI：2 件在等你，打开面板" });
    await userEvent.click(amber);
    expect(within(await panel()).getByRole("heading", { name: "云图AI" })).toBeInTheDocument();
    expect(screen.getByTestId("selection")).toHaveTextContent("p:a");
    expect(amber).toHaveAttribute("aria-pressed", "true");
    await userEvent.click(amber);
    await waitFor(() => expect(screen.queryByRole("complementary", { name: "详情面板" })).toBeNull());
    expect(screen.getByTestId("selection")).toHaveTextContent("");
  });

  it("深链选中的那颗转到正前方：读数里的方位角正好让它落在正前方", async () => {
    const restoreSize = stageSize();
    const restoreMotion = reduceMotion();
    try {
      const { container } = render(<Harness apiClient={makeClient()} initial="p:b" />);
      expect(within(await panel()).getByRole("heading", { name: "数据中台" })).toBeInTheDocument();
      const theta = (layoutOverview(overviewPayload(), null).byId.get("p:b") as IslandNode).theta;
      const heading = String(headingDegrees(turnToFront(theta, 0))).padStart(3, "0");
      expect(heading).not.toBe("000");
      await waitFor(() => expect(readout(container)).toContain(`方位 ${heading}°`));
    } finally {
      restoreMotion();
      restoreSize();
    }
  });

  it("/ 聚焦「在图上找」：写几个字说命中几个，Esc 清空，回车进排第一的那个项目图", async () => {
    const onOpenProjectGraph = vi.fn();
    const { container } = render(<Harness apiClient={makeClient()} handlers={{ onOpenProjectGraph }} />);
    await screen.findByRole("application", { name: "全部项目关系图" });
    const input = screen.getByRole("textbox", { name: "在图上找项目，按 / 键聚焦" });
    const count = () => container.querySelector(".star-finder__count")?.textContent ?? "";

    await userEvent.keyboard("/");
    expect(input).toHaveFocus();
    expect(input).toHaveValue("");
    await userEvent.keyboard("数据");
    expect(count()).toBe("1 个");
    await userEvent.keyboard("{Escape}");
    expect(input).toHaveValue("");
    expect(count()).toBe("");
    expect(input).not.toHaveFocus();

    await userEvent.keyboard("/");
    await userEvent.keyboard("不存在的项目");
    expect(count()).toBe("没找到");
    await userEvent.clear(input);
    await userEvent.type(input, "云图{Enter}");
    expect(onOpenProjectGraph).toHaveBeenCalledWith("a");
  });

  it("最外圈超过 12 个成小行星带：点带子的圈名开列表（最近开过会的在前，可搜索），点一行进项目图", async () => {
    const onOpenProjectGraph = vi.fn();
    const far = Array.from({ length: 13 }, (_, index) =>
      island(`f${index}`, `远处的项目${index}`, { last_day: daysAgo(30 + index), meetings: 0 }),
    );
    render(
      <Harness
        apiClient={makeClient(overviewPayload({ islands: [island("a", "云图AI"), ...far], bridges: [] }))}
        handlers={{ onOpenProjectGraph }}
      />,
    );
    await userEvent.click(await screen.findByRole("button", { name: "更早或没开过会的 13 个项目，打开列表" }));
    const side = await panel();
    expect(within(side).getByRole("heading", { name: "13 个项目" })).toBeInTheDocument();
    expect(screen.getByTestId("selection")).toHaveTextContent("belt");
    const rows = () => within(side).getAllByRole("button", { name: /^远处的项目/ });
    expect(rows().map((row) => row.querySelector(".overview-belt__name")?.textContent)).toEqual(
      far.map((item) => item.name),
    );
    const search = within(side).getByRole("textbox", { name: "在这一圈里找项目" });
    expect(search).toHaveFocus();
    await userEvent.type(search, "12");
    expect(rows().map((row) => row.querySelector(".overview-belt__name")?.textContent)).toEqual(["远处的项目12"]);
    await userEvent.click(rows()[0]);
    expect(onOpenProjectGraph).toHaveBeenCalledWith("f12");
  });

  it("150 个项目：最外圈成带，名字不超预算、互不重叠、字不小于 12px；放大以后放出更多名字（含带子里的），仍不重叠", async () => {
    const restore = stageSize();
    try {
      const { container } = render(
        <Harness apiClient={makeClient(crowdedOverview(), foldersPayload({ state: "unset", parent: null, folders: [] }))} />,
      );
      const viewport = await screen.findByRole("application", { name: "全部项目关系图" });
      expect(within(viewport).getByRole("button", { name: "更早或没开过会的 116 个项目，打开列表" })).toHaveTextContent(
        "R3更早 · 116 个项目",
      );
      await waitFor(() => expect(shownLabels(container).length).toBeGreaterThan(20));
      const before = shownLabels(container);
      expect(before.length).toBeLessThanOrEqual(NAME_BUDGET);
      expect(before.some((element) => element.textContent?.startsWith("模拟项目") && element.dataset.nodeId?.startsWith("p:sim") && Number(element.dataset.nodeId.slice(5)) > 15)).toBe(false);
      expectNoOverlap(container);
      expectReadableText(viewport);

      fireEvent.wheel(viewport, { deltaY: -300 });
      await waitFor(() => expect(readout(container)).toContain("缩放 1.57×"));
      await waitFor(() => expect(shownLabels(container).length).toBeGreaterThan(before.length));
      const after = shownLabels(container);
      // 带子里的项目（模拟项目 016 以后都在最外圈）放大以后也开始有名字
      expect(after.some((element) => Number(element.dataset.nodeId?.replace("p:sim", "") ?? 0) > 15)).toBe(true);
      expectNoOverlap(container);
      expectReadableText(viewport);
    } finally {
      restore();
    }
  });

  it("接口还没有概览时不报错", () => {
    render(<Harness apiClient={{} as ApiClient} />);
    expect(screen.getByText("这个版本的服务还没有全部项目概览")).toBeInTheDocument();
  });
});

describe("api.getGraphOverview", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("带 etag 时发 If-None-Match，304 时返回 overview: null 和原来的 etag", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        new Response(JSON.stringify(overviewPayload()), {
          status: 200,
          headers: { "Content-Type": "application/json", ETag: ETAG },
        }),
      )
      .mockResolvedValueOnce(new Response(null, { status: 304 }));
    vi.stubGlobal("fetch", fetchMock);

    const first = await api.getGraphOverview("28d");
    expect(first.etag).toBe(ETAG);
    expect(first.overview?.islands).toHaveLength(3);
    expect(fetchMock.mock.calls[0][0]).toBe("/api/graph/overview?window=28d");
    expect(fetchMock.mock.calls[0][1].headers).not.toHaveProperty("If-None-Match");

    const second = await api.getGraphOverview("28d", ETAG);
    expect(second).toEqual({ overview: null, etag: ETAG });
    expect(fetchMock.mock.calls[1][1].headers).toMatchObject({ "If-None-Match": ETAG });
  });
});
