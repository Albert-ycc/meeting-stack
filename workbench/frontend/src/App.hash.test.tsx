import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import App, { MOBILE_READ_ONLY_QUERY } from "./App";
import type { ApiClient } from "./api";
import { forgetOverviewCache } from "./components/graph/OverviewGraph";
import { foldersPayload, overviewPayload } from "./components/graph/overviewFixtures";
import { forgetGraphCache } from "./components/graph/ProjectGraph";
import { focusPayload, payload } from "./components/graph/testFixtures";

function desktopMatchMedia() {
  return {
    matches: false,
    media: MOBILE_READ_ONLY_QUERY,
    onchange: null,
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    addListener: vi.fn(),
    removeListener: vi.fn(),
    dispatchEvent: vi.fn(),
  };
}

function client(overrides: Partial<ApiClient> = {}) {
  return {
    bootstrap: vi.fn().mockResolvedValue({
      csrf_token: "token",
      mobile_read_only: true,
      mobile_task_write: true,
      semantic_enabled: true,
      pending_confirm_count: 0,
    }),
    health: vi.fn().mockResolvedValue({
      status: "ok",
      services: {},
      counts: { meetings: 0, unreviewed: 0, failed_jobs: 0, scan_errors: 0 },
    }),
    meetings: vi.fn().mockResolvedValue({ items: [], limit: 50, offset: 0, total: 0 }),
    projects: vi.fn().mockResolvedValue([]),
    tags: vi.fn().mockResolvedValue([]),
    tasks: vi.fn().mockResolvedValue({ items: [], total: 0, limit: 100, offset: 0, counts: {} }),
    jobs: vi.fn().mockResolvedValue({ items: [] }),
    ...overrides,
  } as unknown as ApiClient;
}

function emptyPool() {
  return {
    items: [],
    total: 0,
    limit: 500,
    offset: 0,
    status: "active",
    counts: { pending: 0, active: 0, done: 0, shelved: 0, all: 0 },
    projects: [],
    unassigned_count: 0,
    dropped_count: 0,
  };
}

beforeEach(() => {
  vi.stubGlobal("matchMedia", vi.fn(() => desktopMatchMedia()));
  Object.defineProperty(document, "hidden", { configurable: true, value: false });
});

afterEach(() => {
  window.history.replaceState(null, "", "/");
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("地址栏锚点直达", () => {
  it("冷加载带 #tasks 停在任务页，锚点不被首帧清掉", async () => {
    window.history.replaceState(null, "", "/#tasks");

    render(<App apiClient={client()} />);

    expect(await screen.findByRole("heading", { name: "任务" })).toBeInTheDocument();
    expect(window.location.hash).toBe("#tasks");
  });

  it("冷加载带 #glossary 停在词典页", async () => {
    window.history.replaceState(null, "", "/#glossary");

    render(<App apiClient={client()} />);

    expect(await screen.findByRole("tab", { name: /术语库/ })).toBeInTheDocument();
    expect(window.location.hash).toBe("#glossary");
  });

  it("冷加载带 #requirements/<id> 停在需求详情页，锚点不被首帧清掉", async () => {
    window.history.replaceState(null, "", "/#requirements/req-1");
    const requirement = vi.fn().mockResolvedValue({
      id: "req-1",
      project_id: "project-a",
      project_name: "项目甲",
      project_color: "#376f68",
      title: "北辰仓快递配送",
      priority: "P0",
      status: "active",
      created_at: "2026-09-07T00:00:00Z",
      updated_at: "2026-09-07T00:00:00Z",
      open_task_count: 0,
      meeting_count: 0,
      latest_meeting_date: null,
      folder_count: 0,
      folders: [],
      meetings: [],
      tasks: [],
    });

    render(<App apiClient={client({ requirement })} />);

    expect(await screen.findByRole("heading", { name: "北辰仓快递配送" })).toBeInTheDocument();
    expect(window.location.hash).toBe("#requirements/req-1");
  });

  it("冷加载带 #meetings/<id>@<秒> 打开这场会，秒数用过就从地址栏去掉", async () => {
    window.history.replaceState(null, "", "/#meetings/vm-1@754");
    const meeting = vi.fn().mockResolvedValue({
      id: "vm-1",
      title: "初审规则沟通",
      status: "completed_unreviewed",
      tags: [],
      artifacts: [],
      segments: [],
      speakers: [],
      events: [],
      transcript_versions: [],
      minutes_versions: [],
    });

    render(<App apiClient={client({ meeting, transcriptVersionSegments: vi.fn() } as Partial<ApiClient>)} />);

    expect(await screen.findByRole("heading", { name: "初审规则沟通" })).toBeInTheDocument();
    expect(meeting).toHaveBeenCalledWith("vm-1");
    expect(window.location.hash).toBe("#meetings/vm-1");
  });

  it("冷加载带 #requirements 停在需求池海报墙", async () => {
    window.history.replaceState(null, "", "/#requirements");
    const requirementPool = vi.fn().mockResolvedValue(emptyPool());

    render(<App apiClient={client({ requirementPool })} />);

    expect(await screen.findByRole("heading", { name: "需求池" })).toBeInTheDocument();
    expect(requirementPool).toHaveBeenCalledWith(expect.objectContaining({ status: "active" }));
    expect(window.location.hash).toBe("#requirements");
  });

  it("手机上需求池本期不改，还是原来的只读列表", async () => {
    vi.stubGlobal("matchMedia", vi.fn(() => ({ ...desktopMatchMedia(), matches: true })));
    window.history.replaceState(null, "", "/#requirements");
    const requirementPool = vi.fn();

    render(
      <App
        apiClient={client({
          requirementPool,
          requirements: vi.fn().mockResolvedValue({
            items: [],
            total: 0,
            limit: 10,
            offset: 0,
            counts: { active: 0, done: 0, shelved: 0, all: 0 },
          }),
        })}
      />,
    );

    expect(await screen.findByRole("heading", { name: "需求" })).toBeInTheDocument();
    expect(requirementPool).not.toHaveBeenCalled();
  });

  it("冷加载带 #requirements/new 打开新增需求页，#requirements/claim/<id> 打开认领页", async () => {
    window.history.replaceState(null, "", "/#requirements/new");
    const requirementCandidate = vi.fn().mockReturnValue(new Promise(() => {}));
    const { unmount } = render(<App apiClient={client({ requirementCandidate })} />);

    expect(await screen.findByRole("heading", { name: "新增需求" })).toBeInTheDocument();
    expect(window.location.hash).toBe("#requirements/new");
    unmount();

    window.history.replaceState(null, "", "/#requirements/claim/candidate-1");
    render(<App apiClient={client({ requirementCandidate })} />);

    expect(await screen.findByRole("heading", { name: "认领候选" })).toBeInTheDocument();
    expect(requirementCandidate).toHaveBeenCalledWith("candidate-1");
    expect(window.location.hash).toBe("#requirements/claim/candidate-1");
  });

  it("手机上打开新增、认领的地址退回需求池（手机端只读）", async () => {
    vi.stubGlobal("matchMedia", vi.fn(() => ({ ...desktopMatchMedia(), matches: true })));
    window.history.replaceState(null, "", "/#requirements/claim/candidate-1");
    const requirementCandidate = vi.fn();

    render(
      <App
        apiClient={client({
          requirementCandidate,
          requirements: vi.fn().mockResolvedValue({
            items: [],
            total: 0,
            limit: 10,
            offset: 0,
            counts: { active: 0, done: 0, shelved: 0, all: 0 },
          }),
        })}
      />,
    );

    expect(await screen.findByRole("heading", { name: "需求" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "认领候选" })).not.toBeInTheDocument();
    expect(requirementCandidate).not.toHaveBeenCalled();
    expect(window.location.hash).toBe("#requirements");
  });

  it("冷加载带 #projects/<id>/graph?sel=m:<id> 打开关系图并选中那场会；换选中只改地址栏不压历史", async () => {
    forgetGraphCache();
    window.history.replaceState(null, "", "/#projects/p/graph?sel=m:a");
    const graph = vi.fn().mockResolvedValue(payload());
    const meetingBrief = vi.fn().mockRejectedValue(new Error("简报读不到"));
    render(
      <App
        apiClient={client({
          projects: vi.fn().mockResolvedValue([{ id: "p", name: "云图AI", color: "#2c8d83" }]),
          graph,
          graphRoots: vi.fn().mockResolvedValue({ roots: [], folders: [], loose: { count: 0, recent: [] }, checking: false }),
          meetingBrief,
        } as unknown as Partial<ApiClient>)}
      />,
    );

    expect(await screen.findByRole("complementary", { name: "详情面板" })).toBeInTheDocument();
    expect(graph).toHaveBeenCalledWith("p", undefined, "m:a");
    // 面板先渲染、取简报的 effect 后跑：机器忙时要等一下
    await waitFor(() => expect(meetingBrief).toHaveBeenCalledWith("a"));
    expect(window.location.hash).toBe("#projects/p/graph?sel=m:a");
    expect(within(screen.getByRole("group", { name: "项目视图" })).getByRole("button", { name: "关系图" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );

    const depth = window.history.length;
    await userEvent.click(screen.getByRole("button", { name: /^会议：初审规则沟通 b/ }));
    await waitFor(() => expect(window.location.hash).toBe("#projects/p/graph?sel=m:b"));
    expect(window.history.length).toBe(depth);
  });

  it("展开一场会压一条历史、地址栏带 expand=；回到关系图就是后退，冷加载带 expand= 直接展开", async () => {
    forgetGraphCache();
    window.history.replaceState(null, "", "/#projects/p/graph?sel=m:a");
    const graphMeetingFocus = vi.fn().mockResolvedValue(focusPayload());
    const apiClient = client({
      projects: vi.fn().mockResolvedValue([{ id: "p", name: "云图AI", color: "#2c8d83" }]),
      graph: vi.fn().mockResolvedValue(payload()),
      graphRoots: vi.fn().mockResolvedValue({ roots: [], folders: [], loose: { count: 0, recent: [] }, checking: false }),
      meetingBrief: vi.fn().mockRejectedValue(new Error("简报读不到")),
      graphMeetingFocus,
    } as unknown as Partial<ApiClient>);
    const { unmount } = render(<App apiClient={apiClient} />);

    const panel = await screen.findByRole("complementary", { name: "详情面板" });
    const depth = window.history.length;
    await userEvent.click(within(panel).getByRole("button", { name: "展开这场会" }));
    await waitFor(() => expect(window.location.hash).toBe("#projects/p/graph?expand=a&sel=m:a"));
    expect(window.history.length).toBe(depth + 1);
    expect(await screen.findByRole("application", { name: "展开的会：初审规则沟通 a" })).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "← 回到关系图" }));
    await waitFor(() => expect(window.location.hash).toBe("#projects/p/graph?sel=m:a"));
    expect(await screen.findByRole("button", { name: /^会议：初审规则沟通 a/ })).toBeInTheDocument();
    unmount();

    forgetGraphCache();
    window.history.replaceState(null, "", "/#projects/p/graph?expand=a");
    render(<App apiClient={apiClient} />);
    expect(await screen.findByRole("application", { name: "展开的会：初审规则沟通 a" })).toBeInTheDocument();
    expect(graphMeetingFocus).toHaveBeenLastCalledWith("a");
  });
});

describe("局部图和来龙去脉的地址（4f）", () => {
  function mapFor(fileId: number) {
    return {
      center: { id: `file:${fileId}`, kind: "file", file_id: fileId, name: `文件${fileId}.xlsx`, at: "2026-09-18T10:00:00+08:00", gone: false, copies: [] },
      nodes: [{ id: `file:${fileId + 1}`, kind: "file", file_id: fileId + 1, name: `文件${fileId + 1}.xlsx`, at: "2026-09-10T10:00:00+08:00" }],
      edges: [{ id: `e:same:${fileId + 1}:${fileId}`, kind: "same_name", from: `file:${fileId + 1}`, to: `file:${fileId}`, label: "同属『文件』" }],
      hidden: [],
      hidden_count: 0,
    };
  }

  function localClient(overrides: Record<string, unknown> = {}) {
    return client({
      bootstrap: vi.fn().mockResolvedValue({
        csrf_token: "token",
        mobile_read_only: true,
        mobile_task_write: true,
        semantic_enabled: true,
        links_enabled: true,
        llm_configured: true,
        pending_confirm_count: 0,
      }),
      projects: vi.fn().mockResolvedValue([{ id: "p", name: "云图AI", color: "#2c8d83" }]),
      graph: vi.fn().mockResolvedValue(
        payload({
          files: [{ id: "file:7", kind: "file", file_id: 7, name: "文件7.xlsx", ext: "xlsx", rel_path: "文件7.xlsx", root_id: 1, folder: "root:1" }],
        }),
      ),
      graphRoots: vi.fn().mockResolvedValue({ roots: [], folders: [], loose: { count: 0, recent: [] }, checking: false }),
      meetingBrief: vi.fn().mockRejectedValue(new Error("简报读不到")),
      getGraphFile: vi.fn().mockRejectedValue(new Error("读不到")),
      graphFileMap: vi.fn(async (fileId: number) => mapFor(fileId)),
      graphTrace: vi.fn(async (node: string) => ({
        center: { id: node, kind: "meeting", title: "初审规则沟通 a", at: "2026-09-21T10:00:00+08:00", audio_url: null },
        nodes: [],
        edges: [],
        chain: [node],
        center_index: 0,
        cut: { back: false, forward: false },
      })),
      graphMeetingFocus: vi.fn().mockResolvedValue(focusPayload()),
      ...overrides,
    } as unknown as Partial<ApiClient>);
  }

  it("冷加载 ?file= 打开局部图，［回到关系图］替换地址去掉 file；冷加载 ?trace= 打开来龙去脉", async () => {
    forgetGraphCache();
    window.history.replaceState(null, "", "/#projects/p/graph?file=7");
    const apiClient = localClient();
    const { unmount } = render(<App apiClient={apiClient} />);
    expect(await screen.findByRole("heading", { name: "以『文件7.xlsx』为中心" })).toBeInTheDocument();
    expect(apiClient.graphFileMap).toHaveBeenCalledWith(7, { related: false });
    await userEvent.click(screen.getByRole("button", { name: "回到关系图" }));
    await waitFor(() => expect(window.location.hash).toBe("#projects/p/graph"));
    expect(await screen.findByRole("application", { name: "云图AI 关系图" })).toBeInTheDocument();
    unmount();

    forgetGraphCache();
    window.history.replaceState(null, "", "/#projects/p/graph?trace=m:a");
    render(<App apiClient={apiClient} />);
    expect(await screen.findByRole("heading", { name: "『初审规则沟通 a』的来龙去脉" })).toBeInTheDocument();
    expect(apiClient.graphTrace).toHaveBeenCalledWith("m:a");
    expect(screen.getByText("这场会还没有带原话的来龙去脉")).toBeInTheDocument();
  });

  it("进局部图和每换一次中心各压一条历史：返回键回到上一个中心，［回到关系图］一次回到星图", async () => {
    forgetGraphCache();
    window.history.replaceState(null, "", "/#projects/p/graph");
    render(<App apiClient={localClient()} />);
    // jsdom 里前面的测试可能留下前进的历史，按 state 里记的层数看有没有压历史
    const localDepth = () => (window.history.state as { localDepth?: number } | null)?.localDepth;
    await userEvent.dblClick(await screen.findByRole("button", { name: "文件：文件7.xlsx" }));
    await waitFor(() => expect(window.location.hash).toBe("#projects/p/graph?file=7"));
    expect(localDepth()).toBe(1);
    await userEvent.dblClick(await screen.findByRole("button", { name: "文件：文件8.xlsx" }));
    await waitFor(() => expect(window.location.hash).toBe("#projects/p/graph?file=8"));
    await userEvent.dblClick(await screen.findByRole("button", { name: "文件：文件9.xlsx" }));
    await waitFor(() => expect(window.location.hash).toBe("#projects/p/graph?file=9"));
    expect(localDepth()).toBe(3);

    window.history.back();
    await waitFor(() => expect(window.location.hash).toBe("#projects/p/graph?file=8"));
    expect(await screen.findByRole("heading", { name: "以『文件8.xlsx』为中心" })).toBeInTheDocument();
    expect(localDepth()).toBe(2);

    await userEvent.click(screen.getByRole("button", { name: "回到关系图" }));
    await waitFor(() => expect(window.location.hash).toBe("#projects/p/graph"));
    expect(await screen.findByRole("application", { name: "云图AI 关系图" })).toBeInTheDocument();
    expect(localDepth()).toBeUndefined();
  });

  it("冷启动深链或从别的页进来以后换了中心，［回到关系图］仍回到星图", async () => {
    forgetGraphCache();
    window.history.replaceState(null, "", "/#projects/p/graph?file=7");
    const { unmount } = render(<App apiClient={localClient()} />);
    expect(await screen.findByRole("heading", { name: "以『文件7.xlsx』为中心" })).toBeInTheDocument();
    // 进局部图的那一条记上 localRoot
    await waitFor(() => expect((window.history.state as { localRoot?: boolean } | null)?.localRoot).toBe(true));
    await userEvent.dblClick(await screen.findByRole("button", { name: "文件：文件8.xlsx" }));
    await waitFor(() => expect(window.location.hash).toBe("#projects/p/graph?file=8"));
    await userEvent.dblClick(await screen.findByRole("button", { name: "文件：文件9.xlsx" }));
    await waitFor(() => expect(window.location.hash).toBe("#projects/p/graph?file=9"));
    await userEvent.click(screen.getByRole("button", { name: "回到关系图" }));
    await waitFor(() => expect(window.location.hash).toBe("#projects/p/graph"));
    expect(await screen.findByRole("application", { name: "云图AI 关系图" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: /为中心$/ })).toBeNull();
    expect((window.history.state as { localRoot?: boolean } | null)?.localRoot).toBeUndefined();
    unmount();

    // 从别的页进来（这一条也没有 localDepth）：同样
    forgetGraphCache();
    window.history.replaceState(null, "", "/#tasks");
    render(<App apiClient={localClient()} />);
    await screen.findByRole("heading", { name: "任务" });
    window.history.pushState({ app: true }, "", "/#projects/p/graph?file=7");
    window.dispatchEvent(new PopStateEvent("popstate", { state: window.history.state }));
    expect(await screen.findByRole("heading", { name: "以『文件7.xlsx』为中心" })).toBeInTheDocument();
    await waitFor(() => expect((window.history.state as { localRoot?: boolean } | null)?.localRoot).toBe(true));
    await userEvent.dblClick(await screen.findByRole("button", { name: "文件：文件8.xlsx" }));
    await waitFor(() => expect(window.location.hash).toBe("#projects/p/graph?file=8"));
    await userEvent.click(screen.getByRole("button", { name: "回到关系图" }));
    await waitFor(() => expect(window.location.hash).toBe("#projects/p/graph"));
    expect(await screen.findByRole("application", { name: "云图AI 关系图" })).toBeInTheDocument();
  });

  it("expand 和 file 同时有时留 expand；手机上 ?file= 退回项目列表", async () => {
    forgetGraphCache();
    window.history.replaceState(null, "", "/#projects/p/graph?expand=a&file=7");
    const apiClient = localClient();
    const { unmount } = render(<App apiClient={apiClient} />);
    expect(await screen.findByRole("application", { name: "展开的会：初审规则沟通 a" })).toBeInTheDocument();
    await waitFor(() => expect(window.location.hash).toBe("#projects/p/graph?expand=a"));
    expect(apiClient.graphFileMap).not.toHaveBeenCalled();
    unmount();

    forgetGraphCache();
    vi.stubGlobal("matchMedia", vi.fn(() => ({ ...desktopMatchMedia(), matches: true })));
    window.history.replaceState(null, "", "/#projects/p/graph?file=7");
    const mobile = localClient();
    render(<App apiClient={mobile} />);
    expect(await screen.findByRole("heading", { name: "项目" })).toBeInTheDocument();
    await waitFor(() => expect(window.location.hash).toBe("#projects"));
    expect(mobile.graphFileMap).not.toHaveBeenCalled();
  });
});

describe("全部项目概览的地址 #graph", () => {
  function overviewClient() {
    return client({
      projects: vi.fn().mockResolvedValue([
        { id: "a", name: "云图AI", color: "#2c8d83" },
        { id: "b", name: "数据中台", color: "#7a5af8" },
      ]),
      getGraphOverview: vi.fn().mockResolvedValue({ overview: overviewPayload(), etag: 'W/"o-1"' }),
      getOverviewFolders: vi.fn().mockResolvedValue(foldersPayload()),
    } as unknown as Partial<ApiClient>);
  }

  it("冷加载 #graph?sel=p:a 打开概览并选中那个岛；换选中只改地址栏不压历史", async () => {
    forgetOverviewCache();
    window.history.replaceState(null, "", "/#graph?sel=p:a");
    render(<App apiClient={overviewClient()} />);

    const panel = await screen.findByRole("complementary", { name: "详情面板" });
    expect(within(panel).getByRole("heading", { name: "云图AI" })).toBeInTheDocument();
    expect(window.location.hash).toBe("#graph?sel=p:a");
    expect(screen.getByRole("button", { name: "关系图" })).toHaveAttribute("aria-current", "page");

    const depth = window.history.length;
    await userEvent.click(screen.getByRole("button", { name: /^项目：数据中台/ }));
    await waitFor(() => expect(window.location.hash).toBe("#graph?sel=p:b"));
    expect(window.history.length).toBe(depth);
  });

  it("左侧导航「关系图」打开概览，地址是 #graph", async () => {
    forgetOverviewCache();
    const apiClient = overviewClient();
    render(<App apiClient={apiClient} />);
    await userEvent.click(await screen.findByRole("button", { name: "关系图" }));
    expect(await screen.findByRole("application", { name: "全部项目关系图" })).toBeInTheDocument();
    await waitFor(() => expect(window.location.hash).toBe("#graph"));
    expect(apiClient.getGraphOverview).toHaveBeenCalledWith("28d", null);
  });

  it("手机上打开 #graph 退回项目列表，也没有「关系图」导航", async () => {
    forgetOverviewCache();
    vi.stubGlobal("matchMedia", vi.fn(() => ({ ...desktopMatchMedia(), matches: true })));
    window.history.replaceState(null, "", "/#graph?sel=p:a");
    const apiClient = overviewClient();
    render(<App apiClient={apiClient} />);

    expect(await screen.findByRole("heading", { name: "项目" })).toBeInTheDocument();
    await waitFor(() => expect(window.location.hash).toBe("#projects"));
    expect(screen.queryByRole("button", { name: "关系图" })).not.toBeInTheDocument();
    expect(apiClient.getGraphOverview).not.toHaveBeenCalled();
  });
});

describe("材料预览抽屉（3e）", () => {
  it("项目页读不了的列表点［预览］打开根部的抽屉，本机打开有［在访达中显示］，关了回到项目页", async () => {
    const user = userEvent.setup();
    window.history.replaceState(null, "", "/#projects/project-1");
    const root = { id: 1, project_id: "project-1", path: "/Volumes/资料盘/云图", exists: true, created_at: "2026-09-01T00:00:00Z" };
    const getMaterialPreview = vi.fn().mockResolvedValue({
      file: {
        id: 7,
        name: "报价单.xlsx",
        ext: "xlsx",
        rel_path: "报价单.xlsx",
        root_id: 1,
        folder_path: "/Volumes/资料盘/云图",
        path: "/Volumes/资料盘/云图/报价单.xlsx",
        size: 100,
        modified_at: null,
        project_id: "project-1",
        project_name: "云图",
        root_online: true,
        gone: false,
      },
      state: {
        kind: "unreadable",
        reason: "password",
        note: null,
        what: null,
        paused: null,
        meeting: null,
        text: "读不了：要密码。文件名照样能搜到",
      },
      preview: {
        kind: "none",
        lines: [],
        more: false,
        rows: [],
        sheet: null,
        image_url: null,
        page_url: null,
        media_url: null,
        playable: false,
        duration_ms: null,
        transcript: [],
      },
      mentions: [],
      deliverables: [],
    });
    const apiClient = client({
      bootstrap: vi.fn().mockResolvedValue({
        csrf_token: "token",
        mobile_read_only: true,
        mobile_task_write: true,
        semantic_enabled: true,
        pending_confirm_count: 0,
        can_reveal: true,
      }),
      projects: vi.fn().mockResolvedValue([{ id: "project-1", name: "云图", color: "#2c8d83" }]),
      projectBoard: vi.fn().mockResolvedValue({
        id: "project-1",
        name: "云图",
        color: "#2c8d83",
        meeting_count: 0,
        requirement_counts: { active: 0, done: 0, shelved: 0, all: 0 },
        open_task_count: 0,
        material_roots: [root],
        meetings: [],
      }),
      projectMaterialSubfolders: vi.fn().mockResolvedValue({ roots: [] }),
      projectMeetings: vi.fn().mockResolvedValue([]),
      requirements: vi.fn().mockResolvedValue({
        items: [],
        total: 0,
        limit: 10,
        offset: 0,
        counts: { active: 0, done: 0, shelved: 0, all: 0 },
      }),
      getMaterialIndexStatus: vi.fn().mockResolvedValue({
        roots: [
          {
            root_id: 1,
            project_id: "project-1",
            path: root.path,
            state: "done",
            files: 3,
            name_only_dirs: 0,
            indexed_once: true,
            last_full_at: null,
            updated_at: null,
            error: null,
          },
        ],
      }),
      getMaterialCoverage: vi.fn().mockResolvedValue({
        roots: [
          {
            root_id: 1,
            project_id: "project-1",
            path: root.path,
            state: "done",
            online: true,
            names: { files: 3, name_only_dirs: 0, symlinks: 0 },
            content: {
              total: 3,
              done: 2,
              pending: 0,
              paused: null,
              waiting: [],
              unreadable: { password: 1, corrupt: 0, unsupported: 0, timeout: 0, permission: 0 },
              notes: { small_image: 0, no_text: 0, no_speech: 0, truncated: 0, meeting_audio: 0 },
              names_only: { cards: 0, other: 0 },
            },
          },
        ],
      }),
      getMaterialUnreadable: vi.fn().mockResolvedValue({
        items: [
          {
            file_id: 7,
            name: "报价单.xlsx",
            rel_path: "报价单.xlsx",
            path: "/Volumes/资料盘/云图/报价单.xlsx",
            root_id: 1,
            reason: "password",
            checked_at: null,
          },
        ],
        total: 1,
        next_offset: null,
      }),
      getMaterialPreview,
    } as Partial<ApiClient>);

    render(<App apiClient={apiClient} />);

    await user.click(await screen.findByRole("button", { name: "看看" }));
    await user.click(await screen.findByRole("button", { name: "预览" }));
    const drawer = await screen.findByRole("dialog", { name: "材料预览" });
    expect(await within(drawer).findByText("读不了：要密码。文件名照样能搜到")).toBeInTheDocument();
    expect(getMaterialPreview).toHaveBeenCalledWith(7);
    expect(within(drawer).getByRole("button", { name: "在访达中显示" })).toBeInTheDocument();
    expect(within(drawer).getByRole("button", { name: "在关系图里看" })).toBeInTheDocument();

    await user.click(within(drawer).getByRole("button", { name: "关闭" }));
    expect(screen.queryByRole("dialog", { name: "材料预览" })).toBeNull();
    expect(screen.getByText("内容都读完了，读不了 1 个（要密码 1）")).toBeInTheDocument();
  });
});

describe("搜索里的材料（3f）", () => {
  it("录音文字命中的 ▶ 打开预览抽屉，不等预览数据就从那个时间放", async () => {
    const user = userEvent.setup();
    vi.spyOn(HTMLMediaElement.prototype, "load").mockImplementation(() => undefined);
    vi.spyOn(HTMLMediaElement.prototype, "play").mockImplementation(() => Promise.resolve());
    const search = vi.fn().mockResolvedValue({
      mode: "hybrid",
      items: [],
      similar: [],
      materials: [
        {
          file_id: 9,
          content_key: "k-9",
          name: "访谈.m4a",
          ext: "m4a",
          path: "/Volumes/资料盘/云图/访谈.m4a",
          rel_path: "访谈.m4a",
          folder_path: "/Volumes/资料盘/云图",
          root_id: 1,
          project_id: "p",
          project_name: "云图",
          project_color: null,
          modified_at: null,
          root_online: true,
          playable: true,
          copies: 0,
          name_hit: false,
          hits: [{ kind: "media", loc: null, start_ms: 92_000, text: "报价单下周给", matched: "报价单" }],
          more_hits: 0,
          state_text: null,
          mentioned_meetings: 0,
        },
      ],
      material_similar: [],
      material_state: { pending: 0, rebuilding: false, partial: false },
    });
    const getMaterialPreview = vi.fn(() => new Promise(() => undefined));
    render(<App apiClient={client({ search, getMaterialPreview } as Partial<ApiClient>)} />);

    const input = await screen.findByLabelText("全局检索");
    expect(input).toHaveAttribute("maxLength", "200");
    expect(input).toHaveAttribute("placeholder", "搜索会议、原句、材料或关键词");
    await user.type(input, "报价单");
    await user.click(screen.getByRole("button", { name: "检索" }));
    await user.click(await screen.findByRole("button", { name: "从 01:32 放 访谈.m4a" }));

    const drawer = await screen.findByRole("dialog", { name: "材料预览" });
    expect(getMaterialPreview).toHaveBeenCalledWith(9);
    expect(drawer.querySelector("audio")?.getAttribute("src")).toBe("/api/materials/files/9/media");
  });
});
