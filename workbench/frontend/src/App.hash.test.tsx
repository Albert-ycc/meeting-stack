import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import App, { MOBILE_READ_ONLY_QUERY } from "./App";
import type { ApiClient } from "./api";
import { forgetOverviewCache } from "./components/graph/OverviewGraph";
import { foldersPayload, overviewPayload } from "./components/graph/overviewFixtures";
import { forgetGraphCache } from "./components/graph/ProjectGraph";
import { candidateItem, CVM, EXPORT_SOURCE } from "./components/pool/poolFixtures";
import { focusPayload, payload } from "./components/graph/testFixtures";
import {
  POOL_PRIORITIES_KEY,
  POOL_PRIORITIES_STORE,
  POOL_PROJECTS_KEY,
  POOL_PROJECTS_STORE,
  POOL_QUERY_KEY,
  POOL_QUERY_STORE,
  POOL_TAB_KEY,
} from "./components/pool/RequirementPoolPage";
import { readPersistentState, writePersistentState } from "./viewState";

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
    todo: vi.fn().mockResolvedValue({
      today: "2026-09-30",
      week_end: "2026-10-04",
      total: 0,
      groups: [],
      counts: {},
      project_counts: {},
      projects: [],
    }),
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
  it("冷加载带 #tasks 停在待办页，锚点不被首帧清掉", async () => {
    window.history.replaceState(null, "", "/#tasks");

    render(<App apiClient={client()} />);

    expect(await screen.findByRole("heading", { name: "待办" })).toBeInTheDocument();
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
    // 取数在页面挂上以后的 effect 里：标题先出来，调用可能晚一拍
    await waitFor(() => expect(requirementPool).toHaveBeenCalledWith(expect.objectContaining({ status: "active" })));
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
    await waitFor(() => expect(requirementCandidate).toHaveBeenCalledWith("candidate-1"));
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

  it("认领后新海报会被本机记着的筛选挡住：回需求池时清空筛选，提示里说一声", async () => {
    window.localStorage.setItem("meeting-workbench:view:requirementPool.priorities", JSON.stringify(["P0"]));
    window.history.replaceState(null, "", "/#requirements/claim/candidate-receipt");
    const requirementCandidate = vi.fn().mockResolvedValue({ ...candidateItem(), sources: [] });
    const claimCandidate = vi.fn().mockResolvedValue({
      id: "requirement-receipt",
      title: "京东仓签收凭证",
      project_id: candidateItem().project_id,
      priority: "P2",
    });
    const requirementPool = vi.fn().mockResolvedValue(emptyPool());
    render(<App apiClient={client({ requirementCandidate, claimCandidate, requirementPool })} />);

    await screen.findByDisplayValue("京东仓签收凭证");
    await userEvent.click(screen.getByRole("button", { name: "认领" }));

    expect(await screen.findByRole("heading", { name: "需求池" })).toBeInTheDocument();
    await waitFor(() =>
      expect(screen.getByRole("status")).toHaveTextContent(
        "已认领「京东仓签收凭证」；原来的筛选会挡住它，已清空筛选",
      ),
    );
    await waitFor(() =>
      expect(requirementPool).toHaveBeenLastCalledWith(expect.objectContaining({ status: "active", priority: undefined })),
    );
  });

  it("冷加载带 #requirements/<id>/edit 打开修改需求页", async () => {
    window.history.replaceState(null, "", "/#requirements/requirement-jd/edit");
    const requirement = vi.fn().mockReturnValue(new Promise(() => {}));
    render(<App apiClient={client({ requirement })} />);

    expect(await screen.findByRole("heading", { name: "修改需求" })).toBeInTheDocument();
    await waitFor(() => expect(requirement).toHaveBeenCalledWith("requirement-jd"));
    expect(window.location.hash).toBe("#requirements/requirement-jd/edit");
  });

  it("手机上打开修改需求的地址退回需求详情（手机端本期不改）", async () => {
    vi.stubGlobal("matchMedia", vi.fn(() => ({ ...desktopMatchMedia(), matches: true })));
    window.history.replaceState(null, "", "/#requirements/requirement-jd/edit");
    const requirement = vi.fn().mockReturnValue(new Promise(() => {}));
    render(<App apiClient={client({ requirement })} />);

    expect(await screen.findByText("正在读取需求…")).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "修改需求" })).not.toBeInTheDocument();
    await waitFor(() => expect(window.location.hash).toBe("#requirements/requirement-jd"));
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
          projectBoard: vi.fn().mockResolvedValue({ id: "p", name: "云图AI", color: "#2c8d83", meeting_count: 0, material_roots: [], meetings: [] }),
        } as unknown as Partial<ApiClient>)}
      />,
    );

    expect(await screen.findByRole("complementary", { name: "详情面板" })).toBeInTheDocument();
    expect(graph).toHaveBeenCalledWith("p", undefined, "m:a");
    // 面板先渲染、取简报的 effect 后跑：机器忙时要等一下
    await waitFor(() => expect(meetingBrief).toHaveBeenCalledWith("a"));
    expect(window.location.hash).toBe("#projects/p/graph?sel=m:a");
    // 关系图是项目详情的一个标签页，地址栏带着它时标签页停在这里
    expect(await screen.findByRole("tab", { name: "关系图" })).toHaveAttribute("aria-selected", "true");

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
    await screen.findByRole("heading", { name: "待办" });
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

    // 材料根目录在项目详情的「材料」标签页里
    await user.click(await screen.findByRole("tab", { name: "材料" }));
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

// 云课堂那场会：会名、录音时间、时长、逐字稿照生产库（同 TranscriptPanel 用例、后端 requirement_pool_world）
const CVM_MEETING = {
  id: EXPORT_SOURCE.meeting_id,
  title: EXPORT_SOURCE.meeting_title,
  status: "published",
  tags: [],
  recording_date: EXPORT_SOURCE.recording_date,
  duration_ms: EXPORT_SOURCE.duration_ms,
  project_id: CVM,
  project_name: "CVM 云讲堂",
  artifacts: [],
  segments: (
    [
      [568390, "SPEAKER_03", "预约审核查看。"],
      [576900, "SPEAKER_01", "那我有办法导出 excel 吗？"],
      [581000, "SPEAKER_01", "是没有办法，"],
    ] as const
  ).map(([start, speaker, text], ordinal) => ({
    id: `seg-${start}`,
    ordinal,
    start_ms: start,
    end_ms: start + 1000,
    speaker_label: speaker,
    text,
  })),
  speakers: [],
  events: [],
  current_transcript_version_id: "tv-cvm",
  transcript_versions: [
    { id: "tv-cvm", meeting_id: EXPORT_SOURCE.meeting_id, version_no: 1, kind: "funasr", published: 0, created_at: "2026-09-30T02:40:40Z" },
  ],
  minutes_versions: [],
};

function meetingClient(overrides: Partial<ApiClient> = {}) {
  return client({
    meetings: vi.fn().mockResolvedValue({
      items: [{ id: CVM_MEETING.id, title: CVM_MEETING.title, status: "published", tags: [] }],
      limit: 50,
      offset: 0,
      total: 1,
    }),
    meeting: vi.fn().mockResolvedValue(CVM_MEETING),
    projects: vi.fn().mockResolvedValue([{ id: CVM, name: "CVM 云讲堂", color: "#f0783b", seat: 4 }]),
    transcriptVersionSegments: vi.fn(),
    ...overrides,
  } as Partial<ApiClient>);
}

async function openCvmFromLibrary() {
  fireEvent.click(await screen.findByRole("button", { name: "录音档案" }));
  await userEvent.click(await screen.findByRole("button", { name: /260929 云课堂直播运营问题对齐/ }));
  await screen.findByRole("heading", { name: "260929 云课堂直播运营问题对齐" });
}

/** 在逐字稿里选中「那我有办法导出 excel 吗？是没有办法，」，点［建成需求］ */
async function pickExportQuote() {
  const textOf = (id: string) => screen.getByTestId(`segment-seg-${id}`).querySelector(".segment-text")!.firstChild!;
  const range = document.createRange();
  range.setStart(textOf("576900"), 0);
  range.setEnd(textOf("581000"), 6);
  window.getSelection()!.removeAllRanges();
  window.getSelection()!.addRange(range);
  fireEvent.mouseUp(screen.getByTestId("segment-seg-581000"));
  await userEvent.click(screen.getByRole("button", { name: "建成需求" }));
}

function currentNav() {
  return screen.getAllByRole("button").find((button) => button.getAttribute("aria-current") === "page");
}

describe("需求二级页的来去（R04-1、R04-8）", () => {
  it("从逐字稿选句进新增页再取消：原地回到那场会，返回按钮、侧栏都还是原来的，会议不重读（审查 M7）", async () => {
    const api = meetingClient();
    render(<App apiClient={api} />);
    await openCvmFromLibrary();

    await pickExportQuote();

    expect(await screen.findByRole("heading", { name: "新增需求" })).toBeInTheDocument();
    await waitFor(() => expect(window.location.hash).toBe("#requirements/new"));
    expect(screen.queryByRole("heading", { name: "260929 云课堂直播运营问题对齐" })).not.toBeInTheDocument();
    expect(currentNav()).toHaveTextContent("需求池");
    // 面包屑第一段写这场会是从哪儿打开的（和会议页的返回按钮一致）
    expect(screen.getByText("录音档案 / 260929 云课堂直播运营问题对齐 /")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "取消" }));

    expect(await screen.findByRole("heading", { name: "260929 云课堂直播运营问题对齐" })).toBeInTheDocument();
    await waitFor(() => expect(window.location.hash).toBe(`#meetings/${CVM_MEETING.id}`));
    expect(screen.queryByRole("heading", { name: "新增需求" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "← 返回录音档案" })).toBeInTheDocument();
    expect(currentNav()).toHaveTextContent("录音档案");
    expect(api.meeting).toHaveBeenCalledTimes(1);
  });

  it("新增页盖在会议页上：会议页不卸载，取消回来逐字稿里的查找词还在（不只是不重读接口）", async () => {
    render(<App apiClient={meetingClient()} />);
    await openCvmFromLibrary();
    await userEvent.type(screen.getByLabelText("在本次逐字稿中搜索"), "导出");
    expect(screen.queryByTestId("segment-seg-568390")).not.toBeInTheDocument();
    const text = screen.getByTestId("segment-seg-576900").querySelector(".segment-text")!.firstChild!;
    const range = document.createRange();
    range.setStart(text, 0);
    range.setEnd(text, text.textContent!.length);
    window.getSelection()!.removeAllRanges();
    window.getSelection()!.addRange(range);
    fireEvent.mouseUp(screen.getByTestId("segment-seg-576900"));
    await userEvent.click(screen.getByRole("button", { name: "建成需求" }));
    await screen.findByRole("heading", { name: "新增需求" });

    await userEvent.click(screen.getByRole("button", { name: "取消" }));

    expect(await screen.findByRole("heading", { name: "260929 云课堂直播运营问题对齐" })).toBeInTheDocument();
    expect(screen.getByLabelText("在本次逐字稿中搜索")).toHaveValue("导出");
    expect(screen.queryByTestId("segment-seg-568390")).not.toBeInTheDocument();
  });

  it("从逐字稿选句建成需求：直接进新需求的详情页，提示「需求已创建」；后退回到那场会，返回按钮还是原来的（R04-8）", async () => {
    const created = {
      id: "requirement-export",
      project_id: CVM,
      project_name: "CVM 云讲堂",
      project_color: "#f0783b",
      title: "科室会预约后台导出",
      summary: "",
      priority: "P2",
      status: "active",
      created_at: "2026-10-01T09:00:00Z",
      updated_at: "2026-10-01T09:00:00Z",
      open_task_count: 0,
      meeting_count: 1,
      latest_meeting_date: EXPORT_SOURCE.recording_date,
      folder_count: 0,
      folders: [],
      meetings: [],
      tasks: [],
      source: EXPORT_SOURCE,
      sources: [EXPORT_SOURCE],
    };
    const createRequirement = vi.fn().mockResolvedValue(created);
    const api = meetingClient({ createRequirement, requirement: vi.fn().mockResolvedValue(created) });
    render(<App apiClient={api} />);
    await openCvmFromLibrary();
    await pickExportQuote();

    await userEvent.type(await screen.findByLabelText("需求名"), "科室会预约后台导出");
    await userEvent.click(screen.getByRole("button", { name: "创建" }));

    expect(await screen.findByRole("heading", { name: "科室会预约后台导出" })).toBeInTheDocument();
    await waitFor(() => expect(window.location.hash).toBe("#requirements/requirement-export"));
    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("需求已创建"));
    expect(createRequirement).toHaveBeenCalledWith(
      expect.objectContaining({
        title: "科室会预约后台导出",
        project_id: CVM,
        source: expect.objectContaining({ quote: "那我有办法导出 excel 吗？是没有办法，", anchor_ms: 576900 }),
      }),
    );

    // 新增页那一条换成了详情页：后退一次就回到选句的那场会
    act(() => window.history.back());

    expect(await screen.findByRole("heading", { name: "260929 云课堂直播运营问题对齐" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "← 返回录音档案" })).toBeInTheDocument();
    expect(currentNav()).toHaveTextContent("录音档案");
    // 会议是重新打开的：停在刚才选的那一句，接着往下挑（第二轮审查建议 1）
    expect(screen.getByTestId("segment-seg-576900")).toHaveClass("is-current");
  });

  it("取消回到会议以后按浏览器前进：重新盖上那张带着原话的新增页，会议页不关（第二轮审查一般-3）", async () => {
    const api = meetingClient();
    render(<App apiClient={api} />);
    await openCvmFromLibrary();
    await pickExportQuote();
    await screen.findByRole("heading", { name: "新增需求" });
    await userEvent.click(screen.getByRole("button", { name: "取消" }));
    await screen.findByRole("heading", { name: "260929 云课堂直播运营问题对齐" });

    act(() => window.history.forward());

    expect(await screen.findByRole("heading", { name: "新增需求" })).toBeInTheDocument();
    expect(screen.getAllByText(/那我有办法导出 excel 吗？是没有办法，/).length).toBeGreaterThan(0);
    await userEvent.click(screen.getByRole("button", { name: "取消" }));
    expect(await screen.findByRole("heading", { name: "260929 云课堂直播运营问题对齐" })).toBeInTheDocument();
    expect(api.meeting).toHaveBeenCalledTimes(1);
  });

  it("盖着新增页去了别处再后退回来：还是那张带着原话的新增页（会议已经关了，按普通新增页打开）", async () => {
    render(<App apiClient={meetingClient()} />);
    await openCvmFromLibrary();
    await pickExportQuote();
    await screen.findByRole("heading", { name: "新增需求" });

    // 侧栏「任务池」已改名「待办」（项目页与待办改版 R07-1）
    fireEvent.click(screen.getByRole("button", { name: "待办" }));
    await screen.findByRole("heading", { name: "待办" });
    act(() => window.history.back());

    expect(await screen.findByRole("heading", { name: "新增需求" })).toBeInTheDocument();
    expect(screen.getAllByText(/那我有办法导出 excel 吗？是没有办法，/).length).toBeGreaterThan(0);
    expect(window.location.hash).toBe("#requirements/new");
  });

  it("需求详情「出自录音」上点原话时间：打开那场会、跳到那一秒并开始放（R02-3、R05-1）", async () => {
    vi.stubGlobal("fetch", vi.fn(() => new Promise(() => {})));
    const play = vi.spyOn(HTMLMediaElement.prototype, "play").mockResolvedValue(undefined);
    window.history.replaceState(null, "", "/#requirements/requirement-export");
    const detail = {
      id: "requirement-export",
      project_id: CVM,
      project_name: "CVM 云讲堂",
      project_color: "#f0783b",
      title: "科室会预约后台导出",
      summary: "",
      priority: "P2",
      status: "active",
      created_at: "2026-10-01T09:00:00Z",
      updated_at: "2026-10-01T09:00:00Z",
      open_task_count: 0,
      meeting_count: 1,
      latest_meeting_date: EXPORT_SOURCE.recording_date,
      folder_count: 0,
      folders: [],
      meetings: [],
      tasks: [],
      source: EXPORT_SOURCE,
      sources: [EXPORT_SOURCE],
    };
    const withAudio = { ...CVM_MEETING, artifacts: [{ id: 42, kind: "audio", role: "source", path: "/x/cvm.m4a" }] };
    render(
      <App
        apiClient={meetingClient({
          requirement: vi.fn().mockResolvedValue(detail),
          meeting: vi.fn().mockResolvedValue(withAudio),
        } as Partial<ApiClient>)}
      />,
    );

    await userEvent.click(await screen.findByRole("button", { name: "从 00:09:36 开始放 260929 云课堂直播运营问题对齐" }));
    await screen.findByRole("heading", { name: "260929 云课堂直播运营问题对齐" });
    const audio = document.querySelector("audio")!;
    Object.defineProperty(audio, "readyState", { configurable: true, value: HTMLMediaElement.HAVE_METADATA });
    fireEvent(audio, new Event("loadedmetadata"));

    expect(play).toHaveBeenCalledTimes(1);
    expect(screen.getByTestId("segment-seg-576900")).toHaveClass("is-current");

    // 这场会是从需求详情打开的：在这里选句建需求，面包屑第一段写「需求详情」，和返回按钮一致（第二轮审查建议 4）
    expect(screen.getByRole("button", { name: "← 返回需求详情" })).toBeInTheDocument();
    await pickExportQuote();
    expect(await screen.findByText("需求详情 / 260929 云课堂直播运营问题对齐 /")).toBeInTheDocument();
    play.mockRestore();
  });

  it("新增页改了没存：点侧栏、浏览器后退都先问；选留下就还在新增页，选离开才走（审查 B1）", async () => {
    render(<App apiClient={meetingClient()} />);
    await openCvmFromLibrary();
    await pickExportQuote();
    await userEvent.type(await screen.findByLabelText("需求名"), "科室会预约后台导出");
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);

    fireEvent.click(screen.getByRole("button", { name: "工作台" }));
    expect(confirm).toHaveBeenLastCalledWith("当前需求仍有未保存修改。放弃这些修改并离开吗？");
    expect(screen.getByRole("heading", { name: "新增需求" })).toBeInTheDocument();

    act(() => window.history.back());
    await waitFor(() => expect(confirm).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(window.location.hash).toBe("#requirements/new"));
    expect(screen.getByRole("heading", { name: "新增需求" })).toBeInTheDocument();
    expect(screen.getByLabelText("需求名")).toHaveValue("科室会预约后台导出");

    confirm.mockReturnValue(true);
    fireEvent.click(screen.getByRole("button", { name: "录音档案" }));
    expect(await screen.findByText("会议录音档案")).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "新增需求" })).not.toBeInTheDocument();
    expect(confirm).toHaveBeenCalledTimes(3);
  });
});

describe("从项目列表进项目详情", () => {
  const board = { id: "p", name: "云图AI", color: "#2c8d83", meeting_count: 0, material_roots: [], meetings: [] };
  function listClient() {
    return client({
      projects: vi.fn().mockResolvedValue([{ id: "p", name: "云图AI", color: "#2c8d83" }]),
      projectBoard: vi.fn().mockResolvedValue(board),
      projectWork: vi.fn().mockResolvedValue({
        project_id: "p",
        requirements: [],
        closed_requirements: [],
        unlinked_tasks: [],
        pending_candidates: [],
      }),
      requirementPool: vi.fn().mockResolvedValue({
        items: [],
        total: 0,
        limit: 200,
        offset: 0,
        status: "active",
        counts: { pending: 0, active: 0, done: 0, shelved: 0, all: 0 },
        projects: [{ id: "p", name: "云图AI", color: "#2c8d83", seat: null, latest_meeting_date: null, count: 0 }],
        unassigned_count: 0,
        dropped_count: 0,
      }),
      graph: vi.fn().mockResolvedValue(payload()),
      graphRoots: vi.fn().mockResolvedValue({ roots: [], folders: [], loose: { count: 0, recent: [] }, checking: false }),
    } as unknown as Partial<ApiClient>);
  }

  async function openFromList() {
    window.history.replaceState(null, "", "/#projects");
    render(<App apiClient={listClient()} />);
    const names = await screen.findAllByText("云图AI");
    await userEvent.click(names[names.length - 1]);
    return screen.findByRole("tab", { name: "需求与任务" });
  }

  it("没存过视图偏好：落在「需求与任务」，不是关系图", async () => {
    window.localStorage.removeItem("meeting-workbench:graph:mode.p");
    expect(await openFromList()).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("tab", { name: "关系图" })).toHaveAttribute("aria-selected", "false");
  });

  it("以前存过「关系图」偏好：也落在「需求与任务」，只有点关系图标签才去关系图", async () => {
    window.localStorage.setItem("meeting-workbench:graph:mode.p", "graph");
    try {
      expect(await openFromList()).toHaveAttribute("aria-selected", "true");
      await userEvent.click(screen.getByRole("tab", { name: "关系图" }));
      expect(screen.getByRole("tab", { name: "关系图" })).toHaveAttribute("aria-selected", "true");
    } finally {
      window.localStorage.removeItem("meeting-workbench:graph:mode.p");
    }
  });

  it("「去认领」把需求池切到待认领、只筛本项目，并清掉记着的等级筛选和搜索词", async () => {
    writePersistentState(POOL_PRIORITIES_KEY, ["P0"], POOL_PRIORITIES_STORE);
    writePersistentState(POOL_QUERY_KEY, "对接", POOL_QUERY_STORE);
    window.history.replaceState(null, "", "/#projects");
    const api = listClient();
    api.projectWork = vi.fn().mockResolvedValue({
      project_id: "p",
      requirements: [],
      closed_requirements: [],
      unlinked_tasks: [],
      pending_candidates: [{ id: "candidate-receipt", title: "京东仓签收凭证" }],
    });
    render(<App apiClient={api} />);
    const names = await screen.findAllByText("云图AI");
    await userEvent.click(names[names.length - 1]);
    await userEvent.click(await screen.findByRole("button", { name: /去认领/ }));

    expect(readPersistentState(POOL_TAB_KEY, "active", { local: true })).toBe("pending");
    expect(readPersistentState<string[]>(POOL_PROJECTS_KEY, [], POOL_PROJECTS_STORE)).toEqual(["p"]);
    expect(readPersistentState<string[]>(POOL_PRIORITIES_KEY, ["x"], POOL_PRIORITIES_STORE)).toEqual([]);
    expect(readPersistentState(POOL_QUERY_KEY, "x", POOL_QUERY_STORE)).toBe("");
  });
});
