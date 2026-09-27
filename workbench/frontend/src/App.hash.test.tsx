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

  it("冷加载带 #requirements 停在需求池页", async () => {
    window.history.replaceState(null, "", "/#requirements");

    render(
      <App
        apiClient={client({
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
    expect(meetingBrief).toHaveBeenCalledWith("a");
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
