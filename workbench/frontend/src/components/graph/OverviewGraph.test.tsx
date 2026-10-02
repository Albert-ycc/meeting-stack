import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { api, type ApiClient } from "../../api";
import type { Project } from "../../types";
import { OverviewGraph, forgetOverviewCache } from "./OverviewGraph";
import { foldersPayload, island, overviewPayload } from "./overviewFixtures";
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

beforeEach(() => {
  forgetOverviewCache();
  window.localStorage.clear();
});

afterEach(() => {
  vi.useRealTimers();
});

describe("OverviewGraph", () => {
  it("先说正在画，再画出港湾、项目岛、琥珀点、幽灵岛、灰色文件夹岛和跨项目的线", async () => {
    const apiClient = makeClient();
    const { container } = render(<Harness apiClient={apiClient} />);
    expect(screen.getByText("正在画关系图…")).toBeInTheDocument();

    const canvas = await screen.findByRole("application", { name: "全部项目关系图" });
    expect(apiClient.getGraphOverview).toHaveBeenCalledWith("28d", null);
    const yuntu = within(canvas).getByRole("button", { name: "项目：云图AI，28 天 7 场，2 件在等你" });
    expect(within(yuntu).getByText("2")).toHaveAttribute("title", "1 场可能是这个项目的、1 条任务待确认");
    const quiet = within(canvas).getByRole("button", { name: /^项目：北辰仓，28 天 0 场/ });
    expect(quiet).toHaveClass("is-quiet");
    expect(within(quiet).getByText("⊘")).toHaveAttribute("title", "2 张卡片停了");

    const harbour = within(canvas).getByRole("button", { name: "港湾：9 场没归项目的会" });
    expect(harbour).toHaveTextContent("等 AI 判断 2 · 待你选 3 · AI 没认出 2 · 像新项目 2");
    const ghost = within(canvas).getByRole("group", { name: "像是新项目『云图看板』· 2 场会" });
    expect(within(ghost).getByRole("button", { name: "建成项目" })).toBeInTheDocument();
    expect(within(ghost).getByRole("button", { name: "建成需求" })).toBeInTheDocument();

    expect(await within(canvas).findByRole("button", { name: "没挂到项目的文件夹：旧资料" })).toBeInTheDocument();
    expect(container.querySelectorAll(".overview-bridge")).toHaveLength(1);
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
    await userEvent.click(await screen.findByRole("button", { name: /^项目：云图AI/ }));
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
    await userEvent.click(await screen.findByRole("button", { name: /^项目：云图AI/ }));
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

  it("深链：选中的岛打开面板；折起来的项目选中「其余 N 个项目」；不在图上的说一声", async () => {
    const apiClient = makeClient(
      overviewPayload({ islands_more: { count: 2, project_ids: ["x1", "x2"] } }),
    );
    const { unmount } = render(<Harness apiClient={apiClient} initial="p:b" />);
    expect(within(await panel()).getByRole("heading", { name: "数据中台" })).toBeInTheDocument();
    unmount();

    const folded = render(<Harness apiClient={apiClient} initial="p:x2" />);
    await waitFor(() => expect(screen.getByTestId("selection")).toHaveTextContent("islands:more"));
    expect(within(await panel()).getByRole("heading", { name: "其余 2 个项目" })).toBeInTheDocument();
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

  it("双击岛进项目图；N 键跳到在等你的；「还有 N 个像新项目的名字」进资料库", async () => {
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
    await userEvent.dblClick(yuntu);
    expect(onOpenProjectGraph).toHaveBeenCalledWith("a");

    fireEvent.keyDown(screen.getByRole("button", { name: /^项目：数据中台/ }), { key: "n" });
    await waitFor(() => expect(screen.getByTestId("selection")).toHaveTextContent("p:a"));
    fireEvent.keyDown(yuntu, { key: "n" });
    await waitFor(() => expect(screen.getByTestId("selection")).toHaveTextContent("p:c"));
    fireEvent.keyDown(yuntu, { key: "Escape" });
    await waitFor(() => expect(screen.getByTestId("selection")).toHaveTextContent(""));

    expect(screen.getAllByRole("group", { name: /^像是新项目/ })).toHaveLength(6);
    await userEvent.click(screen.getByRole("button", { name: "还有 2 个像新项目的名字" }));
    expect(onOpenLibrary).toHaveBeenCalledWith("new_project");
  });

  it("跨项目的线超过 30 条时只在悬停或选中时画", async () => {
    const ids = Array.from({ length: 9 }, (_, index) => `q${index}`);
    const bridges = ids.flatMap((a, i) => ids.slice(i + 1).map((b) => ({ a, b, count: 1 })));
    expect(bridges.length).toBeGreaterThan(30);
    const apiClient = makeClient(
      overviewPayload({ islands: ids.map((id) => island(id, `项目${id}`)), bridges }),
    );
    const { container } = render(<Harness apiClient={apiClient} />);
    const first = await screen.findByRole("button", { name: /^项目：项目q0/ });
    expect(container.querySelectorAll(".overview-bridge")).toHaveLength(0);
    fireEvent.mouseEnter(first);
    expect(container.querySelectorAll(".overview-bridge")).toHaveLength(8);
    fireEvent.mouseLeave(first);
    expect(container.querySelectorAll(".overview-bridge")).toHaveLength(0);
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
