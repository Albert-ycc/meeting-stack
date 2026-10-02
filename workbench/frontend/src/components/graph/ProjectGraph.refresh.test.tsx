/*
 * 星图每 30 秒对一次数据：带上次的 etag，没变（304）时不换图对象，布局、画布、相关线都不动。
 * 单独一个文件：要在模块级包住 layoutStarMap 数它被算了几次，不能影响 ProjectGraph.test.tsx 里的别的用例。
 */
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { ApiClient } from "../../api";
import { LinksFlagsContext } from "../links/LinksFlagsContext";
import type { GraphPayload, GraphRootsPayload, GraphWindow, RelatedEdges } from "./graphTypes";
import { layoutStarMap } from "./layout";
import { ProjectGraph, forgetGraphCache } from "./ProjectGraph";
import { day, meeting, payload } from "./testFixtures";

vi.mock("./layout", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./layout")>();
  return { ...actual, layoutStarMap: vi.fn(actual.layoutStarMap) };
});

const FLAGS = { linksEnabled: true, semanticEnabled: true, llmConfigured: true };
const clone = <T,>(value: T): T => JSON.parse(JSON.stringify(value));

// 有材料根目录、最近改过的文件：补出来的节点每次都是新对象，资料盘状态的重取要是不留原对象，布局就白算
const ROOTS: GraphRootsPayload = {
  roots: [
    {
      id: "root:1",
      root_id: 1,
      path: "/材料/云图AI",
      state: "online",
      loose_count: 1,
      checked_at: null,
      recent_dirs: [{ name: "初审规则", dir: "初审规则", path: "/材料/云图AI/初审规则", mtime: "2026-09-25T10:00:00" }],
      recent_files: [
        { file_id: 21, name: "方案.docx", ext: "docx", dir_rel: "方案", mtime: "2026-09-25T10:00:00+00:00", state: "done" },
        { file_id: 22, name: "截图.png", ext: "png", dir_rel: "方案", mtime: "2026-09-25T10:00:00+00:00", state: "done" },
      ],
      content: { files: 12, done: 10, unreadable: 0 },
    },
  ],
  folders: [],
  loose: { count: 1, recent: [] },
  checking: false,
  can_reveal: true,
};
const RELATED: RelatedEdges = { rev: 1, files: {}, edges: [] };
const RELATED_ETAG = 'W/"related-1"';

type Fetched = { graph: GraphPayload | null; etag: string | null };

/** 像后端那样回：etag 对得上回 304（graph: null），对不上回整张图。每次都是新解析出来的一份，和真的响应一样 */
function fakeGraphServer(initial: Record<string, { graph: GraphPayload; etag: string }>) {
  const state = { ...initial };
  const held: Array<(value: Fetched) => void> = [];
  let holdNext = false;
  const fn = vi.fn((_project: string, window?: GraphWindow, focus?: string, etag?: string | null): Promise<Fetched> => {
    const current = state[`${window ?? "auto"}|${focus ?? ""}`] ?? state[window ?? "auto"];
    const answer: Fetched = etag === current.etag ? { graph: null, etag } : { graph: clone(current.graph), etag: current.etag };
    if (!holdNext) return Promise.resolve(answer);
    holdNext = false;
    return new Promise((resolve) => held.push(resolve));
  });
  return {
    fn,
    /** 后端的数据变了 */
    set: (key: string, next: { graph: GraphPayload; etag: string }) => {
      state[key] = next;
    },
    /** 下一次请求先按住，等 release 再回 */
    hold: () => {
      holdNext = true;
    },
    release: (value: Fetched) => held.splice(0).forEach((resolve) => resolve(value)),
  };
}

function makeClient(server: ReturnType<typeof fakeGraphServer>, overrides: Record<string, unknown> = {}) {
  const undoUntil = new Date(Date.now() + 10 * 60_000).toISOString();
  return {
    graph: server.fn,
    graphRoots: vi.fn(async () => clone(ROOTS)),
    graphRelated: vi.fn(async (_project: string, _window: GraphWindow, etag?: string | null) =>
      etag === RELATED_ETAG ? { related: null, etag } : { related: clone(RELATED), etag: RELATED_ETAG },
    ),
    meetingBrief: vi.fn(),
    updateMeeting: vi.fn(async () => ({ effects: { tasks_moved: 0, tasks_left: [], undo_until: undoUntil } })),
    ...overrides,
  } as unknown as ApiClient & Record<string, ReturnType<typeof vi.fn>>;
}

function Harness({ apiClient, focus = null }: { apiClient: ApiClient; focus?: string | null }) {
  const [selection, setSelection] = useState<string | null>(null);
  return (
    <LinksFlagsContext.Provider value={FLAGS}>
      <ProjectGraph
        apiClient={apiClient}
        focus={focus}
        onBack={() => {}}
        onOpenGlossary={() => {}}
        onOpenMeeting={() => {}}
        onOpenProject={() => {}}
        onOpenRequirement={() => {}}
        onSelectionChange={setSelection}
        projectId="p"
        projects={[{ id: "p", name: "云图AI", color: "#2c8d83" }]}
        selection={selection}
      />
    </LinksFlagsContext.Provider>
  );
}

const E1 = 'W/"g-1"';
const layoutCalls = () => vi.mocked(layoutStarMap).mock.calls.length;
const flush = () => act(async () => new Promise((resolve) => setTimeout(resolve, 0)));
// 抓住 30 秒那个定时器的回调，直接调它：不假时钟，waitFor 照常轮询
const intervals: Array<() => void> = [];
/** 走过 30 秒：调最近一次挂上的那个定时回调 */
const tick = async () => {
  await act(async () => intervals[intervals.length - 1]());
  await flush();
};

async function openGraph(apiClient: ApiClient) {
  render(<Harness apiClient={apiClient} />);
  await screen.findByRole("button", { name: /^会议：初审规则沟通 a/ });
  await waitFor(() => expect(apiClient.graphRelated).toHaveBeenCalledTimes(1));
  await flush();
}

let intervalSpy: ReturnType<typeof vi.spyOn> | undefined;

beforeEach(() => {
  forgetGraphCache();
  window.localStorage.clear();
  // ［相关］开着：定时对数据时相关线是不是被重取，看得见
  window.localStorage.setItem("meeting-workbench:graph:lines.p", JSON.stringify({ mention: true, related: true }));
  vi.mocked(layoutStarMap).mockClear();
  intervals.length = 0;
  const realSetInterval = window.setInterval.bind(window);
  intervalSpy = vi.spyOn(window, "setInterval").mockImplementation(((handler: TimerHandler, timeout?: number) => {
    if (timeout === 30_000 && typeof handler === "function") intervals.push(handler as () => void);
    return realSetInterval(handler, timeout);
  }) as typeof window.setInterval);
});

afterEach(() => {
  intervalSpy?.mockRestore();
});

describe("ProjectGraph 30 秒刷新带 etag", () => {
  it("数据没变（304）：请求带着上一次的 etag，图对象不换——不重算布局、不重取相关线", async () => {
    const server = fakeGraphServer({ auto: { graph: payload(), etag: E1 } });
    const apiClient = makeClient(server);
    await openGraph(apiClient);
    expect(apiClient.graph).toHaveBeenCalledTimes(1);
    expect(apiClient.graph).toHaveBeenLastCalledWith("p", undefined, undefined, null);
    const layouts = layoutCalls();

    await tick();
    await tick();
    await tick();

    expect(apiClient.graph).toHaveBeenCalledTimes(4);
    expect(apiClient.graph).toHaveBeenLastCalledWith("p", undefined, undefined, E1);
    // 资料盘状态照旧每 30 秒问一遍；内容没变，补出来的最近文件不换对象，布局也不跟着算
    expect(apiClient.graphRoots).toHaveBeenCalledTimes(4);
    expect(layoutCalls()).toBe(layouts);
    expect(apiClient.graphRelated).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("button", { name: /^会议：初审规则沟通 a/ })).toBeInTheDocument();
  });

  it("数据变了（200）：新图照常画出来，相关线跟着对一次，下一次带的是新的 etag", async () => {
    const server = fakeGraphServer({ auto: { graph: payload(), etag: E1 } });
    const apiClient = makeClient(server);
    await openGraph(apiClient);
    await tick();
    const layouts = layoutCalls();

    server.set("auto", { graph: payload({ meetings: [meeting("a", 0), meeting("b", 2), meeting("c", 10), meeting("d", 1)] }), etag: 'W/"g-2"' });
    await tick();

    expect(await screen.findByRole("button", { name: /^会议：初审规则沟通 d/ })).toBeInTheDocument();
    expect(layoutCalls()).toBeGreaterThan(layouts);
    await waitFor(() => expect(apiClient.graphRelated).toHaveBeenCalledTimes(2));
    expect(apiClient.graphRelated).toHaveBeenLastCalledWith("p", "28d", RELATED_ETAG);

    await tick();
    expect(apiClient.graph).toHaveBeenLastCalledWith("p", undefined, undefined, 'W/"g-2"');
    expect(apiClient.graphRelated).toHaveBeenCalledTimes(2);
  });

  it("晚回来的旧响应：图和它的 etag 都不进缓存，下一次对数据带的还是新的那个", async () => {
    const withDoorstep = payload({
      doorstep: [
        {
          id: "d:door0",
          meeting_id: "door0",
          title: "门口的会",
          date: day(1),
          age_days: 1,
          candidates: [{ project_id: "p", project_name: "云图AI", project_color: "#2c8d83", count: 2 }],
          reason: "",
        },
      ],
    });
    const server = fakeGraphServer({ auto: { graph: withDoorstep, etag: E1 } });
    const apiClient = makeClient(server);
    const view = render(<Harness apiClient={apiClient} />);
    await screen.findByRole("button", { name: "可能是这个项目的会：门口的会" });
    await flush();

    // 定时那次（A）发出去先按住；用户作答，写后重取（B）先回来，已经是写之后的数据
    server.hold();
    await tick();
    expect(apiClient.graph).toHaveBeenCalledTimes(2);
    server.set("auto", { graph: payload(), etag: 'W/"g-2"' });
    fireEvent.click(screen.getByRole("button", { name: "都不是" }));
    await waitFor(() => expect(screen.queryByRole("button", { name: "可能是这个项目的会：门口的会" })).toBeNull());

    // A 这时才回来：带着写之前的图和一个别的 etag（不管是 200 还是 304，都不能进缓存）
    await act(async () => server.release({ graph: clone(withDoorstep), etag: 'W/"g-stale"' }));
    expect(screen.queryByRole("button", { name: "可能是这个项目的会：门口的会" })).toBeNull();
    await tick();
    expect(apiClient.graph).toHaveBeenLastCalledWith("p", undefined, undefined, 'W/"g-2"');

    // 再进这张图：先画的缓存是写之后的那份
    view.unmount();
    const again = makeClient(fakeGraphServer({ auto: { graph: payload(), etag: 'W/"g-2"' } }), {
      graph: vi.fn(() => new Promise(() => undefined)),
    });
    render(<Harness apiClient={again} />);
    await screen.findByRole("button", { name: /^会议：初审规则沟通 a/ });
    expect(screen.queryByRole("button", { name: "可能是这个项目的会：门口的会" })).toBeNull();
  });

  it("etag 跟缓存键对应：换时间窗不带别的窗的 etag，切回来带自己的、304 时画回自己的那份", async () => {
    const week = payload({
      window: { requested: "7d", effective: "7d", days: 7, widened_reason: null },
      meetings: [meeting("a", 0)],
    });
    const month = payload({ window: { requested: "28d", effective: "28d", days: 28, widened_reason: null } });
    const server = fakeGraphServer({
      auto: { graph: payload(), etag: E1 },
      "7d": { graph: week, etag: 'W/"g-7d"' },
      "28d": { graph: month, etag: 'W/"g-28d"' },
    });
    const apiClient = makeClient(server);
    await openGraph(apiClient);

    // 换到没看过的 7 天：不带「自动」那份的 etag
    await userEvent.click(screen.getByRole("button", { name: "7 天" }));
    await waitFor(() => expect(apiClient.graph).toHaveBeenLastCalledWith("p", "7d", undefined, null));
    await waitFor(() => expect(screen.queryByRole("button", { name: /^会议：初审规则沟通 b/ })).toBeNull());

    // 再到 28 天、又回 7 天：第二次回 7 天带的是 7 天自己的 etag，304 以后画的还是 7 天那份（一场会）
    await userEvent.click(screen.getByRole("button", { name: "28 天" }));
    await waitFor(() => expect(apiClient.graph).toHaveBeenLastCalledWith("p", "28d", undefined, null));
    await screen.findByRole("button", { name: /^会议：初审规则沟通 b/ });
    await userEvent.click(screen.getByRole("button", { name: "7 天" }));
    await waitFor(() => expect(apiClient.graph).toHaveBeenLastCalledWith("p", "7d", undefined, 'W/"g-7d"'));
    await waitFor(() => expect(screen.queryByRole("button", { name: /^会议：初审规则沟通 b/ })).toBeNull());
    expect(screen.getByRole("button", { name: /^会议：初审规则沟通 a/ })).toBeInTheDocument();
  });

  it("深链目标换了缓存键：先画同一时间窗的旧图，请求不带那份旧图的 etag；回来的新响应记在自己的键下", async () => {
    const server = fakeGraphServer({
      auto: { graph: payload(), etag: E1 },
      "auto|m:a": { graph: payload(), etag: 'W/"g-focus"' },
    });
    const first = render(<Harness apiClient={makeClient(server)} />);
    await screen.findByRole("button", { name: /^会议：初审规则沟通 a/ });
    await flush();
    first.unmount();

    const apiClient = makeClient(server);
    render(<Harness apiClient={apiClient} focus="m:a" />);
    // 缓存里没有 m:a 这个键，旧图（没有深链目标的那份）已经画出来了
    expect(screen.getByRole("button", { name: /^会议：初审规则沟通 a/ })).toBeInTheDocument();
    await waitFor(() => expect(apiClient.graph).toHaveBeenCalledWith("p", undefined, "m:a", null));
    await flush();

    await tick();
    expect(apiClient.graph).toHaveBeenLastCalledWith("p", undefined, "m:a", 'W/"g-focus"');
  });

  it("304 却没有手上的那份（没带 etag 也回 304）：不带 etag 再取一次", async () => {
    const graph = vi
      .fn()
      .mockResolvedValueOnce({ graph: null, etag: E1 })
      .mockResolvedValueOnce({ graph: payload(), etag: E1 });
    const apiClient = makeClient(fakeGraphServer({ auto: { graph: payload(), etag: E1 } }), { graph });
    render(<Harness apiClient={apiClient} />);
    expect(await screen.findByRole("button", { name: /^会议：初审规则沟通 a/ })).toBeInTheDocument();
    expect(graph).toHaveBeenCalledTimes(2);
    expect(graph).toHaveBeenLastCalledWith("p", undefined, undefined, null);
  });

  it("写操作只动了相关线、图本身没变（304）：刷新以后相关线照样再对一次", async () => {
    const withDoorstep = payload({
      doorstep: [
        {
          id: "d:door0",
          meeting_id: "door0",
          title: "门口的会",
          date: day(1),
          age_days: 1,
          candidates: [{ project_id: "p", project_name: "云图AI", project_color: "#2c8d83", count: 2 }],
          reason: "",
        },
      ],
    });
    // 作答以后后端的图版本号没动（和标「不相关」一样，只动相关自己的版本号）：重取是 304
    const server = fakeGraphServer({ auto: { graph: withDoorstep, etag: E1 } });
    const apiClient = makeClient(server);
    render(<Harness apiClient={apiClient} />);
    const doorstep = await screen.findByRole("group", { name: /可能是这个项目的会：门口的会/ });
    await waitFor(() => expect(apiClient.graphRelated).toHaveBeenCalledTimes(1));
    await flush();

    await userEvent.click(within(doorstep).getByRole("button", { name: "归这里" }));
    expect(apiClient.updateMeeting).toHaveBeenCalledWith("door0", { project_id: "p" });
    await waitFor(() => expect(apiClient.graph).toHaveBeenCalledTimes(2));
    expect(apiClient.graph).toHaveBeenLastCalledWith("p", undefined, undefined, E1);

    await waitFor(() => expect(apiClient.graphRelated).toHaveBeenCalledTimes(2));
  });
});
