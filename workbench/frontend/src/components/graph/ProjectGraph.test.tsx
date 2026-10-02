import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState, type ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError, ApiTimeoutError, type ApiClient, type LinksState, type RelationQuestion } from "../../api";
import { LinksFlagsContext } from "../links/LinksFlagsContext";
import type { Project } from "../../types";
import type { MaterialFilePreview, Task } from "../../types";
import type {
  BriefFile,
  ExpandPayload,
  FilesState,
  GraphFileDetail,
  GraphPayload,
  GraphRootsPayload,
  GraphWindow,
  MeetingBrief,
  RecentFile,
} from "./graphTypes";
import { forgetAskStore, hasDraft, setDraft as setAskDraft } from "../ask/askStore";
import { ProjectGraph, forgetGraphCache, shortHash } from "./ProjectGraph";
import { day, focusPayload, focusTask, meeting, payload, requirement } from "./testFixtures";

const PROJECTS: Project[] = [
  { id: "p", name: "云图AI", color: "#2c8d83" },
  { id: "q", name: "数据中台", color: "#7a5af8" },
];

const ROOTS: GraphRootsPayload = { roots: [], folders: [], loose: { count: 0, recent: [] }, checking: false };

/** graph() 的返回：200 带整张图和 etag；etag 没变时 graph 是 null（304） */
const fetched = (graph: GraphPayload | null, etag: string | null = null) => ({ graph, etag });

function brief(meetingId: string): MeetingBrief {
  return {
    meeting: {
      id: meetingId,
      title: `初审规则沟通 ${meetingId}`,
      date: "2026-09-26",
      duration_ms: 1_800_000,
      project_id: "p",
      project_name: "云图AI",
      project_color: "#2c8d83",
      has_minutes: true,
      audio_url: `/api/meetings/${meetingId}/audio`,
    },
    attribution: {
      state: "auto",
      project_id: "p",
      origin: "ai",
      method: "literal",
      evidence: [],
      candidates: [],
      reason: "",
      new_project_name: null,
      reassigned_from: null,
      ai_configured: true,
    },
    evidence_quotes: [],
    summary: "定了初审规则的口径",
    decisions: [{ text: "初审规则按新口径执行", start_ms: 65_000 }],
    decisions_note: null,
    tasks: [],
    tasks_more: 0,
    requirements: [],
    card: null,
    files: [],
    files_state: "done",
  } as MeetingBrief;
}

function withDoorstep(): GraphPayload {
  return payload({
    doorstep: [
      {
        id: "d:door0",
        meeting_id: "door0",
        title: "门口的会",
        date: day(1),
        age_days: 1,
        candidates: [
          { project_id: "p", project_name: "云图AI", project_color: "#2c8d83", count: 2 },
          { project_id: "q", project_name: "数据中台", project_color: "#7a5af8", count: 1 },
        ],
        reason: "",
      },
    ],
  });
}

function expandPayload(rootId: number, dir: string): ExpandPayload {
  const base = "/材料/云图AI";
  const path = dir ? `${base}/${dir}` : base;
  return {
    root_id: rootId,
    project_id: "p",
    root_path: base,
    dir,
    path,
    state: "online",
    crumbs: dir ? dir.split("/").map((name, index, parts) => ({ name, dir: parts.slice(0, index + 1).join("/") })) : [],
    dirs: dir ? [] : [{ name: "初审规则", dir: "初审规则", path: `${base}/初审规则`, mtime: "2026-09-25T10:00:00" }],
    dirs_total: dir ? 0 : 1,
    files: [{ name: dir ? "口径说明.docx" : "报价单.xlsx", path: `${path}/x`, size: 2048, mtime: "2026-09-24T09:00:00" }],
    files_total: 1,
  };
}

function makeClient(graph: GraphPayload = payload(), overrides: Record<string, unknown> = {}) {
  const undoUntil = new Date(Date.now() + 10 * 60_000).toISOString();
  return {
    graph: vi.fn(async () => fetched(graph)),
    graphRoots: vi.fn(async () => ROOTS),
    meetingBrief: vi.fn(async (meetingId: string) => brief(meetingId)),
    updateMeeting: vi.fn(async () => ({ effects: { tasks_moved: 1, tasks_left: [], undo_until: undoUntil } })),
    undoMeetingProject: vi.fn(async () => ({ effects: { tasks_restored: 1 } })),
    graphExpand: vi.fn(async (rootId: number, dir = "") => expandPayload(rootId, dir)),
    addRequirementMeeting: vi.fn(async () => ({})),
    removeRequirementMeeting: vi.fn(async () => ({})),
    ...overrides,
  } as unknown as ApiClient & Record<string, ReturnType<typeof vi.fn>>;
}

interface Handlers {
  onOpenGlossary?: (projectId: string) => void;
  onOpenMeeting?: (meetingId: string) => void;
  onOpenRequirement?: (requirementId: string) => void;
  onOpenPreview?: (fileId: number, startMs?: number) => void;
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
      <ProjectGraph
        apiClient={apiClient}
        focus={initial}
        onBack={() => {}}
        onOpenGlossary={handlers.onOpenGlossary ?? (() => {})}
        onOpenMeeting={handlers.onOpenMeeting ?? (() => {})}
        onOpenPreview={handlers.onOpenPreview}
        onOpenProject={() => {}}
        onOpenRequirement={handlers.onOpenRequirement ?? (() => {})}
        onSelectionChange={setSelection}
        projectId="p"
        projects={PROJECTS}
        selection={selection}
      />
      <span data-testid="selection">{selection ?? ""}</span>
    </>
  );
}

beforeEach(() => {
  forgetGraphCache();
  window.localStorage.clear();
});

afterEach(() => {
  vi.useRealTimers();
});

describe("ProjectGraph", () => {
  it("点会议打开面板，Esc 关闭", async () => {
    const apiClient = makeClient();
    render(<Harness apiClient={apiClient} />);
    await userEvent.click(await screen.findByRole("button", { name: /^会议：初审规则沟通 a/ }));
    const panel = await screen.findByRole("complementary", { name: "详情面板" });
    expect(await within(panel).findByText("定了初审规则的口径")).toBeInTheDocument();
    expect(within(panel).getByText("初审规则按新口径执行")).toBeInTheDocument();
    expect(apiClient.meetingBrief).toHaveBeenCalledWith("a");
    expect(screen.getByTestId("selection")).toHaveTextContent("m:a");

    fireEvent.keyDown(within(panel).getByRole("button", { name: "关闭面板" }), { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("complementary", { name: "详情面板" })).not.toBeInTheDocument());

    // 画布上按 Esc 也能关
    await userEvent.click(screen.getByRole("button", { name: /^会议：初审规则沟通 b/ }));
    await screen.findByRole("complementary", { name: "详情面板" });
    fireEvent.keyDown(screen.getByRole("button", { name: /^会议：初审规则沟通 b/ }), { key: "Escape" });
    await waitFor(() => expect(screen.getByTestId("selection")).toHaveTextContent(""));
  });

  it("点线、点面板里的字之后焦点落在页面上：Esc 照样关面板", async () => {
    const graph = payload({
      edges: [
        { id: "e:disc:r1:a", kind: "discussion", from: "m:a", to: "r:r1", label: "你关联的 a", meeting_id: "a", requirement_id: "r1" },
      ],
    });
    render(<Harness apiClient={makeClient(graph)} />);

    // 线是 SVG 的 path，不能聚焦：点完焦点在 body 上，画布和面板上的 onKeyDown 都收不到 Esc
    await userEvent.click(await screen.findByRole("button", { name: "连线：你关联的 a" }));
    await screen.findByRole("complementary", { name: "详情面板" });
    expect(screen.getByTestId("selection")).toHaveTextContent("e:disc:r1:a");
    expect(document.activeElement).toBe(document.body);
    await userEvent.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("complementary", { name: "详情面板" })).not.toBeInTheDocument());
    expect(screen.getByTestId("selection")).toHaveTextContent("");

    // 点面板里的字：焦点也落回 body
    await userEvent.click(screen.getByRole("button", { name: /^会议：初审规则沟通 a/ }));
    const panel = await screen.findByRole("complementary", { name: "详情面板" });
    await userEvent.click(await within(panel).findByText("定了初审规则的口径"));
    expect(document.activeElement).toBe(document.body);
    await userEvent.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("complementary", { name: "详情面板" })).not.toBeInTheDocument());
  });

  it("面板开着时，输入框里的 Esc 只清搜索不关面板，输入法组合中的 Esc 在页面上也不关", async () => {
    render(<Harness apiClient={makeClient()} />);
    await userEvent.click(await screen.findByRole("button", { name: /^会议：初审规则沟通 a/ }));
    await screen.findByRole("complementary", { name: "详情面板" });

    const input = screen.getByRole("searchbox", { name: "在图上找" });
    await userEvent.type(input, "初审");
    await userEvent.keyboard("{Escape}");
    expect(input).toHaveValue("");
    expect(screen.getByRole("complementary", { name: "详情面板" })).toBeInTheDocument();

    fireEvent.keyDown(document.body, { key: "Escape", isComposing: true });
    fireEvent.keyDown(document.body, { key: "Escape", keyCode: 229 });
    expect(screen.getByRole("complementary", { name: "详情面板" })).toBeInTheDocument();
    expect(screen.getByTestId("selection")).toHaveTextContent("m:a");
  });

  it("门口的［归这里］调接口、弹带撤销的提示，撤销也走接口", async () => {
    const apiClient = makeClient(withDoorstep());
    render(<Harness apiClient={apiClient} />);
    const doorstep = await screen.findByRole("group", { name: /可能是这个项目的会：门口的会/ });
    await userEvent.click(within(doorstep).getByRole("button", { name: "归这里" }));

    expect(apiClient.updateMeeting).toHaveBeenCalledWith("door0", { project_id: "p" });
    const notice = await screen.findByRole("status");
    expect(notice).toHaveTextContent("已归到 云图AI；1 条待确认/过期的任务一起移过去");
    await waitFor(() => expect(apiClient.graph).toHaveBeenCalledTimes(2));

    await userEvent.click(within(notice).getByRole("button", { name: "撤销" }));
    expect(apiClient.undoMeetingProject).toHaveBeenCalledWith("door0");
    expect(await screen.findByText("已撤销刚才的改动")).toBeInTheDocument();
  });

  it("定时重取比写后重取晚回来时，旧数据既不进画面也不进缓存", async () => {
    vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] });
    let releaseStale: (value: GraphPayload) => void = () => undefined;
    const graph = vi
      .fn()
      .mockResolvedValueOnce(fetched(withDoorstep()))
      .mockImplementationOnce(() => new Promise<ReturnType<typeof fetched>>((resolve) => (releaseStale = (value) => resolve(fetched(value)))))
      .mockResolvedValue(fetched(payload()));
    const view = render(<Harness apiClient={makeClient(payload(), { graph })} />);
    await screen.findByRole("button", { name: "可能是这个项目的会：门口的会" });
    act(() => vi.advanceTimersByTime(30_000));
    vi.useRealTimers();
    expect(graph).toHaveBeenCalledTimes(2);
    fireEvent.click(screen.getByRole("button", { name: "都不是" }));
    await waitFor(() => expect(screen.queryByRole("button", { name: "可能是这个项目的会：门口的会" })).toBeNull());
    await act(async () => releaseStale(withDoorstep()));
    expect(screen.queryByRole("button", { name: "可能是这个项目的会：门口的会" })).toBeNull();
    view.unmount();

    // 再进这张图：先画的缓存得是写之后的那份
    render(<Harness apiClient={makeClient(payload(), { graph: vi.fn(() => new Promise(() => undefined)) })} />);
    await screen.findByRole("button", { name: /^会议：初审规则沟通 a/ });
    expect(screen.queryByRole("button", { name: "可能是这个项目的会：门口的会" })).toBeNull();
  });

  it("聚焦过的门口的会作答后从图上消失，会议这一侧的 Tab 停靠点退回到剩下的第一个", async () => {
    const graph = vi.fn().mockResolvedValueOnce(fetched(withDoorstep())).mockResolvedValue(fetched(payload()));
    render(<Harness apiClient={makeClient(payload(), { graph })} />);
    const door = await screen.findByRole("button", { name: "可能是这个项目的会：门口的会" });
    act(() => door.focus());
    fireEvent.click(screen.getByRole("button", { name: "都不是" }));
    await waitFor(() => expect(screen.queryByRole("button", { name: "可能是这个项目的会：门口的会" })).toBeNull());
    const left = screen.getByRole("group", { name: "会议" });
    const stops = Array.from(left.querySelectorAll<HTMLElement>("[data-node-id]")).filter((element) => element.tabIndex === 0);
    expect(stops).toHaveLength(1);
  });

  it("门口的［都不是］把会标成不归项目，［归 另一个］归到那个项目", async () => {
    const apiClient = makeClient(withDoorstep());
    render(<Harness apiClient={apiClient} />);
    const doorstep = await screen.findByRole("group", { name: /可能是这个项目的会：门口的会/ });
    await userEvent.click(within(doorstep).getByRole("button", { name: "归 数据中台" }));
    expect(apiClient.updateMeeting).toHaveBeenLastCalledWith("door0", { project_id: "q" });
    expect(await screen.findByRole("status")).toHaveTextContent("已归到 数据中台");
    await waitFor(() => expect(within(doorstep).getByRole("button", { name: "都不是" })).toBeEnabled());
    await userEvent.click(within(doorstep).getByRole("button", { name: "都不是" }));
    expect(apiClient.updateMeeting).toHaveBeenLastCalledWith("door0", { project_id: "" });
  });

  it("方向键移到最近的节点，Enter 打开面板", async () => {
    const apiClient = makeClient();
    render(<Harness apiClient={apiClient} />);
    const center = await screen.findByRole("button", { name: "云图AI，3 场会" });
    center.focus();
    fireEvent.keyDown(center, { key: "ArrowLeft" });
    expect((document.activeElement as HTMLElement).dataset.nodeId).toMatch(/^m:/);
    fireEvent.keyDown(document.activeElement as HTMLElement, { key: "ArrowUp" });
    fireEvent.keyDown(document.activeElement as HTMLElement, { key: "ArrowUp" });
    expect((document.activeElement as HTMLElement).dataset.nodeId).toMatch(/^(m|r):/);

    center.focus();
    fireEvent.keyDown(center, { key: "ArrowRight" });
    expect((document.activeElement as HTMLElement).dataset.nodeId).toMatch(/^(root:|cards)/);
    await userEvent.keyboard("{Enter}");
    expect(await screen.findByRole("complementary", { name: "详情面板" })).toBeInTheDocument();
  });

  it("打中文时 Esc 和数字键不触发快捷键", async () => {
    const apiClient = makeClient();
    render(<Harness apiClient={apiClient} initial="m:a" />);
    await screen.findByRole("complementary", { name: "详情面板" });
    const node = screen.getByRole("button", { name: /^会议：初审规则沟通 a/ });
    fireEvent.keyDown(node, { key: "Escape", isComposing: true });
    fireEvent.keyDown(node, { key: "Process" });
    expect(screen.getByRole("complementary", { name: "详情面板" })).toBeInTheDocument();
    expect(screen.getByTestId("selection")).toHaveTextContent("m:a");
  });

  it("点状态句里的短语，点亮对应的节点，其余变淡", async () => {
    const graph = payload({
      status: { ok: [], waiting: [{ text: "1 条任务待确认", node_ids: ["m:b"] }], stopped: [], note: "另有 1 场新会还在判断归属" },
    });
    render(<Harness apiClient={makeClient(graph)} />);
    await userEvent.click(await screen.findByRole("button", { name: "1 条任务待确认" }));
    expect(screen.getByRole("button", { name: /^会议：初审规则沟通 b/ })).toHaveClass("is-lit");
    expect(screen.getByRole("button", { name: /^会议：初审规则沟通 a/ })).toHaveClass("is-dim");
    expect(screen.getByText("另有 1 场新会还在判断归属")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "1 条任务待确认" }));
    expect(screen.getByRole("button", { name: /^会议：初审规则沟通 a/ })).not.toHaveClass("is-dim");
  });

  it("切时间窗重新取图，并按项目记住", async () => {
    const apiClient = makeClient();
    render(<Harness apiClient={apiClient} />);
    await screen.findByRole("button", { name: /^会议：初审规则沟通 a/ });
    expect(apiClient.graph).toHaveBeenCalledWith("p", undefined, undefined, null);
    await userEvent.click(screen.getByRole("button", { name: "7 天" }));
    await waitFor(() => expect(apiClient.graph).toHaveBeenLastCalledWith("p", "7d", undefined, null));
    expect(window.localStorage.getItem("meeting-workbench:graph:window.p")).toBe("7d");
  });

  it("自动放宽时间窗时写明原因；深链目标不在图上时说一声并清掉选中", async () => {
    const graph = payload({
      window: { requested: null, effective: "90d", days: 90, widened_reason: "自动放宽到 90 天：28 天内只有 1 场会" },
    });
    const apiClient = makeClient(graph);
    render(<Harness apiClient={apiClient} initial="r:gone" />);
    expect(await screen.findByText("自动放宽到 90 天：28 天内只有 1 场会")).toBeInTheDocument();
    expect(apiClient.graph).toHaveBeenCalledWith("p", undefined, "r:gone", null);
    expect(await screen.findByText("这个需求不在进行中，关系图只画进行中的需求")).toBeInTheDocument();
    await waitFor(() => expect(screen.getByTestId("selection")).toHaveTextContent(""));
    expect(screen.getByRole("button", { name: "90 天" })).toHaveAttribute("aria-pressed", "true");
  });

  it("深链到一场被折叠的会时选中它所在的折叠组", async () => {
    const graph = payload({
      collapsed: [
        { id: "c:older", kind: "older", label: "更早 2 场", count: 2, from: "2026-05-01", to: "2026-08-20", meeting_ids: ["old1", "old2"], ring: "outer" },
      ],
    });
    const apiClient = makeClient(graph, {
      graphCollapsed: vi.fn(async () => ({ id: "c:older", label: "更早 2 场", months: [] })),
    });
    render(<Harness apiClient={apiClient} initial="m:old2" />);
    await waitFor(() => expect(screen.getByTestId("selection")).toHaveTextContent("c:older"));
    expect(await screen.findByRole("complementary", { name: "详情面板" })).toBeInTheDocument();
  });

  it("折叠组读失败后换到另一个组，错误不跟过去，列出这一组的会", async () => {
    const graph = payload({
      collapsed: [
        { id: "c:older", kind: "older", label: "更早 2 场", count: 2, from: "2026-05-01", to: "2026-07-20", meeting_ids: ["old1", "old2"], ring: "outer" },
        { id: "c:2026-08", kind: "month", label: "8 月 1 场", count: 1, from: "2026-08-01", to: "2026-08-20", meeting_ids: ["aug1"], ring: "outer" },
      ],
    });
    const graphCollapsed = vi
      .fn()
      .mockRejectedValueOnce(new Error("读取失败：网络断了"))
      .mockResolvedValue({
        id: "c:2026-08",
        label: "8 月 1 场",
        months: [{ month: "2026-08", label: "8 月", meetings: [{ meeting_id: "aug1", title: "八月那场会", date: "2026-08-10", open_tasks: 0 }] }],
      });
    render(<Harness apiClient={makeClient(graph, { graphCollapsed })} initial="c:older" />);
    expect(await screen.findByText("读取失败：网络断了")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /^8 月 1 场/ }));
    expect(await screen.findByText(/八月那场会/)).toBeInTheDocument();
    expect(screen.queryByText("读取失败：网络断了")).toBeNull();
  });

  it("选着折叠组时换时间窗，按新时间窗重取这一组", async () => {
    const graph = vi.fn(async (_project: string, window?: GraphWindow) =>
      fetched(
        payload({
          window: { requested: window ?? null, effective: window ?? "28d", days: window === "90d" ? 90 : 28, widened_reason: null },
          collapsed: [
            { id: "c:older", kind: "older", label: "更早 2 场", count: 2, from: "2026-05-01", to: "2026-07-20", meeting_ids: ["old1", "old2"], ring: "outer" },
          ],
        }),
      ),
    );
    const graphCollapsed = vi.fn(async (_project: string, _group: string, window?: string) => ({
      id: "c:older",
      label: "更早",
      months: [{ month: "2026-07", label: "7 月", meetings: [{ meeting_id: "old1", title: `旧会（${window}）`, date: "2026-07-01", open_tasks: 0 }] }],
    }));
    render(<Harness apiClient={makeClient(payload(), { graph, graphCollapsed })} initial="c:older" />);
    expect(await screen.findByText(/旧会（28d）/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "90 天" }));
    expect(await screen.findByText(/旧会（90d）/)).toBeInTheDocument();
    expect(graphCollapsed).toHaveBeenLastCalledWith("p", "c:older", "90d");
  });

  it("关系图读不出来时给出原因和重试", async () => {
    const graph = vi.fn().mockRejectedValueOnce(new Error("项目不存在")).mockResolvedValue(fetched(payload()));
    const apiClient = makeClient(payload(), { graph });
    render(<Harness apiClient={apiClient} />);
    expect(await screen.findByRole("alert")).toHaveTextContent("项目不存在");
    await userEvent.click(screen.getByRole("button", { name: "重试" }));
    expect(await screen.findByRole("button", { name: /^会议：初审规则沟通 a/ })).toBeInTheDocument();
  });
});

/** 按住一场会拖到 target 上松手；target 为空时在空白处松手 */
function dragMeeting(name: RegExp, target: HTMLElement | null, distance = 40) {
  const node = screen.getByRole("button", { name });
  fireEvent.mouseDown(node, { button: 0, clientX: 100, clientY: 100 });
  fireEvent.mouseMove(window, { clientX: 100 + distance, clientY: 110 });
  if (target) fireEvent.mouseEnter(target);
  fireEvent.mouseMove(window, { clientX: 100 + distance + 4, clientY: 112 });
  fireEvent.mouseUp(window, { clientX: 100 + distance + 4, clientY: 112 });
}

describe("ProjectGraph 拖放、残影、⌘Z、N", () => {
  it("把会拖到底部的项目上改归属，放下前说清楚什么跟着过去", async () => {
    const graph = payload({ meetings: [meeting("a", 0, { tasks_follow: 3, tasks_stay: 1, card: "ok" }), meeting("b", 2)] });
    const apiClient = makeClient(graph);
    render(<Harness apiClient={apiClient} />);
    const node = await screen.findByRole("button", { name: /^会议：初审规则沟通 a/ });

    fireEvent.mouseDown(node, { button: 0, clientX: 100, clientY: 100 });
    fireEvent.mouseMove(window, { clientX: 140, clientY: 110 });
    const dock = screen.getByRole("group", { name: "拖到项目上改归属" });
    expect(screen.getByText(/在空白处松手会弹回原位/)).toBeInTheDocument();
    fireEvent.mouseEnter(within(dock).getByText("数据中台"));
    expect(
      screen.getByText("放下：改到 数据中台（3 条任务、会议卡片一起过去）；1 条挂在本项目需求上的任务留下"),
    ).toBeInTheDocument();
    fireEvent.mouseUp(window);

    expect(apiClient.updateMeeting).toHaveBeenCalledWith("a", { project_id: "q" });
    expect(await screen.findByText(/^已改到 数据中台；1 条待确认\/过期的任务一起移过去/)).toBeInTheDocument();
    // 松手后补的 click 不会把这场会选中
    expect(screen.getByTestId("selection")).toHaveTextContent("");
    expect(screen.queryByRole("group", { name: "拖到项目上改归属" })).not.toBeInTheDocument();
  });

  it("拖到需求上关联，⌘Z 撤销；已经关联过的不再调接口", async () => {
    const graph = payload({
      requirements: [requirement("r1", { title: "白名单运营后台" })],
      edges: [
        { id: "e:disc:r1:b", kind: "discussion", from: "m:b", to: "r:r1", label: "你关联的", meeting_id: "b", requirement_id: "r1" },
      ],
    });
    const apiClient = makeClient(graph);
    render(<Harness apiClient={apiClient} />);
    await screen.findByRole("button", { name: /^会议：初审规则沟通 a/ });
    const target = screen.getByRole("button", { name: /^需求：白名单运营后台/ });

    dragMeeting(/^会议：初审规则沟通 a/, target);
    expect(apiClient.addRequirementMeeting).toHaveBeenCalledWith("r1", "a");
    expect(await screen.findByText("已关联到「白名单运营后台」")).toBeInTheDocument();
    await waitFor(() => expect(apiClient.graph).toHaveBeenCalledTimes(2));

    fireEvent.keyDown(document.body, { key: "z", metaKey: true });
    await waitFor(() => expect(apiClient.removeRequirementMeeting).toHaveBeenCalledWith("r1", "a"));
    expect(await screen.findByText("已撤销：这场会不再关联「白名单运营后台」")).toBeInTheDocument();

    dragMeeting(/^会议：初审规则沟通 b/, screen.getByRole("button", { name: /^需求：白名单运营后台/ }));
    expect(await screen.findByText("已经关联过「白名单运营后台」了")).toBeInTheDocument();
    expect(apiClient.addRequirementMeeting).toHaveBeenCalledTimes(1);
  });

  it("在空白处松手弹回原位，第一次说明原因；挪不到 6px 还是点击", async () => {
    const apiClient = makeClient();
    render(<Harness apiClient={apiClient} />);
    await screen.findByRole("button", { name: /^会议：初审规则沟通 a/ });

    dragMeeting(/^会议：初审规则沟通 a/, null);
    expect(apiClient.updateMeeting).not.toHaveBeenCalled();
    expect(screen.getByText(/节点不能随意摆放/)).toBeInTheDocument();
    expect(screen.getByTestId("selection")).toHaveTextContent("");
    expect(window.localStorage.getItem("meeting-workbench:graph:hint.drag")).toBe("1");

    // 上一次松手后浏览器补的 click 已经过去
    await new Promise((resolve) => window.setTimeout(resolve, 0));
    const node = screen.getByRole("button", { name: /^会议：初审规则沟通 b/ });
    fireEvent.mouseDown(node, { button: 0, clientX: 100, clientY: 100 });
    fireEvent.mouseMove(window, { clientX: 103, clientY: 102 });
    expect(screen.queryByRole("group", { name: "拖到项目上改归属" })).not.toBeInTheDocument();
    fireEvent.mouseUp(window);
    fireEvent.click(node);
    expect(screen.getByTestId("selection")).toHaveTextContent("m:b");
  });

  it("改走的会在原槽位留残影，点它撤销", async () => {
    const until = new Date(Date.now() + 5 * 60_000).toISOString();
    const graph = payload({
      meetings: [meeting("b", 2)],
      moved_out: [
        { meeting_id: "a", title: "初审规则沟通 a", date: day(0), age_days: 0, to_project_id: "q", to_project_name: "数据中台", undo_until: until },
        { meeting_id: "old", title: "很早的会", date: day(60), age_days: 60, to_project_id: "q", to_project_name: "数据中台", undo_until: until },
      ],
    });
    const apiClient = makeClient(graph);
    render(<Harness apiClient={apiClient} />);
    const ghost = await screen.findByRole("button", { name: "刚改到 数据中台 的会：初审规则沟通 a，点一下撤销" });
    // 画不下的（60 天前）才放在顶上一行
    const moved = screen.getByRole("list", { name: "刚移走的会" });
    expect(within(moved).getAllByRole("listitem")).toHaveLength(1);
    expect(moved).toHaveTextContent("很早的会");

    await userEvent.click(ghost);
    expect(apiClient.undoMeetingProject).toHaveBeenCalledWith("a");
  });

  it("N 在要你处理的节点之间跳，没有时说一声", async () => {
    const graph = payload({
      meetings: [meeting("a", 0), meeting("b", 2, { pending_tasks: 1, open_tasks: 1 }), meeting("c", 10, { state: "needs_review" })],
    });
    render(<Harness apiClient={makeClient(graph)} />);
    const center = await screen.findByRole("button", { name: "云图AI，3 场会" });
    center.focus();
    fireEvent.keyDown(center, { key: "n" });
    const first = screen.getByTestId("selection").textContent;
    expect(["m:b", "m:c"]).toContain(first);
    fireEvent.keyDown(document.activeElement ?? center, { key: "n" });
    const second = screen.getByTestId("selection").textContent;
    expect(["m:b", "m:c"]).toContain(second);
    expect(second).not.toBe(first);
    // 打中文时不跳
    fireEvent.keyDown(center, { key: "n", isComposing: true });
    expect(screen.getByTestId("selection")).toHaveTextContent(second ?? "");
  });

  it("图上没有要处理的节点时，N 说一声", async () => {
    render(<Harness apiClient={makeClient()} />);
    const center = await screen.findByRole("button", { name: "云图AI，3 场会" });
    fireEvent.keyDown(center, { key: "N" });
    expect(await screen.findByText("这张图上没有要你处理的了")).toBeInTheDocument();
  });
});

describe("ProjectGraph 撤销失败和在途", () => {
  const UNDONE = { effects: { tasks_restored: 1 } };

  /** 门口的会［归这里］之后提示条上有［撤销］；返回提示条 */
  async function answerDoorstepAndGetNotice() {
    const doorstep = await screen.findByRole("group", { name: /可能是这个项目的会：门口的会/ });
    await userEvent.click(within(doorstep).getByRole("button", { name: "归这里" }));
    return screen.findByRole("status");
  }
  const undoKey = () => fireEvent.keyDown(document.body, { key: "z", metaKey: true });

  it.each([
    ["网络断了", () => new TypeError("Failed to fetch"), "Failed to fetch"],
    ["写请求超时", () => new ApiTimeoutError("服务没有响应，可能仍在处理，稍后刷新确认"), "服务没有响应，可能仍在处理，稍后刷新确认"],
    ["服务端 500", () => new ApiError("服务出错了，稍后再试", 500, { detail: "服务出错了，稍后再试" }), "服务出错了，稍后再试"],
    ["429 让稍后再来", () => new ApiError("请求太频繁，稍后再试", 429, { detail: "请求太频繁，稍后再试" }), "请求太频繁，稍后再试"],
  ])("撤销请求失败（%s）：提示写原因，这一步还留在栈里，⌘Z 再撤一次就成", async (_name, makeError, message) => {
    const undoMeetingProject = vi.fn().mockRejectedValueOnce(makeError()).mockResolvedValue(UNDONE);
    const apiClient = makeClient(withDoorstep(), { undoMeetingProject });
    render(<Harness apiClient={apiClient} />);
    const notice = await answerDoorstepAndGetNotice();

    await userEvent.click(within(notice).getByRole("button", { name: "撤销" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(message);
    expect(undoMeetingProject).toHaveBeenCalledTimes(1);

    undoKey();
    expect(await screen.findByText("已撤销刚才的改动")).toBeInTheDocument();
    expect(undoMeetingProject).toHaveBeenCalledTimes(2);
    expect(undoMeetingProject).toHaveBeenLastCalledWith("door0");

    // 撤成了就出栈：再按 ⌘Z 没有这一步了
    undoKey();
    expect(await screen.findByText("没有能撤销的操作了（只保留 10 分钟内的）")).toBeInTheDocument();
    expect(undoMeetingProject).toHaveBeenCalledTimes(2);
  });

  it("服务端明确回绝（过了撤销期、已经撤销过）：原样显示那一句，这一步出栈，不再一直挡着", async () => {
    const undoMeetingProject = vi.fn().mockRejectedValue(new ApiError("撤销期已过，改不回去了", 409, { detail: "撤销期已过，改不回去了" }));
    const apiClient = makeClient(withDoorstep(), { undoMeetingProject });
    render(<Harness apiClient={apiClient} />);
    const notice = await answerDoorstepAndGetNotice();

    await userEvent.click(within(notice).getByRole("button", { name: "撤销" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("撤销期已过，改不回去了");

    undoKey();
    expect(await screen.findByText("没有能撤销的操作了（只保留 10 分钟内的）")).toBeInTheDocument();
    expect(undoMeetingProject).toHaveBeenCalledTimes(1);
  });

  it("撤销在途时连按 ⌘Z、再点［撤销］：同一步只发一次", async () => {
    let finish: (value: unknown) => void = () => undefined;
    const undoMeetingProject = vi.fn(() => new Promise((resolve) => (finish = resolve)));
    const apiClient = makeClient(withDoorstep(), { undoMeetingProject });
    render(<Harness apiClient={apiClient} />);
    const notice = await answerDoorstepAndGetNotice();

    await userEvent.click(within(notice).getByRole("button", { name: "撤销" }));
    await waitFor(() => expect(undoMeetingProject).toHaveBeenCalledTimes(1));
    undoKey();
    undoKey();
    // 在途时提示条上的［撤销］还在，但按不了
    expect(within(screen.getByRole("status")).getByRole("button", { name: "撤销" })).toBeDisabled();
    await userEvent.click(within(screen.getByRole("status")).getByRole("button", { name: "撤销" }));
    expect(undoMeetingProject).toHaveBeenCalledTimes(1);

    await act(async () => finish(UNDONE));
    expect(await screen.findByText("已撤销刚才的改动")).toBeInTheDocument();
    expect(undoMeetingProject).toHaveBeenCalledTimes(1);
  });

  it("同一帧里连按两次 ⌘Z（还没重画，读到的 busy 还是旧的）：也只发一次", async () => {
    let finish: (value: unknown) => void = () => undefined;
    const undoMeetingProject = vi.fn(() => new Promise((resolve) => (finish = resolve)));
    const apiClient = makeClient(withDoorstep(), { undoMeetingProject });
    render(<Harness apiClient={apiClient} />);
    await answerDoorstepAndGetNotice();

    act(() => {
      undoKey();
      undoKey();
    });
    expect(undoMeetingProject).toHaveBeenCalledTimes(1);

    await act(async () => finish(UNDONE));
    expect(await screen.findByText("已撤销刚才的改动")).toBeInTheDocument();
    expect(undoMeetingProject).toHaveBeenCalledTimes(1);
  });

  it("失败那次以后，别的操作的撤销照样排在它后面：先撤最新的，失败的那步还在更早的位置", async () => {
    // 先归这场会（第一步），再有第二步（拖到需求上关联）；第二步的撤销断网，第一步不受影响
    const undoUntil = new Date(Date.now() + 10 * 60_000).toISOString();
    const graph = withDoorstep();
    const removeRequirementMeeting = vi.fn().mockRejectedValueOnce(new TypeError("Failed to fetch")).mockResolvedValue({});
    const apiClient = makeClient(graph, {
      removeRequirementMeeting,
      updateMeeting: vi.fn(async () => ({ effects: { tasks_moved: 0, tasks_left: [], undo_until: undoUntil } })),
    });
    render(<Harness apiClient={apiClient} />);
    await answerDoorstepAndGetNotice();
    // 第二步：把会 a 拖到需求 r1 上关联
    dragMeeting(/^会议：初审规则沟通 a/, screen.getByRole("button", { name: /^需求：需求 r1/ }));
    expect(await screen.findByText("已关联到「需求 r1」")).toBeInTheDocument();

    undoKey();
    expect(await screen.findByRole("alert")).toHaveTextContent("Failed to fetch");
    undoKey();
    expect(await screen.findByText("已撤销：这场会不再关联「需求 r1」")).toBeInTheDocument();
    expect(removeRequirementMeeting).toHaveBeenCalledTimes(2);
    // 第二步撤完才轮到第一步
    undoKey();
    expect(await screen.findByText("已撤销刚才的改动")).toBeInTheDocument();
    expect(apiClient.undoMeetingProject).toHaveBeenCalledTimes(1);
  });
});

function focusClient(overrides: Record<string, unknown> = {}) {
  return makeClient(payload(), {
    graphMeetingFocus: vi.fn(async (meetingId: string) =>
      focusPayload({
        meeting: { ...focusPayload().meeting, id: meetingId, title: `初审规则沟通 ${meetingId}` },
        previous: meetingId === "a" ? { meeting_id: "b", title: "初审规则沟通 b", date: day(2) } : null,
        next: meetingId === "b" ? { meeting_id: "a", title: "初审规则沟通 a", date: day(0) } : null,
      }),
    ),
    meetingQuotes: vi.fn(async (meetingId: string, at: number[]) => ({
      meeting_id: meetingId,
      quotes: at.map((ms) => ({
        at: ms,
        segments: [
          { segment_id: "s1", start_ms: ms - 15_000, end_ms: ms - 1_000, text: "先把旧口径停掉", speaker: null },
          { segment_id: "s2", start_ms: ms - 1_000, end_ms: ms + 8_000, text: "初审规则就按新口径", speaker: "王工" },
        ],
      })),
    })),
    confirmTask: vi.fn(async () => ({})),
    rejectTask: vi.fn(async () => ({})),
    setTaskStatus: vi.fn(async () => ({})),
    updateTask: vi.fn(async () => ({})),
    ...overrides,
  });
}

async function expandMeetingA(apiClient: ApiClient) {
  render(<Harness apiClient={apiClient} />);
  fireEvent.doubleClick(await screen.findByRole("button", { name: /^会议：初审规则沟通 a/ }));
  return screen.findByRole("application", { name: "展开的会：初审规则沟通 a" });
}

describe("ProjectGraph 展开一场会", () => {
  it("双击会议展开：决议在录音条上方、任务在下方，前后场贴边，Esc 回到关系图", async () => {
    const apiClient = focusClient();
    const view = await expandMeetingA(apiClient);
    expect(apiClient.graphMeetingFocus).toHaveBeenCalledWith("a");
    expect(await within(view).findByRole("button", { name: "决议：初审规则按新口径执行，01:00" })).toBeInTheDocument();
    expect(within(view).getByRole("button", { name: "任务（待确认）：任务 t1，05:00" })).toBeInTheDocument();
    expect(within(view).getByRole("button", { name: "决议：没写时间的决议，没有时间点" })).toBeInTheDocument();
    expect(within(view).getAllByText("没有时间点", { selector: ".meeting-focus__untimed" })).toHaveLength(2);
    expect(within(view).getByRole("button", { name: "上一场：初审规则沟通 b" })).toBeInTheDocument();
    expect(within(view).getByText("这是这个项目最近的会")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "初审规则沟通 a" })).toBeInTheDocument();

    fireEvent.keyDown(view, { key: "ArrowLeft" });
    expect(await screen.findByRole("application", { name: "展开的会：初审规则沟通 b" })).toBeInTheDocument();
    expect(apiClient.graphMeetingFocus).toHaveBeenLastCalledWith("b");

    fireEvent.keyDown(screen.getByRole("application", { name: "展开的会：初审规则沟通 b" }), { key: "Escape" });
    expect(await screen.findByRole("button", { name: /^会议：初审规则沟通 a/ })).toBeInTheDocument();
    expect(screen.queryByRole("application", { name: /^展开的会/ })).not.toBeInTheDocument();
  });

  it("深链展开、关系图还没到时，「今天」按本地日期算", async () => {
    // 北京 9 月 26 日早上 7 点，UTC 还是 25 日
    vi.useFakeTimers({ now: new Date("2026-09-26T07:00:00+08:00"), toFake: ["Date"] });
    const apiClient = focusClient({ graph: vi.fn(() => new Promise(() => undefined)) });
    render(
      <ProjectGraph
        apiClient={apiClient}
        expanded="a"
        onBack={() => {}}
        onExpandChange={() => {}}
        onOpenGlossary={() => {}}
        onOpenMeeting={() => {}}
        onOpenProject={() => {}}
        onOpenRequirement={() => {}}
        onSelectionChange={() => {}}
        projectId="p"
        projects={PROJECTS}
        selection={null}
      />,
    );
    await screen.findByRole("application", { name: "展开的会：初审规则沟通 a" });
    expect(await screen.findByText(/^今天 · 10:00 · 定了/)).toBeInTheDocument();
  });

  it("会议面板里［展开这场会］也能展开；点决议看全文和前后 20 秒的原话", async () => {
    const apiClient = focusClient();
    render(<Harness apiClient={apiClient} initial="m:a" />);
    const panel = await screen.findByRole("complementary", { name: "详情面板" });
    await userEvent.click(within(panel).getByRole("button", { name: "展开这场会" }));
    const view = await screen.findByRole("application", { name: "展开的会：初审规则沟通 a" });

    await userEvent.click(await within(view).findByRole("button", { name: "决议：初审规则按新口径执行，01:00" }));
    const detail = await screen.findByRole("complementary", { name: "详情面板" });
    expect(within(detail).getByText("初审规则按新口径执行，旧口径下月停用")).toBeInTheDocument();
    expect(within(detail).getByText("会上 01:00 说的")).toBeInTheDocument();
    expect(apiClient.meetingQuotes).toHaveBeenCalledWith("a", [60_000], "wide");
    const anchor = (await within(detail).findByText("初审规则就按新口径")).closest("li");
    expect(anchor).toHaveClass("is-anchor");
    expect(within(detail).getByText("王工：")).toBeInTheDocument();
  });

  it("展开的会里点决议开了面板：焦点落在页面上时 Esc 关面板，展开的会还在", async () => {
    render(<Harness apiClient={focusClient()} initial="m:a" />);
    const panel = await screen.findByRole("complementary", { name: "详情面板" });
    await userEvent.click(within(panel).getByRole("button", { name: "展开这场会" }));
    const view = await screen.findByRole("application", { name: "展开的会：初审规则沟通 a" });
    await userEvent.click(await within(view).findByRole("button", { name: "决议：初审规则按新口径执行，01:00" }));
    const detail = await screen.findByRole("complementary", { name: "详情面板" });
    await userEvent.click(within(detail).getByText("会上 01:00 说的"));
    expect(document.activeElement).toBe(document.body);

    await userEvent.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("complementary", { name: "详情面板" })).toBeNull());
    expect(screen.getByRole("application", { name: "展开的会：初审规则沟通 a" })).toBeInTheDocument();
  });

  it("展开的会里点决议开了面板，焦点在展开的会上：一次 Esc 只关面板，再按一次才回到关系图", async () => {
    render(<Harness apiClient={focusClient()} initial="m:a" />);
    const panel = await screen.findByRole("complementary", { name: "详情面板" });
    await userEvent.click(within(panel).getByRole("button", { name: "展开这场会" }));
    const view = await screen.findByRole("application", { name: "展开的会：初审规则沟通 a" });
    const decision = await within(view).findByRole("button", { name: "决议：初审规则按新口径执行，01:00" });
    await userEvent.click(decision);
    await screen.findByRole("complementary", { name: "详情面板" });

    fireEvent.keyDown(decision, { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("complementary", { name: "详情面板" })).toBeNull());
    expect(screen.getByRole("application", { name: "展开的会：初审规则沟通 a" })).toBeInTheDocument();

    fireEvent.keyDown(screen.getByRole("application", { name: "展开的会：初审规则沟通 a" }), { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("application", { name: /^展开的会/ })).toBeNull());
    expect(await screen.findByRole("button", { name: /^会议：初审规则沟通 a/ })).toBeInTheDocument();
  });

  it("决议带台账 id（4a）时按 id 选中：面板里还是那一条", async () => {
    const withIds = focusPayload({
      decisions: [
        { id: "dec-00aa11bb22cc33dd", text: "没写时间的决议", start_ms: null },
        { id: "dec-3f2a9c0b1d4e5f60", text: "初审规则按新口径执行", start_ms: 60_000, detail: "初审规则按新口径执行，旧口径下月停用" },
      ],
    });
    const apiClient = focusClient({ graphMeetingFocus: vi.fn(async () => withIds) });
    const view = await expandMeetingA(apiClient);
    await userEvent.click(await within(view).findByRole("button", { name: "决议：初审规则按新口径执行，01:00" }));
    const detail = await screen.findByRole("complementary", { name: "详情面板" });
    expect(within(detail).getByText("初审规则按新口径执行，旧口径下月停用")).toBeInTheDocument();
  });

  it("4c：决议下面画标记行，［不是一回事］进画布的撤销栈", async () => {
    const later = {
      relation_id: 17, decision_id: "dec-b", meeting: { id: "b", title: "周会", date: "2026-09-28" },
      text: "初审规则改回旧口径", start_ms: 30_000, quote: "改回旧口径", audio_url: "/api/media/9",
    };
    const withMarks = focusPayload({
      decisions: [
        { id: "dec-3f2a9c0b1d4e5f60", text: "初审规则按新口径执行", start_ms: 60_000, detail: "初审规则按新口径执行",
          later: [later], earlier: [] },
      ],
    });
    const apiClient = focusClient({
      graphMeetingFocus: vi.fn(async () => withMarks),
      answerRelation: vi.fn(async () => ({ relation: {}, undo_until: new Date(Date.now() + 600_000).toISOString() })),
      undoRelation: vi.fn(async () => ({ relation: {}, removed_deliverable_id: null })),
    });
    render(
      <LinksFlagsContext.Provider value={{ linksEnabled: true, semanticEnabled: true, llmConfigured: true }}>
        <Harness apiClient={apiClient} />
      </LinksFlagsContext.Provider>,
    );
    fireEvent.doubleClick(await screen.findByRole("button", { name: /^会议：初审规则沟通 a/ }));
    const view = await screen.findByRole("application", { name: "展开的会：初审规则沟通 a" });
    await userEvent.click(await within(view).findByRole("button", { name: "决议：初审规则按新口径执行，01:00" }));
    const detail = await screen.findByRole("complementary", { name: "详情面板" });
    expect(within(detail).getByText("后来改了：9月28日 周会『初审规则改回旧口径』")).toBeInTheDocument();
    await userEvent.click(within(detail).getByRole("button", { name: "从 00:30 听这条" }));

    await userEvent.click(within(detail).getByRole("button", { name: "不是一回事" }));
    expect(apiClient.answerRelation).toHaveBeenCalledWith(17, { answer: "no" });
    expect(await screen.findByText("已去掉这条『后来改了』")).toBeInTheDocument();
    fireEvent.keyDown(document.body, { key: "z", metaKey: true });
    await waitFor(() => expect(apiClient.undoRelation).toHaveBeenCalledWith(17));
  });

  it("4c：简报「定了什么」有后来改了时接「· 9月28日后来改了」", async () => {
    const apiClient = makeClient(payload(), {
      meetingBrief: vi.fn(async (meetingId: string) => ({
        ...brief(meetingId),
        decisions: [
          { id: "dec-1", text: "初审规则按新口径执行", start_ms: 60_000,
            later: { date: "2026-09-28", meeting_title: "周会", text: "改回旧口径" } },
          { id: "dec-2", text: "没有后来的决议", start_ms: null, later: null },
        ],
      })),
    });
    render(<Harness apiClient={apiClient} initial="m:a" />);
    const panel = await screen.findByRole("complementary", { name: "详情面板" });
    expect(await within(panel).findByText("· 9月28日后来改了")).toBeInTheDocument();
    expect(within(panel).getAllByText(/后来改了/)).toHaveLength(1);
  });

  it("任务：确认、编辑后 ⌘Z 改回原样、不要；「+N」列出全部", async () => {
    const tasks = [
      focusTask("t1", 300_000, { status: "pending_confirm" }),
      ...Array.from({ length: 6 }, (_, index) =>
        focusTask(`o${index}`, 10_000 * (index + 1), { status: index === 5 ? "done" : "confirmed" }),
      ),
    ];
    const apiClient = focusClient({
      graphMeetingFocus: vi.fn(async () => focusPayload({ tasks })),
    });
    const view = await expandMeetingA(apiClient);

    await userEvent.click(await within(view).findByRole("button", { name: "任务（待确认）：任务 t1，05:00" }));
    let detail = await screen.findByRole("complementary", { name: "详情面板" });
    await userEvent.click(within(detail).getByRole("button", { name: "确认" }));
    expect(apiClient.confirmTask).toHaveBeenCalledWith("t1");
    expect(await screen.findByText("已确认「任务 t1」")).toBeInTheDocument();
    await waitFor(() => expect(apiClient.graphMeetingFocus).toHaveBeenCalledTimes(2));

    await userEvent.click(within(detail).getByRole("button", { name: "编辑…" }));
    const input = within(detail).getByRole("textbox", { name: "任务标题" });
    await userEvent.clear(input);
    await userEvent.type(input, "改过的标题");
    await userEvent.click(within(detail).getByRole("button", { name: "保存" }));
    expect(apiClient.updateTask).toHaveBeenCalledWith("t1", { title: "改过的标题", detail: "" });
    expect(await screen.findByText("已改好「改过的标题」")).toBeInTheDocument();
    fireEvent.keyDown(document.body, { key: "z", metaKey: true });
    await waitFor(() => expect(apiClient.updateTask).toHaveBeenLastCalledWith("t1", { title: "任务 t1", detail: "" }));
    expect(await screen.findByText("已撤销：任务改回「任务 t1」")).toBeInTheDocument();

    detail = screen.getByRole("complementary", { name: "详情面板" });
    await userEvent.click(within(detail).getByRole("button", { name: "不要" }));
    expect(apiClient.rejectTask).toHaveBeenCalledWith("t1");
    await waitFor(() => expect(screen.queryByRole("complementary", { name: "详情面板" })).not.toBeInTheDocument());

    await userEvent.click(within(view).getByRole("button", { name: "+1 条任务" }));
    detail = await screen.findByRole("complementary", { name: "详情面板" });
    expect(within(detail).getByRole("heading", { name: "全部任务（7 条）" })).toBeInTheDocument();
    await userEvent.click(within(detail).getByRole("button", { name: "任务 o2" }));
    expect(within(screen.getByRole("complementary", { name: "详情面板" })).getByRole("button", { name: "完成" })).toBeInTheDocument();
  });

  it("读不出来时说原因，可以重试或回去", async () => {
    const graphMeetingFocus = vi.fn().mockRejectedValueOnce(new Error("会议不存在")).mockResolvedValue(focusPayload());
    const apiClient = focusClient({ graphMeetingFocus });
    render(<Harness apiClient={apiClient} />);
    fireEvent.doubleClick(await screen.findByRole("button", { name: /^会议：初审规则沟通 a/ }));
    expect(await screen.findByRole("alert")).toHaveTextContent("会议不存在");
    expect(screen.getByRole("application", { name: "展开的会" })).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "重试" }));
    expect(await screen.findByRole("button", { name: "决议：初审规则按新口径执行，01:00" })).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "← 回到关系图" }));
    expect(await screen.findByRole("button", { name: /^会议：初审规则沟通 a/ })).toBeInTheDocument();
  });
});

const ONLINE_ROOTS: GraphRootsPayload = {
  roots: [
    {
      id: "root:1",
      root_id: 1,
      path: "/材料/云图AI",
      state: "online",
      loose_count: 4,
      checked_at: null,
      recent_dirs: [{ name: "初审规则", dir: "初审规则", path: "/材料/云图AI/初审规则", mtime: "2026-09-25T10:00:00" }],
    },
  ],
  folders: [],
  loose: { count: 4, recent: [{ name: "报价单.xlsx", path: "/材料/云图AI/报价单.xlsx", size: 2048, mtime: "2026-09-24T09:00:00" }] },
  checking: false,
  can_reveal: true,
};

describe("ProjectGraph 完整面板和在图上找", () => {
  it("项目面板：会、需求、任务、卡片的数，材料文件夹的盘状态，词典入口", async () => {
    const onOpenGlossary = vi.fn();
    const graph = payload({ meetings: [meeting("a", 0, { open_tasks: 3, pending_tasks: 1 }), meeting("b", 2, { open_tasks: 1 })] });
    const apiClient = makeClient(graph, { graphRoots: vi.fn(async () => ({ ...ONLINE_ROOTS, roots: [{ ...ONLINE_ROOTS.roots[0], state: "volume_offline" }] })) });
    render(<Harness apiClient={apiClient} handlers={{ onOpenGlossary }} initial="project" />);
    const panel = await screen.findByRole("complementary", { name: "详情面板" });
    expect(within(panel).getByText("4 条")).toBeInTheDocument();
    expect(within(panel).getByText("1 条待确认")).toBeInTheDocument();
    expect(within(panel).getByText("已写 3 张")).toBeInTheDocument();
    expect(await within(panel).findByText(/资料盘未连接/)).toBeInTheDocument();
    await userEvent.click(within(panel).getByRole("button", { name: "看这个项目的词典 →" }));
    expect(onOpenGlossary).toHaveBeenCalledWith("p");
  });

  it("文件夹面板逐层看，本机时能在访达中显示；根目录外侧挂最近改过的子文件夹", async () => {
    const apiClient = makeClient(payload(), {
      graphRoots: vi.fn(async () => ONLINE_ROOTS),
      revealMaterial: vi.fn(async () => ({ ok: true, path: "" })),
    });
    render(<Harness apiClient={apiClient} />);
    const sub = await screen.findByRole("button", { name: "最近改过的子文件夹：初审规则" });
    await userEvent.click(screen.getByRole("button", { name: "文件夹：云图AI" }));
    let panel = await screen.findByRole("complementary", { name: "详情面板" });
    expect(apiClient.graphExpand).toHaveBeenCalledWith(1, "");
    expect(await within(panel).findByText("报价单.xlsx")).toBeInTheDocument();
    await userEvent.click(within(panel).getByRole("button", { name: "初审规则/" }));
    expect(await within(panel).findByText("口径说明.docx")).toBeInTheDocument();
    expect(apiClient.graphExpand).toHaveBeenLastCalledWith(1, "初审规则");
    await userEvent.click(within(panel).getByRole("button", { name: "在访达中显示" }));
    expect(apiClient.revealMaterial).toHaveBeenCalledWith("/材料/云图AI/初审规则");
    await userEvent.click(within(within(panel).getByRole("navigation", { name: "文件夹位置" })).getByRole("button", { name: "云图AI" }));
    expect(await within(panel).findByText("报价单.xlsx")).toBeInTheDocument();

    await userEvent.click(sub);
    panel = screen.getByRole("complementary", { name: "详情面板" });
    expect(await within(panel).findByText("口径说明.docx")).toBeInTheDocument();
    // 子文件夹节点 id 用相对路径的短 hash，中文路径不进地址栏
    expect(screen.getByTestId("selection").textContent).toMatch(/^sub:1:[0-9a-z]+$/);
    expect(screen.getByTestId("selection")).toHaveTextContent(`sub:1:${shortHash("初审规则")}`);
  });

  it("信标面板：解除和也移过去都能 ⌘Z 撤销，任务连需求一起搬回来；去看跳到对应的页", async () => {
    const onOpenRequirement = vi.fn();
    const graph = payload({
      beacons: [
        {
          id: "b:q",
          project_id: "q",
          project_name: "数据中台",
          project_color: "#7a5af8",
          count: 2,
          label: "→ 数据中台 ×2",
          items: [
            {
              kind: "meeting_requirement",
              text: "会议「初审规则沟通 a」关联了数据中台的需求「指标口径」",
              meeting_id: "a",
              requirement_id: "rq",
              requirement_project_id: "q",
              requirement_title: "指标口径",
            },
            {
              kind: "task_from_elsewhere",
              text: "任务「导出报表」来自数据中台的会「周会」",
              meeting_id: "qm",
              task_id: "tq",
              task_title: "导出报表",
              task_project_id: "p",
              meeting_project_id: "q",
              requirement_id: "r1",
              requirement_title: "需求 r1",
              requirement_project_id: "p",
            },
          ],
        },
      ],
    });
    const apiClient = makeClient(graph, { updateTask: vi.fn(async () => ({})) });
    render(<Harness apiClient={apiClient} handlers={{ onOpenRequirement }} />);
    await userEvent.click(await screen.findByRole("button", { name: "→ 数据中台 ×2" }));
    const panel = await screen.findByRole("complementary", { name: "详情面板" });
    expect(within(panel).getByText("移到 数据中台（会移出需求『需求 r1』）")).toBeInTheDocument();

    await userEvent.click(within(panel).getByRole("button", { name: "解除" }));
    expect(apiClient.removeRequirementMeeting).toHaveBeenCalledWith("rq", "a");
    expect(await screen.findByText("已解除这条跨项目的关联")).toBeInTheDocument();
    fireEvent.keyDown(document.body, { key: "z", metaKey: true });
    await waitFor(() => expect(apiClient.addRequirementMeeting).toHaveBeenCalledWith("rq", "a"));
    expect(await screen.findByText("已撤销：重新关联了「指标口径」")).toBeInTheDocument();

    await userEvent.click(within(screen.getByRole("complementary", { name: "详情面板" })).getByRole("button", { name: "也移过去" }));
    expect(apiClient.updateTask).toHaveBeenCalledWith("tq", { project_id: "q", requirement_id: null });
    expect(await screen.findByText("已把任务「导出报表」移到 数据中台")).toBeInTheDocument();
    fireEvent.keyDown(document.body, { key: "z", metaKey: true });
    await waitFor(() => expect(apiClient.updateTask).toHaveBeenLastCalledWith("tq", { project_id: "p", requirement_id: "r1" }));
    expect(await screen.findByText("已撤销：任务「导出报表」搬回去了")).toBeInTheDocument();

    await userEvent.click(within(screen.getByRole("complementary", { name: "详情面板" })).getAllByRole("button", { name: "去看" })[0]);
    expect(onOpenRequirement).toHaveBeenCalledWith("rq");
  });

  it("线索词面板给出全部逐字稿里的次数", async () => {
    const graphFulltext = vi.fn(async () => ({
      variants: ["初审规则", "初审"],
      total: 9,
      meeting_count: 2,
      meetings: [
        { meeting_id: "a", title: "初审规则沟通 a", date: day(0), count: 6, first_ms: 61_000 },
        { meeting_id: "old", title: "很早的会", date: day(80), count: 3, first_ms: 5_000 },
      ],
    }));
    const apiClient = makeClient(payload(), { graphFulltext, glossaryTermDetail: vi.fn(async () => { throw new Error("x"); }) });
    render(<Harness apiClient={apiClient} initial="cue:1" />);
    const panel = await screen.findByRole("complementary", { name: "详情面板" });
    expect(await within(panel).findByText(/全部逐字稿里说了 9 次，在 2 场会上/)).toBeInTheDocument();
    expect(within(panel).getByText("（连「初审」一起数）")).toBeInTheDocument();
    expect(graphFulltext).toHaveBeenCalledWith("p", { term: "t1" });
  });

  it("在图上找：标题和逐字稿命中的会一起点亮，回车逐个跳，Esc 清掉", async () => {
    const graphFulltext = vi.fn(async () => ({
      variants: ["口径"],
      total: 2,
      meeting_count: 1,
      meetings: [{ meeting_id: "c", title: "初审规则沟通 c", date: day(10), count: 2, first_ms: 1_000 }],
    }));
    const graph = payload({
      meetings: [meeting("a", 0, { title: "口径对齐" }), meeting("b", 2), meeting("c", 10)],
      requirements: [requirement("r1", { title: "口径统一" })],
    });
    render(<Harness apiClient={makeClient(graph, { graphFulltext })} />);
    const input = await screen.findByRole("searchbox", { name: "在图上找" });
    await userEvent.type(input, "口径");
    await waitFor(() => expect(graphFulltext).toHaveBeenCalledWith("p", { q: "口径" }));
    expect(await screen.findByText("3 个，回车逐个跳；逐字稿里 1 场会说到")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^会议：初审规则沟通 b/ })).toHaveClass("is-dim");
    expect(screen.getByRole("button", { name: /^会议：初审规则沟通 c/ })).not.toHaveClass("is-dim");

    await userEvent.keyboard("{Enter}");
    expect(screen.getByTestId("selection")).toHaveTextContent("r:r1");
    await userEvent.keyboard("{Enter}");
    expect(screen.getByTestId("selection")).toHaveTextContent("m:a");
    expect(screen.getByText("2/3 个，回车逐个跳；逐字稿里 1 场会说到")).toBeInTheDocument();

    await userEvent.keyboard("{Escape}");
    expect(input).toHaveValue("");
    expect(screen.getByRole("button", { name: /^会议：初审规则沟通 b/ })).not.toHaveClass("is-dim");
  });
});

describe("ProjectGraph 会议面板里的「像是新需求」提示", () => {
  it("和会议页同一个提示；建成需求后画布的提示条带［打开需求］［撤销］", async () => {
    const hinted = (meetingId: string): MeetingBrief => {
      const value = brief(meetingId);
      return {
        ...value,
        attribution: {
          ...value.attribution,
          name_hint: { kind: "requirement", name: "数据看板", spoken: [], project_id: "p", project_name: "云图AI" },
        },
      };
    };
    const undoUntil = new Date(Date.now() + 10 * 60_000).toISOString();
    const apiClient = makeClient(payload(), {
      meetingBrief: vi.fn(async (meetingId: string) => hinted(meetingId)),
      nameCandidates: vi.fn(async () => ({
        hint: { kind: "requirement", name: "数据看板", spoken: [], project_id: "p", project_name: "云图AI" },
        candidates: [{ name: "数据看板", folder_path: null, spoken: null, ai: true, similar_folder_path: null }],
        meetings: [{ id: "a", title: "初审规则沟通 a", date: "2026-09-26", said_ms: null }],
        default_action: "create_requirement",
        project: { id: "p", name: "云图AI" },
        requirement_projects: [],
        folder: { mode: "none", reason: null },
        folders_state: "ready",
        create_parent: null,
        create_parent_source: null,
        create_parent_state: null,
      })),
      nameAsRequirement: vi.fn(async () => ({
        requirement_id: "r-new",
        requirement_title: "数据看板",
        project_id: "p",
        project_name: "云图AI",
        existing: false,
        priority: "P2",
        meetings_linked: 1,
        meetings_assigned: 0,
        meeting_ids: ["a"],
        folder_attached: null,
        folder_error: null,
        event_id: 9,
        undo_until: undoUntil,
      })),
      meeting: vi.fn(async () => ({
        id: "a",
        project_id: "p",
        project_name: "云图AI",
        project_color: "#2c8d83",
        project_origin: "ai",
        attribution: { ...brief("a").attribution, name_hint: null },
      })),
    });
    const onOpenRequirement = vi.fn();
    render(<Harness apiClient={apiClient} handlers={{ onOpenRequirement }} />);
    await userEvent.click(await screen.findByRole("button", { name: /^会议：初审规则沟通 a/ }));
    const panel = await screen.findByRole("complementary", { name: "详情面板" });

    expect(await within(panel).findByText("像是『云图AI』里的一个新需求")).toBeInTheDocument();
    expect(await within(panel).findByDisplayValue("数据看板")).toBeInTheDocument();
    await userEvent.click(within(panel).getByRole("button", { name: "建成需求" }));

    expect(apiClient.nameAsRequirement).toHaveBeenCalledWith("a", { title: "数据看板" });
    const notice = await screen.findByRole("status");
    expect(notice).toHaveTextContent("已在『云图AI』建好需求『数据看板』（P2），1 场会已关联");
    expect(within(notice).getAllByRole("button", { name: "撤销" })).toHaveLength(1);
    await userEvent.click(within(notice).getByRole("button", { name: "打开需求" }));
    expect(onOpenRequirement).toHaveBeenCalledWith("r-new");
  });
});

// ------------------------------------------------------------------ 第二期：会上提到的文件、像是新需求、等补建的文件夹

function briefFile(fileId: number, name: string, overrides: Partial<BriefFile> = {}): BriefFile {
  const stem = name.replace(/(v\d+)?\.\w+$/, "");
  return {
    file_id: fileId,
    name,
    rel_path: `商务/${name}`,
    root_id: 1,
    stem_key: stem,
    needle: stem,
    count: 1,
    minutes_count: 0,
    first_ms: 60_000,
    source: "transcript",
    picked: false,
    generic: false,
    ...overrides,
  };
}

const QUOTE_FILE = briefFile(7, "报价单v2.xlsx", { count: 3, first_ms: 754_000 });
const ROSTER_FILE = briefFile(8, "排班表.xlsx");

function withFiles(overrides: Partial<GraphPayload> = {}): GraphPayload {
  return payload({
    files: [
      { id: "file:7", kind: "file", file_id: 7, name: "报价单v2.xlsx", ext: "xlsx", rel_path: "商务/报价单v2.xlsx", root_id: 1, folder: "root:1", meeting_count: 2 },
    ],
    edges: [
      {
        id: "e:file:7:a",
        kind: "mentioned",
        from: "m:a",
        to: "file:7",
        label: "会上说『报价单』3 次 · 00:12:34",
        count: 3,
        source: "transcript",
        needle: "报价单",
        stem_key: "报价单",
        meeting_id: "a",
        anchors_ms: [754_000],
      },
    ],
    ...overrides,
  });
}

function fileDetail(overrides: Partial<GraphFileDetail> = {}): GraphFileDetail {
  const row = (meetingId: string, date: string, count: number, firstMs: number, quote: string) => ({
    meeting_id: meetingId,
    title: `初审规则沟通 ${meetingId}`,
    date,
    stem_key: "报价单",
    needle: "报价单",
    count,
    minutes_count: 0,
    first_ms: firstMs,
    anchors_ms: [firstMs],
    source: "transcript" as const,
    status: "active" as const,
    picked: false,
    quote,
  });
  return {
    file: {
      id: 7,
      name: "报价单v2.xlsx",
      ext: "xlsx",
      stem: "报价单v2",
      stem_key: "报价单",
      rel_path: "商务/报价单v2.xlsx",
      root_id: 1,
      root_path: "/材料/云图AI",
      folder_path: "/材料/云图AI/商务",
      path: "/材料/云图AI/商务/报价单v2.xlsx",
      size: 2048,
      modified_at: "2026-09-24T09:00:00",
      zone: "normal",
      gone: false,
      project_id: "p",
      project_name: "云图AI",
    },
    siblings: [{ id: 6, name: "报价单v1.xlsx", rel_path: "商务/报价单v1.xlsx", root_id: 1, modified_at: "2026-09-01T09:00:00" }],
    meetings: [row("a", day(0), 3, 754_000, "报价单下周发给甲方"), row("b", day(2), 1, 5_000, "报价单还没改完")],
    active_meetings: 2,
    ...overrides,
  };
}

function fileClient(graph: GraphPayload = withFiles(), overrides: Record<string, unknown> = {}) {
  const mentionResult = { meeting_id: "a", project_id: "p", stem_key: "报价单", file_id: 7, status: "active", picked: false };
  return makeClient(graph, {
    graphRoots: vi.fn(async () => ONLINE_ROOTS),
    meetingBrief: vi.fn(async (meetingId: string) => ({
      ...brief(meetingId),
      files: meetingId === "a" ? [QUOTE_FILE, ROSTER_FILE] : [],
      files_state: "done",
    })),
    getGraphFile: vi.fn(async () => fileDetail()),
    rejectFileMention: vi.fn(async () => ({ ...mentionResult, status: "rejected" })),
    restoreFileMention: vi.fn(async () => mentionResult),
    pickFileMention: vi.fn(async () => ({ ...mentionResult, file_id: 6, picked: true })),
    revealMaterial: vi.fn(async () => ({ ok: true, path: "" })),
    ...overrides,
  });
}

describe("ProjectGraph 会上提到的文件（2d）", () => {
  it("文件节点挂在材料那一侧；「提到」线默认淡色，悬停时加深并显示线上的字", async () => {
    render(<Harness apiClient={fileClient()} />);
    const node = await screen.findByRole("button", { name: "文件：报价单v2.xlsx" });
    expect(within(screen.getByRole("group", { name: "材料" })).getByRole("button", { name: "文件：报价单v2.xlsx" })).toBe(node);
    expect(node).toHaveTextContent("xlsx");
    const hit = screen.getByRole("button", { name: "连线：会上说『报价单』3 次 · 00:12:34" });
    const edge = hit.closest(".graph-edge")!;
    expect(edge).toHaveClass("graph-edge--mentioned");
    expect(edge).not.toHaveClass("is-lit");
    expect(edge.querySelector(".graph-edge__quote")).not.toBeNull();

    fireEvent.mouseEnter(hit);
    expect(edge).toHaveClass("is-lit");
    fireEvent.mouseLeave(hit);
    expect(edge).not.toHaveClass("is-lit");
    // 选中文件节点时，连到它的线也亮
    await userEvent.click(node);
    expect(hit.closest(".graph-edge")).toHaveClass("is-lit");
  });

  it("选中一场会时从简报补出它提到的全部文件，已有节点不挪；换一场会就收回去", async () => {
    const apiClient = fileClient();
    render(<Harness apiClient={apiClient} />);
    const existing = await screen.findByRole("button", { name: "文件：报价单v2.xlsx" });
    const before = existing.getAttribute("style");
    expect(screen.queryByRole("button", { name: "文件：排班表.xlsx" })).toBeNull();

    await userEvent.click(screen.getByRole("button", { name: /^会议：初审规则沟通 a/ }));
    expect(await screen.findByRole("button", { name: "文件：排班表.xlsx" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "连线：会上说『排班表』1 次 · 00:01:00" }).closest(".graph-edge")).toHaveClass("is-lit");
    expect(screen.getByRole("button", { name: "文件：报价单v2.xlsx" }).getAttribute("style")).toBe(before);
    // 已经在图上的文件不重复画线
    expect(screen.getAllByRole("button", { name: /^连线：会上说『报价单』/ })).toHaveLength(1);

    const panel = screen.getByRole("complementary", { name: "详情面板" });
    const list = await within(panel).findByRole("list", { name: "会上提到的文件" });
    expect(within(list).getByText("会上说『报价单』3 次")).toBeInTheDocument();
    expect(within(panel).getByText("会上提到的文件 2")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: /^会议：初审规则沟通 b/ }));
    await waitFor(() => expect(screen.queryByRole("button", { name: "文件：排班表.xlsx" })).toBeNull());
    expect(screen.getByRole("button", { name: "文件：报价单v2.xlsx" })).toBeInTheDocument();
  });

  it("文件面板：所在文件夹、修改时间、逐场被提到；从会点进来时［不是这份文件］立即生效，［撤销］改回来", async () => {
    const apiClient = fileClient();
    render(<Harness apiClient={apiClient} />);
    await userEvent.click(await screen.findByRole("button", { name: /^会议：初审规则沟通 a/ }));
    let panel = screen.getByRole("complementary", { name: "详情面板" });
    await userEvent.click(await within(panel).findByRole("button", { name: "报价单v2.xlsx" }));
    expect(screen.getByTestId("selection")).toHaveTextContent("file:7");
    expect(apiClient.getGraphFile).toHaveBeenCalledWith(7);

    panel = screen.getByRole("complementary", { name: "详情面板" });
    expect(await within(panel).findByText("/材料/云图AI/商务")).toBeInTheDocument();
    expect(within(panel).getByText("修改时间")).toBeInTheDocument();
    expect(within(panel).getByRole("heading", { name: "在 2 场会上被提到" })).toBeInTheDocument();
    expect(within(panel).getByText("报价单下周发给甲方")).toBeInTheDocument();
    expect(within(panel).getByRole("button", { name: "从 00:12:34 播放「初审规则沟通 a」" })).toBeInTheDocument();
    expect(within(panel).getByRole("heading", { name: "同名的还有 报价单v1.xlsx" })).toBeInTheDocument();
    expect(within(panel).getByRole("button", { name: "复制路径" })).toBeInTheDocument();
    await userEvent.click(within(panel).getByRole("button", { name: "在访达中显示" }));
    expect(apiClient.revealMaterial).toHaveBeenCalledWith("/材料/云图AI/商务/报价单v2.xlsx");
    // 只有点进来的那场会有［不是这份文件］
    expect(within(panel).getAllByRole("button", { name: "不是这份文件" })).toHaveLength(1);

    await userEvent.click(within(panel).getByRole("button", { name: "不是这份文件" }));
    expect(apiClient.rejectFileMention).toHaveBeenCalledWith("a", "报价单");
    const notice = await screen.findByRole("status");
    expect(notice).toHaveTextContent("已标成不是这份文件");
    expect(screen.getByTestId("selection")).toHaveTextContent("m:a");

    await userEvent.click(within(notice).getByRole("button", { name: "撤销" }));
    expect(apiClient.restoreFileMention).toHaveBeenCalledWith("a", "报价单");
    expect(await screen.findByText("已撤销：这场会又连回「报价单v2.xlsx」")).toBeInTheDocument();
  });

  it("［换成这份］：从会点进来只换那一场；直接点文件节点时提到它的会一起换", async () => {
    const apiClient = fileClient();
    render(<Harness apiClient={apiClient} />);
    await userEvent.click(await screen.findByRole("button", { name: "文件：报价单v2.xlsx" }));
    let panel = screen.getByRole("complementary", { name: "详情面板" });
    expect(await within(panel).findByText("报价单下周发给甲方")).toBeInTheDocument();
    expect(within(panel).queryByRole("button", { name: "不是这份文件" })).toBeNull();
    await userEvent.click(within(panel).getByRole("button", { name: "换成这份" }));
    expect(apiClient.pickFileMention).toHaveBeenCalledWith("a", "报价单", 6);
    expect(apiClient.pickFileMention).toHaveBeenCalledWith("b", "报价单", 6);
    expect(await screen.findByText("已把 2 场会换成「报价单v1.xlsx」")).toBeInTheDocument();

    vi.mocked(apiClient.pickFileMention).mockClear();
    await userEvent.click(screen.getByRole("button", { name: /^会议：初审规则沟通 a/ }));
    panel = screen.getByRole("complementary", { name: "详情面板" });
    await userEvent.click(await within(panel).findByRole("button", { name: "报价单v2.xlsx" }));
    panel = screen.getByRole("complementary", { name: "详情面板" });
    await userEvent.click(await within(panel).findByRole("button", { name: "换成这份" }));
    expect(apiClient.pickFileMention).toHaveBeenCalledTimes(1);
    expect(apiClient.pickFileMention).toHaveBeenCalledWith("a", "报价单", 6);
    expect(await screen.findByText("已换成「报价单v1.xlsx」")).toBeInTheDocument();
    expect(screen.getByTestId("selection")).toHaveTextContent("m:a");
  });

  it("点「提到」线：为什么相连写词、次数和原话 ▶，［不是这份文件］能 ⌘Z 撤销", async () => {
    const apiClient = fileClient();
    render(<Harness apiClient={apiClient} />);
    await userEvent.click(await screen.findByRole("button", { name: "连线：会上说『报价单』3 次 · 00:12:34" }));
    const panel = await screen.findByRole("complementary", { name: "详情面板" });
    expect(within(panel).getByRole("heading", { name: "为什么相连" })).toBeInTheDocument();
    expect(within(panel).getByText("会上说『报价单』3 次")).toBeInTheDocument();
    expect(await within(panel).findByText("报价单下周发给甲方")).toBeInTheDocument();
    expect(within(panel).getByRole("button", { name: "从 00:12:34 播放「初审规则沟通 a」" })).toBeInTheDocument();

    await userEvent.click(within(panel).getByRole("button", { name: "不是这份文件" }));
    expect(apiClient.rejectFileMention).toHaveBeenCalledWith("a", "报价单");
    expect(await screen.findByText("已标成不是这份文件")).toBeInTheDocument();
    fireEvent.keyDown(document.body, { key: "z", metaKey: true });
    await waitFor(() => expect(apiClient.restoreFileMention).toHaveBeenCalledWith("a", "报价单"));
  });

  it("会议面板里没有文件时按 files_state 说清原因；盘没插时列表照常、上面写一句", async () => {
    const texts: Record<FilesState, string> = {
      done: "这场会没提到项目文件夹里的文件名",
      indexing: "文件名还在认，认完后这里列出会上提到的文件",
      offline: "资料盘未连接，先按上次认得的算",
      no_project: "这场会还没归项目",
      no_root: "这个项目还没挂文件夹",
    };
    for (const [state, text] of Object.entries(texts) as Array<[FilesState, string]>) {
      forgetGraphCache();
      const apiClient = makeClient(payload(), {
        meetingBrief: vi.fn(async (meetingId: string) => ({ ...brief(meetingId), files: [], files_state: state })),
      });
      const view = render(<Harness apiClient={apiClient} initial="m:a" />);
      const panel = await screen.findByRole("complementary", { name: "详情面板" });
      expect(await within(panel).findByText(text)).toBeInTheDocument();
      expect(within(panel).queryByRole("list", { name: "会上提到的文件" })).toBeNull();
      view.unmount();
    }

    forgetGraphCache();
    const apiClient = makeClient(payload(), {
      meetingBrief: vi.fn(async (meetingId: string) => ({ ...brief(meetingId), files: [QUOTE_FILE], files_state: "offline" })),
    });
    render(<Harness apiClient={apiClient} initial="m:a" />);
    const panel = await screen.findByRole("complementary", { name: "详情面板" });
    expect(await within(panel).findByText("资料盘未连接，先按上次认得的算")).toBeInTheDocument();
    expect(within(panel).getByRole("list", { name: "会上提到的文件" })).toHaveTextContent("报价单v2.xlsx");
  });
});

describe("ProjectGraph 像是新需求、等补建的文件夹（2c）", () => {
  const suggested = (): GraphPayload =>
    payload({
      suggested_requirements: [
        { id: "nr:k1", kind: "suggested_requirement", name: "数据看板", meeting_ids: ["a", "b"], spoken: ["数据看板"], last_day: day(0), count: 2 },
      ],
      edges: [
        { id: "e:nr:k1:a", kind: "suggested", from: "m:a", to: "nr:k1", label: "", meeting_id: "a", name: "数据看板" },
        { id: "e:nr:k1:b", kind: "suggested", from: "m:b", to: "nr:k1", label: "", meeting_id: "b", name: "数据看板" },
      ],
    });

  function suggestedClient() {
    const undoUntil = new Date(Date.now() + 10 * 60_000).toISOString();
    return makeClient(suggested(), {
      nameCandidates: vi.fn(async () => ({
        hint: { kind: "requirement", name: "数据看板", spoken: ["数据看板"], project_id: "p", project_name: "云图AI" },
        candidates: [
          { name: "数据看板", folder_path: null, spoken: { count: 4, first_ms: 61_000, anchors_ms: [61_000] }, ai: true, similar_folder_path: null },
        ],
        meetings: [
          { id: "a", title: "初审规则沟通 a", date: "2026-09-26", said_ms: 61_000 },
          { id: "b", title: "初审规则沟通 b", date: "2026-09-24", said_ms: null },
        ],
        default_action: "create_requirement",
        project: { id: "p", name: "云图AI" },
        requirement_projects: [],
        folder: { mode: "none", reason: null },
        folders_state: "ready",
        create_parent: null,
        create_parent_source: null,
        create_parent_state: null,
      })),
      nameAsRequirement: vi.fn(async () => ({
        requirement_id: "r-new",
        requirement_title: "数据看板",
        project_id: "p",
        project_name: "云图AI",
        existing: false,
        priority: "P2",
        meetings_linked: 2,
        meetings_assigned: 0,
        meeting_ids: ["a", "b"],
        folder_attached: null,
        folder_error: null,
        event_id: 9,
        undo_until: undoUntil,
      })),
    });
  }

  it("虚线圆角框排在需求那一侧，和会之间是虚线；点虚线说会上说了几次", async () => {
    const apiClient = suggestedClient();
    render(<Harness apiClient={apiClient} />);
    const node = await screen.findByRole("button", { name: "像是新需求『数据看板』· 2 场会" });
    expect(node).toHaveClass("graph-node--suggested_requirement");
    expect(within(screen.getByRole("group", { name: "需求" })).getByRole("button", { name: "像是新需求『数据看板』· 2 场会" })).toBe(node);
    expect(node).toHaveTextContent("2 场会");
    const edges = screen.getAllByRole("button", { name: "连线：像是新需求『数据看板』" });
    expect(edges).toHaveLength(2);
    expect(edges[0].closest(".graph-edge")).toHaveClass("graph-edge--suggested");

    await userEvent.click(edges[0]);
    const panel = await screen.findByRole("complementary", { name: "详情面板" });
    expect(within(panel).getByRole("heading", { name: "为什么相连" })).toBeInTheDocument();
    expect(await within(panel).findByText("会上说『数据看板』4 次")).toBeInTheDocument();
    expect(within(panel).getByRole("button", { name: "从 00:01:01 播放" })).toBeInTheDocument();
    expect(apiClient.nameCandidates).toHaveBeenCalledWith("a");

    await userEvent.click(within(panel).getByRole("button", { name: "像是新需求『数据看板』：建成需求或不算 →" }));
    expect(screen.getByTestId("selection")).toHaveTextContent("nr:k1");
  });

  it("点节点在面板里打开和会议页同一个提示，对最近的那场会建成需求", async () => {
    const apiClient = suggestedClient();
    render(<Harness apiClient={apiClient} />);
    await userEvent.click(await screen.findByRole("button", { name: "像是新需求『数据看板』· 2 场会" }));
    const panel = await screen.findByRole("complementary", { name: "详情面板" });
    expect(await within(panel).findByText("像是『云图AI』里的一个新需求")).toBeInTheDocument();
    expect(await within(panel).findByDisplayValue("数据看板")).toBeInTheDocument();
    expect(await within(panel).findByText(/最近一场/)).toBeInTheDocument();
    await userEvent.click(within(panel).getByRole("button", { name: "建成需求" }));
    expect(apiClient.nameAsRequirement).toHaveBeenCalledWith("a", { title: "数据看板" });
    expect(await screen.findByRole("status")).toHaveTextContent("已在『云图AI』建好需求『数据看板』（P2）");
  });

  it("面板开着时从一个「像是新需求」换到另一个，不带上一个的会名和读失败", async () => {
    const graph = payload({
      suggested_requirements: [
        { id: "nr:k1", kind: "suggested_requirement", name: "数据看板", meeting_ids: ["a"], spoken: ["数据看板"], last_day: day(0), count: 1 },
        { id: "nr:k2", kind: "suggested_requirement", name: "审批流", meeting_ids: ["c"], spoken: ["审批流"], last_day: day(10), count: 1 },
      ],
      edges: [
        { id: "e:nr:k1:a", kind: "suggested", from: "m:a", to: "nr:k1", label: "", meeting_id: "a", name: "数据看板" },
        { id: "e:nr:k2:c", kind: "suggested", from: "m:c", to: "nr:k2", label: "", meeting_id: "c", name: "审批流" },
      ],
    });
    const apiClient = makeClient(graph, {
      // c 的简报和候选名都一直不回来，面板停在载入中
      meetingBrief: vi.fn((meetingId: string) => (meetingId === "c" ? new Promise(() => undefined) : Promise.resolve(brief(meetingId)))),
      nameCandidates: vi.fn((meetingId: string) =>
        meetingId === "a" ? Promise.reject(new Error("读不到")) : new Promise(() => undefined),
      ),
    });
    render(<Harness apiClient={apiClient} />);
    await userEvent.click(await screen.findByRole("button", { name: "像是新需求『数据看板』· 1 场会" }));
    const panel = await screen.findByRole("complementary", { name: "详情面板" });
    expect(await within(panel).findByText(/最近一场/)).toHaveTextContent("初审规则沟通 a");
    await userEvent.click(screen.getByRole("button", { name: "像是新需求『审批流』· 1 场会" }));
    await within(panel).findByDisplayValue("审批流");
    expect(within(panel).queryByText(/最近一场/)).toBeNull();

    await userEvent.click(screen.getByRole("button", { name: "连线：像是新需求『数据看板』" }));
    expect(await within(panel).findByText(/逐字稿里没数到这个词/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "连线：像是新需求『审批流』" }));
    await within(panel).findByText("会上说『审批流』");
    expect(within(panel).queryByText(/逐字稿里没数到这个词/)).toBeNull();
  });

  it("等补建的文件夹：灰色虚边，写插上后自动建；停了写原因", async () => {
    const pending = (state: "waiting" | "stopped", reason: string | null = null): GraphPayload =>
      payload({
        folders: [
          {
            id: "pending:p",
            kind: "pending",
            name: "云图AI",
            path: "/Volumes/资料盘/项目/云图AI",
            parent: "/Volumes/资料盘/项目",
            ring: "inner",
            state,
            reason,
          },
        ],
      });
    const view = render(<Harness apiClient={makeClient(pending("waiting"))} />);
    const node = await screen.findByRole("button", { name: "等补建的文件夹：云图AI" });
    expect(node).toHaveClass("is-pending");
    expect(node).toHaveTextContent("插上后自动建");
    await userEvent.click(node);
    const panel = await screen.findByRole("complementary", { name: "详情面板" });
    expect(within(panel).getByText("资料盘未连接，插上后自动建 /Volumes/资料盘/项目/云图AI")).toBeInTheDocument();
    view.unmount();

    forgetGraphCache();
    render(<Harness apiClient={makeClient(pending("stopped", "资料盘是只读的，建不了文件夹"))} initial="pending:p" />);
    const stopped = await screen.findByRole("button", { name: "等补建的文件夹：云图AI" });
    expect(stopped).toHaveClass("is-pending-stopped");
    expect(stopped).toHaveTextContent("资料盘是只读的，建不了文件夹");
    expect(
      within(await screen.findByRole("complementary", { name: "详情面板" })).getByText("资料盘是只读的，建不了文件夹"),
    ).toBeInTheDocument();
  });
});

describe("ProjectGraph 材料面板的补充", () => {
  it("散放文件每行都能复制路径", async () => {
    const writeText = vi.fn(async () => undefined);
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText } });
    render(<Harness apiClient={makeClient(payload(), { graphRoots: vi.fn(async () => ONLINE_ROOTS) })} />);
    await userEvent.click(await screen.findByRole("button", { name: "根目录里散放的文件" }));
    const panel = await screen.findByRole("complementary", { name: "详情面板" });
    const row = (await within(panel).findByText("报价单.xlsx")).closest("li")!;
    await userEvent.click(within(row).getByRole("button", { name: "复制路径" }));
    expect(writeText).toHaveBeenCalledWith("/材料/云图AI/报价单.xlsx");
    expect(await screen.findByText("已复制路径")).toBeInTheDocument();
  });

  it("根目录找不到时，文件夹面板里问是不是改了名（和项目页同一个组件）", async () => {
    const renameCandidates = vi.fn(async () => ({
      state: "ready",
      candidates: [
        {
          path: "/材料/云图AI-2026",
          name: "云图AI-2026",
          modified_at: "2026-09-20T10:00:00",
          strength: 3,
          evidence: { kind: "cards", text: "里面有这个项目的会议卡片" },
        },
      ],
      default_path: "/材料/云图AI-2026",
    }));
    const repointProjectMaterialRoot = vi.fn(async () => ({
      id: 1,
      project_id: "p",
      path: "/材料/云图AI-2026",
      exists: true,
      created_at: "",
      moved_folders: 2,
    }));
    const apiClient = makeClient(payload(), {
      graphRoots: vi.fn(async () => ({ ...ONLINE_ROOTS, roots: [{ ...ONLINE_ROOTS.roots[0], state: "missing" }] })),
      renameCandidates,
      repointProjectMaterialRoot,
    });
    render(<Harness apiClient={apiClient} />);
    await userEvent.click(await screen.findByRole("button", { name: "文件夹：云图AI" }));
    const panel = await screen.findByRole("complementary", { name: "详情面板" });
    const question = await within(panel).findByRole("group", { name: "是不是改了名" });
    expect(question).toHaveTextContent("找不到这个文件夹了。是不是改名成了『云图AI-2026』？（里面有这个项目的会议卡片）");
    expect(renameCandidates).toHaveBeenCalledWith("p", 1);
    await userEvent.click(within(question).getByRole("button", { name: "是它" }));
    expect(repointProjectMaterialRoot).toHaveBeenCalledWith("p", 1, "/材料/云图AI-2026");
    expect(await screen.findByText("材料根目录已改到 /材料/云图AI-2026，2 个需求文件夹一起改了")).toBeInTheDocument();
  });

  describe("取径器盖在节点面板里面时的 Esc", () => {
    function pickerClient() {
      return makeClient(payload(), {
        graphRoots: vi.fn(async () => ({ ...ONLINE_ROOTS, roots: [{ ...ONLINE_ROOTS.roots[0], state: "missing" }] })),
        renameCandidates: vi.fn(async () => ({ state: "ready", candidates: [], default_path: null })),
        browseMaterials: vi.fn(async () => ({
          base: "/Volumes/资料盘",
          path: "/Volumes/资料盘",
          parent: null,
          breadcrumbs: [{ name: "资料盘", path: "/Volumes/资料盘" }],
          dirs: [{ name: "蓝鲸云", path: "/Volumes/资料盘/蓝鲸云" }],
        })),
      });
    }

    async function openPicker() {
      render(<Harness apiClient={pickerClient()} />);
      await userEvent.click(await screen.findByRole("button", { name: "文件夹：云图AI" }));
      const panel = await screen.findByRole("complementary", { name: "详情面板" });
      await userEvent.click(await within(panel).findByRole("button", { name: "重新选…" }));
      return screen.findByRole("dialog", { name: "添加材料根目录" });
    }

    it("焦点在取径器里：Esc 只关取径器、面板还在；再按一次才关面板", async () => {
      const dialog = await openPicker();
      await waitFor(() => expect(dialog.contains(document.activeElement)).toBe(true));

      await userEvent.keyboard("{Escape}");
      await waitFor(() => expect(screen.queryByRole("dialog", { name: "添加材料根目录" })).toBeNull());
      expect(screen.getByRole("complementary", { name: "详情面板" })).toBeInTheDocument();
      expect(screen.getByTestId("selection")).toHaveTextContent("root:1");

      await userEvent.keyboard("{Escape}");
      await waitFor(() => expect(screen.queryByRole("complementary", { name: "详情面板" })).toBeNull());
    });

    it("焦点不在取径器里（点「›」进下一级后被点的按钮换掉了）：Esc 照样只关取径器，面板还在", async () => {
      await openPicker();
      await screen.findByText("蓝鲸云");
      act(() => (document.activeElement as HTMLElement | null)?.blur());
      expect(document.activeElement).toBe(document.body);

      await userEvent.keyboard("{Escape}");
      await waitFor(() => expect(screen.queryByRole("dialog", { name: "添加材料根目录" })).toBeNull());
      expect(screen.getByRole("complementary", { name: "详情面板" })).toBeInTheDocument();
      expect(screen.getByTestId("selection")).toHaveTextContent("root:1");

      await userEvent.keyboard("{Escape}");
      await waitFor(() => expect(screen.queryByRole("complementary", { name: "详情面板" })).toBeNull());
    });
  });

  it("面包屑最前面是「全部项目」，链接到全部项目概览", async () => {
    render(<Harness apiClient={makeClient()} />);
    expect(await screen.findByRole("link", { name: "全部项目" })).toHaveAttribute("href", "#graph");
  });
});

// ------------------------------------------------------------------ 第三期：关系图里的文件（3g）

function recentFile(fileId: number, name: string, state: RecentFile["state"] = "done"): RecentFile {
  return { file_id: fileId, name, ext: name.split(".").pop() ?? "", dir_rel: "方案", mtime: "2026-09-25T10:00:00+00:00", state };
}

const RECENT_ROOTS: GraphRootsPayload = {
  ...ONLINE_ROOTS,
  roots: [
    {
      ...ONLINE_ROOTS.roots[0],
      recent_files: [recentFile(21, "方案.docx", "pending"), recentFile(22, "截图.png", "waiting"), recentFile(23, "旧稿.pdf", "unreadable")],
      content: { files: 1234, done: 1020, unreadable: 7 },
    },
  ],
  loose: { count: 1, recent: [{ name: "报价单.xlsx", path: "/材料/云图AI/报价单.xlsx", size: 2048, mtime: "2026-09-24T09:00:00", file_id: 41 }] },
};

function preview(fileId: number, name: string): MaterialFilePreview {
  return {
    file: {
      id: fileId,
      name,
      ext: "xlsx",
      rel_path: `商务/${name}`,
      root_id: 1,
      folder_path: "/材料/云图AI/商务",
      path: `/材料/云图AI/商务/${name}`,
      size: 2048,
      modified_at: "2026-09-24T09:00:00",
      project_id: "p",
      project_name: "云图AI",
      root_online: true,
      gone: false,
    },
    state: { kind: "done", reason: null, note: null, what: null, paused: null, meeting: null, text: "" },
    preview: {
      kind: "text",
      lines: ["报价单第一行：初审规则服务费"],
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
  };
}

function openTask(id: string, title: string, meetingDate: string | null): Task {
  return {
    id,
    title,
    detail: "",
    status: "confirmed",
    origin: "ai",
    assignee: "me",
    project_id: "p",
    status_changed_at: "2026-09-20T00:00:00",
    created_at: "2026-09-20T00:00:00",
    updated_at: "2026-09-20T00:00:00",
    stall_days: 0,
    stalled: false,
    meeting_title: meetingDate ? `会 ${meetingDate}` : null,
    meeting_recording_date: meetingDate,
  };
}

function materialClient(graph: GraphPayload = withFiles(), overrides: Record<string, unknown> = {}) {
  return fileClient(graph, {
    graphRoots: vi.fn(async () => RECENT_ROOTS),
    getGraphFile: vi.fn(async (fileId: number) =>
      fileId === 7
        ? fileDetail({ deliverables: [] })
        : fileDetail({
            file: { ...fileDetail().file, id: fileId, name: `文件${fileId}.docx`, rel_path: `方案/文件${fileId}.docx` },
            meetings: [],
            active_meetings: 0,
            siblings: [],
            deliverables: [],
          }),
    ),
    getMaterialPreview: vi.fn(async (fileId: number) => preview(fileId, fileId === 7 ? "报价单v2.xlsx" : `文件${fileId}.docx`)),
    tasks: vi.fn(async () => ({
      items: [openTask("t-old", "旧会里的任务", "2026-08-01"), openTask("t-new", "整理报价单", "2026-09-25")],
      total: 2,
      limit: 500,
      offset: 0,
    })),
    addDeliverable: vi.fn(async (taskId: string) => ({ id: taskId, deliverables: [], events: [], deliverable_id: 9 })),
    removeDeliverable: vi.fn(async (taskId: string) => ({ id: taskId, deliverables: [], events: [] })),
    ...overrides,
  });
}

describe("ProjectGraph 关系图里的文件（3g）", () => {
  it("最近改过的文件挂在文件夹外侧，带读到哪一步的标记；根目录面板顶上能点", async () => {
    const apiClient = materialClient(payload());
    render(<Harness apiClient={apiClient} />);
    const pending = await screen.findByRole("button", { name: "最近改过的文件：方案.docx" });
    expect(pending.querySelector(".graph-file__mark--system")).toHaveTextContent("◷");
    expect(screen.getByRole("button", { name: "最近改过的文件：截图.png" }).querySelector(".graph-file__mark--you")).toHaveTextContent("◷");
    expect(screen.getByRole("button", { name: "最近改过的文件：旧稿.pdf" }).querySelector(".graph-file__mark--unreadable")).toHaveTextContent("⊘");

    await userEvent.click(screen.getByRole("button", { name: "文件夹：云图AI" }));
    const panel = await screen.findByRole("complementary", { name: "详情面板" });
    const list = await within(panel).findByRole("list", { name: "最近改过的文件" });
    expect(within(list).getByText(/还没读到/)).toBeInTheDocument();
    await userEvent.click(within(list).getByRole("button", { name: "截图.png" }));
    expect(screen.getByTestId("selection")).toHaveTextContent("file:22");
    expect(apiClient.getGraphFile).toHaveBeenCalledWith(22);
  });

  it("文件夹面板、散放文件的文件行能点：在图上补出文件并打开文件面板", async () => {
    const apiClient = materialClient(payload(), {
      graphExpand: vi.fn(async (rootId: number, dir = "") => {
        const base = expandPayload(rootId, dir);
        return { ...base, files: base.files.map((file) => ({ ...file, file_id: dir ? 32 : 31 })) };
      }),
    });
    render(<Harness apiClient={apiClient} />);
    await userEvent.click(await screen.findByRole("button", { name: "文件夹：云图AI" }));
    let panel = await screen.findByRole("complementary", { name: "详情面板" });
    await userEvent.click(await within(panel).findByRole("button", { name: "报价单.xlsx" }));
    expect(screen.getByTestId("selection")).toHaveTextContent("file:31");
    expect(await screen.findByRole("button", { name: "文件：报价单.xlsx" })).toBeInTheDocument();
    panel = screen.getByRole("complementary", { name: "详情面板" });
    expect(within(panel).getByText("文件")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "根目录里散放的文件" }));
    panel = await screen.findByRole("complementary", { name: "详情面板" });
    await userEvent.click(await within(panel).findByRole("button", { name: "报价单.xlsx" }));
    expect(screen.getByTestId("selection")).toHaveTextContent("file:41");
    expect(await screen.findByRole("button", { name: "文件：报价单.xlsx" })).toBeInTheDocument();
  });

  it("图上放不下点出来的文件时直接打开预览抽屉，不留空选中", async () => {
    // 材料那一侧的槽位都被会上提到的文件占满
    const crowded = payload({
      files: Array.from({ length: 30 }, (_, index) => ({
        id: `file:${500 + index}`,
        kind: "file" as const,
        file_id: 500 + index,
        name: `提到${index}.xlsx`,
        ext: "xlsx",
        rel_path: "x",
        root_id: 1,
        folder: "root:1",
      })),
    });
    const onOpenPreview = vi.fn();
    const apiClient = materialClient(crowded, {
      graphExpand: vi.fn(async (rootId: number, dir = "") => {
        const base = expandPayload(rootId, dir);
        return { ...base, files: base.files.map((file) => ({ ...file, file_id: 31 })) };
      }),
    });
    render(<Harness apiClient={apiClient} handlers={{ onOpenPreview }} initial="root:1" />);
    const panel = await screen.findByRole("complementary", { name: "详情面板" });
    await userEvent.click(await within(panel).findByRole("button", { name: "报价单.xlsx" }));
    await waitFor(() => expect(onOpenPreview).toHaveBeenCalledWith(31));
    expect(screen.getByTestId("selection")).toHaveTextContent("");
    expect(screen.queryByText("要看的节点不在当前的图上，换个时间窗试试")).toBeNull();
  });

  it("深链到不在图上的文件：先取文件信息补成节点，不说不在图上", async () => {
    const apiClient = materialClient(payload());
    render(<Harness apiClient={apiClient} initial="file:77" />);
    expect(await screen.findByRole("button", { name: "文件：文件77.docx" })).toBeInTheDocument();
    expect(apiClient.getGraphFile).toHaveBeenCalledWith(77);
    expect(screen.getByTestId("selection")).toHaveTextContent("file:77");
    expect(screen.queryByText("要看的节点不在当前的图上，换个时间窗试试")).toBeNull();
    expect(await screen.findByRole("complementary", { name: "详情面板" })).toBeInTheDocument();
  });

  it("文件面板：预览、交付物，［标为交付物 ▾］按最近的会排、能筛，选了立即生效，［撤销］删掉", async () => {
    const apiClient = materialClient();
    render(<Harness apiClient={apiClient} />);
    await userEvent.click(await screen.findByRole("button", { name: "文件：报价单v2.xlsx" }));
    const panel = screen.getByRole("complementary", { name: "详情面板" });
    expect(within(panel).getByText("文件")).toBeInTheDocument();
    expect(await within(panel).findByText("报价单第一行：初审规则服务费")).toBeInTheDocument();
    expect(apiClient.getMaterialPreview).toHaveBeenCalledWith(7, "preview");
    expect(within(panel).getByText("还不是哪个任务的交付物")).toBeInTheDocument();

    await userEvent.click(within(panel).getByRole("button", { name: "标为交付物 ▾" }));
    const choices = await within(panel).findByRole("list", { name: "选一个任务" });
    expect(within(choices).getAllByRole("button").map((button) => button.textContent)).toEqual(["整理报价单", "旧会里的任务"]);
    expect(apiClient.tasks).toHaveBeenCalledWith({ project_id: "p", status: "pending_confirm,confirmed,in_progress", limit: 500 });
    await userEvent.type(within(panel).getByRole("searchbox", { name: "筛选任务" }), "报价");
    expect(within(choices).getAllByRole("button").map((button) => button.textContent)).toEqual(["整理报价单"]);

    const graphCalls = vi.mocked(apiClient.graph).mock.calls.length;
    await userEvent.click(within(choices).getByRole("button", { name: "整理报价单" }));
    expect(apiClient.addDeliverable).toHaveBeenCalledWith("t-new", { file_id: 7 });
    const notice = await screen.findByRole("status");
    expect(notice).toHaveTextContent("已标为『整理报价单』的交付物");
    await waitFor(() => expect(vi.mocked(apiClient.graph).mock.calls.length).toBeGreaterThan(graphCalls));

    await userEvent.click(within(notice).getByRole("button", { name: "撤销" }));
    expect(apiClient.removeDeliverable).toHaveBeenCalledWith("t-new", 9);
    expect(await screen.findByText("已撤销：「报价单v2.xlsx」不再是「整理报价单」的交付物")).toBeInTheDocument();
  });

  it("文件面板列出是哪些任务的交付物", async () => {
    const apiClient = materialClient(withFiles(), {
      getGraphFile: vi.fn(async () =>
        fileDetail({ deliverables: [{ deliverable_id: 3, task_id: "t1", title: "整理报价单", status: "in_progress" }] }),
      ),
    });
    render(<Harness apiClient={apiClient} />);
    await userEvent.click(await screen.findByRole("button", { name: "文件：报价单v2.xlsx" }));
    const panel = screen.getByRole("complementary", { name: "详情面板" });
    const list = await within(panel).findByRole("list", { name: "是哪些任务的交付物" });
    expect(list).toHaveTextContent("整理报价单");
    expect(list).toHaveTextContent("进行中");
  });

  it("旧后端没有这些接口时不显示：交付物、［标为交付物］、预览、会议记录的文件", async () => {
    const apiClient = fileClient(withFiles(), { graphRoots: vi.fn(async () => ONLINE_ROOTS) });
    render(<Harness apiClient={apiClient} />);
    await userEvent.click(await screen.findByRole("button", { name: "文件：报价单v2.xlsx" }));
    const panel = screen.getByRole("complementary", { name: "详情面板" });
    expect(await within(panel).findByText("/材料/云图AI/商务")).toBeInTheDocument();
    expect(within(panel).queryByRole("button", { name: "标为交付物 ▾" })).toBeNull();
    expect(within(panel).queryByText("还不是哪个任务的交付物")).toBeNull();

    await userEvent.click(screen.getByRole("button", { name: "文件夹：声档会议记录/" }));
    const cards = await screen.findByRole("complementary", { name: "详情面板" });
    expect(within(cards).getByText("已写 3 张 · 停了 0 张 · 在等 0 张")).toBeInTheDocument();
    expect(within(cards).queryByRole("heading", { name: "文件" })).toBeNull();
  });

  it("需求文件夹、声档会议记录面板的「文件」一节：从库里列，点了在图上打开", async () => {
    const graph = payload({
      folders: [
        ...payload().folders,
        { id: "rf:5", kind: "requirement_folder", name: "白名单", path: "/材料/云图AI/白名单", ring: "middle", requirement_id: "r1", folder_id: 5 },
      ],
    });
    const requirementFolderFiles = vi.fn(async () => ({
      folder_id: 5,
      path: "/材料/云图AI/白名单",
      exists: true,
      total: 2,
      capped: false,
      items: [
        { relative_path: "名单.xlsx", size_bytes: 10, modified_at: "2026-09-20T10:00:00", file_id: 51 },
        { relative_path: "子/说明.docx", size_bytes: 10, modified_at: "2026-09-19T10:00:00" },
      ],
    }));
    const graphCardsFiles = vi.fn(async () => ({
      files: [{ file_id: 61, name: "0926 周会.md", rel_path: "声档会议记录/0926 周会.md", root_id: 1, mtime: "2026-09-26T10:00:00" }],
    }));
    const apiClient = materialClient(graph, {
      graphRoots: vi.fn(async () => ({
        ...RECENT_ROOTS,
        folders: [
          { id: "rf:5", folder_id: 5, requirement_id: "r1", path: "/材料/云图AI/白名单", state: "online", root_id: 1, recent_files: [recentFile(52, "最新名单.xlsx")] },
        ],
      })),
      requirementFolderFiles,
      graphCardsFiles,
    });
    render(<Harness apiClient={apiClient} initial="rf:5" />);
    let panel = await screen.findByRole("complementary", { name: "详情面板" });
    expect(await within(panel).findByRole("list", { name: "最近改过的文件" })).toHaveTextContent("最新名单.xlsx");
    const files = await within(panel).findByRole("list", { name: "文件夹里的文件" });
    expect(requirementFolderFiles).toHaveBeenCalledWith("r1", 5, { limit: 40 });
    expect(within(files).getByText("子/说明.docx")).toBeInTheDocument();
    await userEvent.click(within(files).getByRole("button", { name: "名单.xlsx" }));
    expect(screen.getByTestId("selection")).toHaveTextContent("file:51");
    expect(apiClient.getGraphFile).toHaveBeenCalledWith(51);

    await userEvent.click(screen.getByRole("button", { name: "文件夹：声档会议记录/" }));
    panel = await screen.findByRole("complementary", { name: "详情面板" });
    const cardList = await within(panel).findByRole("list", { name: "会议记录文件" });
    await userEvent.click(within(cardList).getByRole("button", { name: "0926 周会.md" }));
    expect(screen.getByTestId("selection")).toHaveTextContent("file:61");
  });

  it("项目面板每个根目录写「文件名 N 个 · 已读 N 个 · 读不了 N 个」；在图上找能找文件", async () => {
    const apiClient = materialClient(withFiles(), { graphFulltext: vi.fn(async () => ({ variants: [], total: 0, meeting_count: 0, meetings: [] })) });
    render(<Harness apiClient={apiClient} initial="project" />);
    const panel = await screen.findByRole("complementary", { name: "详情面板" });
    expect(await within(panel).findByText("文件名 1,234 个 · 已读 1,020 个 · 读不了 7 个")).toBeInTheDocument();

    const input = screen.getByRole("searchbox", { name: "在图上找" });
    await userEvent.type(input, "报价单");
    await userEvent.keyboard("{Enter}");
    expect(screen.getByTestId("selection")).toHaveTextContent("file:7");
  });

  it("展开一场会：任务卡右边挂交付物小签，点了打开预览抽屉；任务面板里的文件也能点", async () => {
    const onOpenPreview = vi.fn();
    const deliverables = [
      { id: 1, kind: "file", url: "/材料/云图AI/交付/定稿.pdf", title: "", file_id: 71, name: "定稿.pdf", gone: false },
      { id: 2, kind: "file", url: "/材料/云图AI/交付/旧稿.pdf", title: "", file_id: null, name: "旧稿.pdf", gone: true },
    ];
    const apiClient = focusClient({
      graphMeetingFocus: vi.fn(async () => focusPayload({ tasks: [focusTask("t1", 300_000, { deliverables })] })),
    });
    render(<Harness apiClient={apiClient} handlers={{ onOpenPreview }} />);
    fireEvent.doubleClick(await screen.findByRole("button", { name: /^会议：初审规则沟通 a/ }));
    const view = await screen.findByRole("application", { name: "展开的会：初审规则沟通 a" });
    await userEvent.click(await within(view).findByRole("button", { name: "交付物 · 你标的：定稿.pdf，点了预览" }));
    expect(onOpenPreview).toHaveBeenCalledWith(71);

    await userEvent.click(within(view).getByRole("button", { name: /^任务（已确认）：任务 t1/ }));
    const panel = await screen.findByRole("complementary", { name: "详情面板" });
    await userEvent.click(within(panel).getByRole("button", { name: "定稿.pdf" }));
    expect(onOpenPreview).toHaveBeenCalledTimes(2);
    expect(within(panel).getByText(/找不到这个文件了/)).toBeInTheDocument();
  });

  it("4e：在问的交付物小签只在新后台（有 linksFlags）画，旧后台照旧画已登记的", async () => {
    const deliverables = [
      { id: 1, kind: "file", url: "/材料/云图AI/交付/定稿.pdf", title: "", file_id: 71, name: "定稿.pdf", gone: false },
    ];
    const asks = [{ relation_id: 61, file_id: 930, name: "方案.key", ext: "key" }];
    const focus = focusPayload({ tasks: [focusTask("t1", 300_000, { deliverables, asks })] });
    const onOpenPreview = vi.fn();
    const old = render(<Harness apiClient={focusClient({ graphMeetingFocus: vi.fn(async () => focus) })} handlers={{ onOpenPreview }} />);
    fireEvent.doubleClick(await screen.findByRole("button", { name: /^会议：初审规则沟通 a/ }));
    let view = await screen.findByRole("application", { name: "展开的会：初审规则沟通 a" });
    expect(await within(view).findByRole("button", { name: "交付物 · 你标的：定稿.pdf，点了预览" })).toBeInTheDocument();
    expect(within(view).queryByRole("button", { name: /^交付物？/ })).not.toBeInTheDocument();
    old.unmount();

    render(
      <LinksFlagsContext.Provider value={{ linksEnabled: true, semanticEnabled: true, llmConfigured: true }}>
        <Harness apiClient={focusClient({ graphMeetingFocus: vi.fn(async () => focus) })} handlers={{ onOpenPreview }} />
      </LinksFlagsContext.Provider>,
    );
    fireEvent.doubleClick(await screen.findByRole("button", { name: /^会议：初审规则沟通 a/ }));
    view = await screen.findByRole("application", { name: "展开的会：初审规则沟通 a" });
    await userEvent.click(
      await within(view).findByRole("button", { name: "交付物？：方案.key，等你认交付物，另有 1 个已登记，点了预览" }),
    );
    expect(onOpenPreview).toHaveBeenCalledWith(930);
  });
});

// ------------------------------------------------------------------ 第四期 4b：放宽的提到（会上换了叫法的文件）

const LOOSE_FILE = briefFile(7, "报价单v2.xlsx", {
  count: 2,
  first_ms: 754_000,
  needle: "上周那版报价单",
  relation_id: 11,
  phrase: "上周那版报价单",
  via: "time_hint",
});

const LINKS_FLAGS = { linksEnabled: true, semanticEnabled: true, llmConfigured: true };

function looseDetail(status: "active" | "rejected" = "active"): GraphFileDetail {
  const base = fileDetail();
  return {
    ...base,
    meetings: [
      { ...base.meetings[0], needle: "上周那版报价单", count: 2, status, relation_id: 11, phrase: "上周那版报价单", via: "time_hint" },
      base.meetings[1],
    ],
    active_meetings: status === "active" ? 2 : 1,
  };
}

function looseClient(overrides: Record<string, unknown> = {}) {
  const undoUntil = new Date(Date.now() + 600_000).toISOString();
  const relation = { id: 11, kind: "mention", status: "rejected", by_you: true, meeting_id: "a", at_ms: 754_000 };
  return fileClient(withFiles({ edges: [] }), {
    meetingBrief: vi.fn(async (meetingId: string) => ({
      ...brief(meetingId),
      files: meetingId === "a" ? [LOOSE_FILE] : [],
      files_state: "done",
      loose_state: null,
    })),
    getGraphFile: vi.fn(async () => looseDetail()),
    answerRelation: vi.fn(async () => ({ relation, undo_until: undoUntil })),
    undoRelation: vi.fn(async () => ({ relation: { ...relation, status: "shown" }, removed_deliverable_id: null })),
    retryLinks: vi.fn(async () => ({ requeued: 1 })),
    ...overrides,
  });
}

function WithFlags({ children }: { children: ReactNode }) {
  return <LinksFlagsContext.Provider value={LINKS_FLAGS}>{children}</LinksFlagsContext.Provider>;
}

async function openFileFromMeeting() {
  await userEvent.click(await screen.findByRole("button", { name: /^会议：初审规则沟通 a/ }));
  let panel = screen.getByRole("complementary", { name: "详情面板" });
  await userEvent.click(await within(panel).findByRole("button", { name: "报价单v2.xlsx" }));
  panel = screen.getByRole("complementary", { name: "详情面板" });
  await within(panel).findByText("报价单下周发给甲方");
  return panel;
}

describe("ProjectGraph 放宽的提到（4b）", () => {
  it("会议面板：放宽行写「说的是『…』」，补出的线写「会上说『…』等 N 处」", async () => {
    render(<Harness apiClient={looseClient()} />);
    await userEvent.click(await screen.findByRole("button", { name: /^会议：初审规则沟通 a/ }));
    const panel = screen.getByRole("complementary", { name: "详情面板" });
    const list = await within(panel).findByRole("list", { name: "会上提到的文件" });
    expect(within(list).getByText("说的是『上周那版报价单』")).toBeInTheDocument();
    expect(within(list).queryByText(/次$/)).toBeNull();
    expect(await screen.findByRole("button", { name: "连线：会上说『上周那版报价单』等 2 处 · 00:12:34" })).toBeInTheDocument();
  });

  it("会议面板的状态句：七种说法，最多一个按钮；［现在重试］放回排队后重取", async () => {
    const texts = [
      "会上换了叫法的文件还在整理",
      "没配置 AI，会上换了叫法的文件先不整理",
      "AI 的 key 不对，会上换了叫法的文件先不整理",
      "今天的 AI 用量到上限了，明天接着整理",
      "AI 账户余额不足，会上换了叫法的文件先不整理",
      "AI 连不上，过一会儿自动再试",
      "这场会的 AI 整理没做成",
    ];
    for (const text of texts) {
      forgetGraphCache();
      const failed = text === "这场会的 AI 整理没做成";
      const state: LinksState = {
        kind: text === texts[0] ? "waiting" : "stopped",
        text,
        action: failed ? { kind: "retry", label: "现在重试" } : null,
      };
      const apiClient = looseClient({
        meetingBrief: vi.fn(async (meetingId: string) => ({ ...brief(meetingId), files: [], files_state: "done", loose_state: state })),
      });
      const view = render(
        <WithFlags>
          <Harness apiClient={apiClient} initial="m:a" />
        </WithFlags>,
      );
      const panel = await screen.findByRole("complementary", { name: "详情面板" });
      const line = (await within(panel).findByText(text)).closest("p")!;
      expect(within(line).queryAllByRole("button")).toHaveLength(failed ? 1 : 0);
      expect(screen.queryByRole("alert")).toBeNull();
      if (failed) {
        await userEvent.click(within(line).getByRole("button", { name: "现在重试" }));
        expect(apiClient.retryLinks).toHaveBeenCalledTimes(1);
      }
      view.unmount();
    }
  });

  it("还在整理时 30 秒的刷新重取这场会的简报；离开那场会后不再重取", async () => {
    const intervals: Array<() => void> = [];
    const realSetInterval = window.setInterval.bind(window);
    const spy = vi.spyOn(window, "setInterval").mockImplementation(((handler: TimerHandler, timeout?: number) => {
      if (timeout === 30_000 && typeof handler === "function") intervals.push(handler as () => void);
      return realSetInterval(handler, timeout);
    }) as typeof window.setInterval);
    try {
      const waiting: LinksState = { kind: "waiting", text: "会上换了叫法的文件还在整理", action: null };
      const apiClient = looseClient({
        meetingBrief: vi.fn(async (meetingId: string) => ({ ...brief(meetingId), files: [], files_state: "done", loose_state: waiting })),
      });
      render(
        <WithFlags>
          <Harness apiClient={apiClient} />
        </WithFlags>,
      );
      await userEvent.click(await screen.findByRole("button", { name: /^会议：初审规则沟通 a/ }));
      const panel = screen.getByRole("complementary", { name: "详情面板" });
      await within(panel).findByText("会上换了叫法的文件还在整理");
      const tick = () => act(() => intervals.forEach((handler) => handler()));
      const briefCalls = () => vi.mocked(apiClient.meetingBrief).mock.calls.filter(([id]) => id === "a").length;
      expect(intervals.length).toBeGreaterThan(0);

      // 选着这场会：刷新顺带重取
      let before = briefCalls();
      await tick();
      await waitFor(() => expect(briefCalls()).toBeGreaterThan(before));

      // 离开这场会：刷新不再重取，回来时用的是缓存
      fireEvent.keyDown(within(panel).getByRole("button", { name: "关闭面板" }), { key: "Escape" });
      await waitFor(() => expect(screen.getByTestId("selection")).toHaveTextContent(""));
      before = briefCalls();
      await tick();
      await tick();
      await userEvent.click(screen.getByRole("button", { name: /^会议：初审规则沟通 a/ }));
      await within(screen.getByRole("complementary", { name: "详情面板" })).findByText("会上换了叫法的文件还在整理");
      expect(briefCalls()).toBe(before);
    } finally {
      spy.mockRestore();
    }
  });

  it("「提到」线：文件详情读到之前［不是这份文件］按不了，读到后放宽行走 answerRelation", async () => {
    let release: (value: GraphFileDetail) => void = () => {};
    const apiClient = looseClient({
      getGraphFile: vi.fn(() => new Promise<GraphFileDetail>((resolve) => (release = resolve))),
    });
    render(<Harness apiClient={apiClient} />);
    await userEvent.click(await screen.findByRole("button", { name: /^会议：初审规则沟通 a/ }));
    await userEvent.click(await screen.findByRole("button", { name: "连线：会上说『上周那版报价单』等 2 处 · 00:12:34" }));
    const panel = await screen.findByRole("complementary", { name: "详情面板" });
    const button = within(panel).getByRole("button", { name: "不是这份文件" });
    expect(button).toBeDisabled();
    await userEvent.click(button);
    expect(apiClient.rejectFileMention).not.toHaveBeenCalled();
    expect(apiClient.answerRelation).not.toHaveBeenCalled();

    await act(async () => release(looseDetail()));
    await waitFor(() => expect(within(panel).getByRole("button", { name: "不是这份文件" })).toBeEnabled());
    await userEvent.click(within(panel).getByRole("button", { name: "不是这份文件" }));
    expect(apiClient.answerRelation).toHaveBeenCalledWith(11, { answer: "no" });
    expect(apiClient.rejectFileMention).not.toHaveBeenCalled();
  });

  it("旧后台：简报没有 loose_state、行里没有 relation_id 时照第二期显示", async () => {
    const apiClient = fileClient();
    render(
      <WithFlags>
        <Harness apiClient={apiClient} initial="m:a" />
      </WithFlags>,
    );
    const panel = await screen.findByRole("complementary", { name: "详情面板" });
    const list = await within(panel).findByRole("list", { name: "会上提到的文件" });
    expect(within(list).getByText("会上说『报价单』3 次")).toBeInTheDocument();
    expect(within(panel).queryByText(/说的是/)).toBeNull();
    expect(within(panel).queryByText(/会上换了叫法/)).toBeNull();
  });

  it("文件面板：放宽行［不是这份文件］走 answerRelation，提示进撤销栈，［撤销］调 undoRelation", async () => {
    const apiClient = looseClient();
    render(<Harness apiClient={apiClient} />);
    const panel = await openFileFromMeeting();
    expect(within(panel).getByText("说的是『上周那版报价单』")).toBeInTheDocument();
    expect(within(panel).getByText("1 次")).toBeInTheDocument(); // 字面那一行照旧

    await userEvent.click(within(panel).getByRole("button", { name: "不是这份文件" }));
    expect(apiClient.answerRelation).toHaveBeenCalledWith(11, { answer: "no" });
    expect(apiClient.rejectFileMention).not.toHaveBeenCalled();
    const notice = await screen.findByRole("status");
    expect(notice).toHaveTextContent("已记下：『上周那版报价单』不是这份文件");
    await userEvent.click(within(notice).getByRole("button", { name: "撤销" }));
    await waitFor(() => expect(apiClient.undoRelation).toHaveBeenCalledWith(11));
    expect(apiClient.restoreFileMention).not.toHaveBeenCalled();
  });

  it("文件面板：放宽行［换成这份］发 pick，⌘Z 能撤；标过的放宽行从「你标过…」发 restore", async () => {
    const apiClient = looseClient();
    render(<Harness apiClient={apiClient} />);
    let panel = await openFileFromMeeting();
    await userEvent.click(within(panel).getByRole("button", { name: "换成这份" }));
    expect(apiClient.answerRelation).toHaveBeenCalledWith(11, { answer: "pick", file_id: 6 });
    expect(apiClient.pickFileMention).not.toHaveBeenCalled();
    expect(await screen.findByText("已换成「报价单v1.xlsx」")).toBeInTheDocument();
    fireEvent.keyDown(document.body, { key: "z", metaKey: true });
    await waitFor(() => expect(apiClient.undoRelation).toHaveBeenCalledWith(11));

    vi.mocked(apiClient.getGraphFile).mockImplementation(async () => looseDetail("rejected"));
    await userEvent.click(await screen.findByRole("button", { name: "文件：报价单v2.xlsx" }));
    panel = screen.getByRole("complementary", { name: "详情面板" });
    const row = (await within(panel).findByText("你标过「初审规则沟通 a」说的不是这份文件")).closest("li")!;
    await userEvent.click(within(row).getByRole("button", { name: "撤销" }));
    expect(apiClient.answerRelation).toHaveBeenCalledWith(11, { answer: "restore" });
    expect(apiClient.restoreFileMention).not.toHaveBeenCalled();
  });

  function twoLooseDetail(): GraphFileDetail {
    const base = looseDetail();
    return {
      ...base,
      meetings: [
        base.meetings[0],
        { ...base.meetings[1], needle: "那版报价", status: "active", relation_id: 12, phrase: "那版报价", via: "time_hint" },
      ],
    };
  }

  it("文件面板：一次把几场会的放宽行［换成这份］，提示带一个［撤销］，点了逐条撤", async () => {
    const apiClient = looseClient({ getGraphFile: vi.fn(async () => twoLooseDetail()) });
    render(<Harness apiClient={apiClient} />);
    await userEvent.click(await screen.findByRole("button", { name: "文件：报价单v2.xlsx" }));
    const panel = screen.getByRole("complementary", { name: "详情面板" });
    await userEvent.click(await within(panel).findByRole("button", { name: "换成这份" }));
    expect(apiClient.answerRelation).toHaveBeenCalledWith(11, { answer: "pick", file_id: 6 });
    expect(apiClient.answerRelation).toHaveBeenCalledWith(12, { answer: "pick", file_id: 6 });
    expect(apiClient.pickFileMention).not.toHaveBeenCalled();
    const notice = (await screen.findByText("已把 2 场会换成「报价单v1.xlsx」")).closest("[role='status']") as HTMLElement;
    // 一句话、一个［撤销］（另一个是提示条自带的关闭）
    expect(within(notice).getAllByRole("button", { name: "撤销" })).toHaveLength(1);
    await userEvent.click(within(notice).getByRole("button", { name: "撤销" }));
    await waitFor(() => expect(apiClient.undoRelation).toHaveBeenCalledTimes(2));
    expect(apiClient.undoRelation).toHaveBeenCalledWith(11);
    expect(apiClient.undoRelation).toHaveBeenCalledWith(12);
    expect(await screen.findByText("已撤销")).toBeInTheDocument();
  });

  it("文件面板：一批［换成这份］中途有一条出错，已经换过的撤回，提示出错那一句", async () => {
    const undoUntil = new Date(Date.now() + 600_000).toISOString();
    const apiClient = looseClient({
      getGraphFile: vi.fn(async () => twoLooseDetail()),
      answerRelation: vi.fn(async (relationId: number) => {
        if (relationId === 12) throw new ApiError("这条已经处理过了", 409, { detail: "这条已经处理过了" });
        return { relation: { id: relationId }, undo_until: undoUntil };
      }),
    });
    render(<Harness apiClient={apiClient} />);
    await userEvent.click(await screen.findByRole("button", { name: "文件：报价单v2.xlsx" }));
    const panel = screen.getByRole("complementary", { name: "详情面板" });
    await userEvent.click(await within(panel).findByRole("button", { name: "换成这份" }));
    expect(await screen.findByText("这条已经处理过了")).toBeInTheDocument();
    expect(apiClient.undoRelation).toHaveBeenCalledTimes(1);
    expect(apiClient.undoRelation).toHaveBeenCalledWith(11);
    expect(screen.queryByText(/场会换成/)).toBeNull();
    // 撤回过了，⌘Z 不再有这一批
    fireEvent.keyDown(document.body, { key: "z", metaKey: true });
    expect(await screen.findByText("没有能撤销的操作了（只保留 10 分钟内的）")).toBeInTheDocument();
    expect(apiClient.undoRelation).toHaveBeenCalledTimes(1);
  });

  it("文件面板：一批［换成这份］撤销到一半断网：这一步只留没撤成的那几条，再撤只发那几条", async () => {
    const detail = () => {
      const base = twoLooseDetail();
      return { ...base, meetings: [...base.meetings, { ...base.meetings[0], meeting_id: "c", relation_id: 13 }] };
    };
    const undoRelation = vi.fn(async (relationId: number) => {
      // 第一次撤：11 成功，12 断网，13 成功
      if (relationId === 12 && undoRelation.mock.calls.filter(([id]) => id === 12).length === 1) throw new TypeError("Failed to fetch");
      return { relation: {}, removed_deliverable_id: null };
    });
    const apiClient = looseClient({ getGraphFile: vi.fn(async () => detail()), undoRelation });
    render(<Harness apiClient={apiClient} />);
    await userEvent.click(await screen.findByRole("button", { name: "文件：报价单v2.xlsx" }));
    const panel = screen.getByRole("complementary", { name: "详情面板" });
    await userEvent.click(await within(panel).findByRole("button", { name: "换成这份" }));
    const notice = (await screen.findByText("已把 3 场会换成「报价单v1.xlsx」")).closest("[role='status']") as HTMLElement;

    await userEvent.click(within(notice).getByRole("button", { name: "撤销" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Failed to fetch");
    expect(undoRelation.mock.calls.map(([id]) => id)).toEqual([11, 12, 13]);

    fireEvent.keyDown(document.body, { key: "z", metaKey: true });
    expect(await screen.findByText("已撤销")).toBeInTheDocument();
    // 再撤只发没撤成的那一条，不重发已经撤过的
    expect(undoRelation.mock.calls.map(([id]) => id)).toEqual([11, 12, 13, 12]);
  });

  it("旧后台回答放宽行时写「后台还是旧版本，重启声档后再试」", async () => {
    const apiClient = looseClient({
      answerRelation: vi.fn(async () => {
        throw new ApiError("Not Found", 404, { detail: "Not Found" });
      }),
    });
    render(<Harness apiClient={apiClient} />);
    const panel = await openFileFromMeeting();
    await userEvent.click(within(panel).getByRole("button", { name: "不是这份文件" }));
    expect(await screen.findByText("后台还是旧版本，重启声档后再试")).toBeInTheDocument();
  });
});

// ------------------------------------------------------------------ 第四期 4g：问这个项目

describe("ProjectGraph 问这个项目（4g）", () => {
  const askSource = {
    id: "T1",
    kind: "meeting" as const,
    meeting_id: "b",
    title: "初审规则沟通 b",
    date: "2026-09-24",
    start_ms: 60_000,
    audio_url: null,
    text: "原话",
    quote: "原话",
  };

  function askClient() {
    return makeClient(payload(), {
      askPrepare: vi.fn(async () => ({
        plan_id: "plan-1",
        expires_in: 600,
        question: "定了什么？",
        counts: { meetings: 1, materials: 0 },
        confirm: null,
        local_model: false,
        llm: "ok",
        highlight: [],
        sources: [askSource],
        notes: [],
        unattributed_meetings: 0,
      })),
      ask: vi.fn(async () => ({ job_id: "job-1", state: "waiting", text: "在等 AI 回答" })),
      askJob: vi.fn(async () => ({
        state: "done",
        answer: { text: "定了新口径[T1]。", cited: ["T1"], found: true, no_evidence: false, truncated: false },
        sent: { meetings: 1, materials: 0 },
        sources: [{ ...askSource, sent: true }],
        notes: [],
        local_model: false,
      })),
    });
  }

  beforeEach(() => {
    forgetAskStore();
  });

  it("［问这个项目］在右侧面板的位置打开，Esc 关掉（焦点在输入框里时不关），不占 sel=", async () => {
    render(<Harness apiClient={askClient()} />);
    await userEvent.click(await screen.findByRole("button", { name: "问这个项目" }));
    const input = screen.getByRole("textbox", { name: "问题" });
    expect(screen.getByTestId("selection")).toHaveTextContent("");
    fireEvent.keyDown(input, { key: "Escape" });
    expect(screen.getByRole("textbox", { name: "问题" })).toBeInTheDocument();
    fireEvent.keyDown(screen.getByRole("heading", { name: "问这个项目" }), { key: "Escape" });
    expect(screen.queryByRole("textbox", { name: "问题" })).toBeNull();
    expect(screen.queryByText("要看的节点不在当前的图上，换个时间窗试试")).toBeNull();
  });

  it("回答显示期间出处里的会点亮、其余变暗；点节点换成那个节点的面板", async () => {
    const apiClient = askClient();
    render(<Harness apiClient={apiClient} />);
    await userEvent.click(await screen.findByRole("button", { name: "问这个项目" }));
    const input = screen.getByRole("textbox", { name: "问题" });
    fireEvent.change(input, { target: { value: "定了什么？" } });
    fireEvent.submit(input.closest("form")!);
    await screen.findByText("定了新口径", { exact: false }, { timeout: 3_000 });
    expect(apiClient.ask).toHaveBeenCalledWith("p", "plan-1", false);
    // 点亮在回答画出来以后的下一次渲染里到（出处列表的 effect 调 onHighlight），机器忙时晚一拍
    await waitFor(() => expect(screen.getByRole("button", { name: /^会议：初审规则沟通 b/ })).toHaveClass("is-lit"));
    expect(screen.getByRole("button", { name: /^会议：初审规则沟通 a/ })).toHaveClass("is-dim");
    await userEvent.click(screen.getByRole("button", { name: /^会议：初审规则沟通 a/ }));
    expect(await screen.findByRole("complementary", { name: "详情面板" })).toBeInTheDocument();
    expect(screen.queryByRole("textbox", { name: "问题" })).toBeNull();
    expect(screen.getByTestId("selection")).toHaveTextContent("m:a");
    expect(screen.getByRole("button", { name: /^会议：初审规则沟通 a/ })).not.toHaveClass("is-dim");
  });

  it("搜索页交过来的问题：打开关系图时问答面板展开、输入框填好，不自动发", async () => {
    const apiClient = askClient();
    setAskDraft("p", "报价定了多少？");
    render(<Harness apiClient={apiClient} />);
    await screen.findByRole("textbox", { name: "问题" });
    // 草稿在 ProjectAsk 的 effect 里填进去
    await waitFor(() => expect(screen.getByRole("textbox", { name: "问题" })).toHaveValue("报价定了多少？"));
    expect(screen.getByRole("button", { name: "问这个项目" })).toHaveAttribute("aria-pressed", "true");
    expect(apiClient.askPrepare).not.toHaveBeenCalled();
    expect(hasDraft("p")).toBe(false);
  });

  it("没有草稿时问答面板不展开", async () => {
    render(<Harness apiClient={askClient()} />);
    await screen.findByRole("button", { name: /^会议：初审规则沟通 a/ });
    expect(screen.queryByRole("textbox", { name: "问题" })).toBeNull();
  });

  it("没有 askPrepare 的客户端不出按钮", async () => {
    render(<Harness apiClient={makeClient()} />);
    await screen.findByRole("button", { name: /^会议：初审规则沟通 a/ });
    expect(screen.queryByRole("button", { name: "问这个项目" })).toBeNull();
  });
});

describe("ProjectGraph 文件面板的问题块（4e）", () => {
  const AFFECTS_Q: RelationQuestion = {
    relation_id: 57,
    kind: "affects",
    text: "可能过时：9/21 决议『总价下调 5%』",
    decision: {
      id: "dec-3f2a9c0b1d4e5f60",
      text: "总价下调 5%",
      date: "2026-09-21",
      meeting_id: "a",
      meeting_title: "报价沟通",
      start_ms: 754_000,
      audio_url: "/api/media/412",
    },
    passage: { loc: "第 2 页", text: "…总价在原基础上下调 3%，含税…" },
    file: { id: 7, name: "报价单v2.xlsx", folder: "商务/" },
    answers: ["updated", "no"],
  };
  const PRODUCED_Q: RelationQuestion = {
    relation_id: 61,
    kind: "produced",
    text: "会后 3 天新增在『商务/』",
    ask: "是任务『写一版方案』的交付物吗？",
    task: { id: "t-new", title: "写一版方案", status: "in_progress" },
    file: { id: 7, name: "报价单v2.xlsx", folder: "商务/" },
    words: [],
    answers: ["yes", "no"],
  };

  function questionClient(overrides: Record<string, unknown> = {}) {
    return materialClient(withFiles(), {
      getGraphFile: vi.fn(async () => fileDetail({ deliverables: [], questions: [PRODUCED_Q, AFFECTS_Q].reverse() })),
      answerRelation: vi.fn(async () => ({ relation: {}, undo_until: new Date(Date.now() + 600_000).toISOString(), deliverable_id: null })),
      undoRelation: vi.fn(async () => ({ relation: {}, removed_deliverable_id: null })),
      ...overrides,
    });
  }

  async function openFilePanel() {
    await userEvent.click(await screen.findByRole("button", { name: "文件：报价单v2.xlsx" }));
    const panel = screen.getByRole("complementary", { name: "详情面板" });
    await within(panel).findByText("报价单第一行：初审规则服务费");
    return panel;
  }

  it("问题在预览之后、「在 N 场会上被提到」之前，可能过时在前", async () => {
    render(
      <WithFlags>
        <Harness apiClient={questionClient()} />
      </WithFlags>,
    );
    const panel = await openFilePanel();
    const affects = await within(panel).findByRole("group", { name: AFFECTS_Q.text });
    const produced = within(panel).getByRole("group", { name: PRODUCED_Q.text });
    const preview = within(panel).getByText("报价单第一行：初审规则服务费");
    const mentioned = within(panel).getByRole("heading", { name: "在 2 场会上被提到" });
    expect(preview.compareDocumentPosition(affects) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(affects.compareDocumentPosition(produced) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(produced.compareDocumentPosition(mentioned) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(within(affects).getByText("第 2 页：『…总价在原基础上下调 3%，含税…』")).toBeInTheDocument();
    expect(within(produced).getByText("是任务『写一版方案』的交付物吗？")).toBeInTheDocument();
    expect(within(produced).getByText("会后 3 天新增在『商务/』")).toBeInTheDocument();
  });

  it("［已更新］发请求并出带［撤销］的提示；⌘Z 走画布的撤销栈调 undoRelation", async () => {
    const apiClient = questionClient();
    render(
      <WithFlags>
        <Harness apiClient={apiClient} />
      </WithFlags>,
    );
    const panel = await openFilePanel();
    const affects = await within(panel).findByRole("group", { name: AFFECTS_Q.text });
    await userEvent.click(within(affects).getByRole("button", { name: "已更新" }));
    expect(apiClient.answerRelation).toHaveBeenCalledWith(57, { answer: "updated" });
    const notice = await screen.findByRole("status");
    expect(notice).toHaveTextContent("已标为更新过");
    await userEvent.click(within(notice).getByRole("button", { name: "撤销" }));
    expect(apiClient.undoRelation).toHaveBeenCalledWith(57);
    expect(await screen.findByText("已撤销")).toBeInTheDocument();

    const produced = await within(screen.getByRole("complementary", { name: "详情面板" })).findByRole("group", {
      name: PRODUCED_Q.text,
    });
    await userEvent.click(within(produced).getByRole("button", { name: "是" }));
    expect(apiClient.answerRelation).toHaveBeenCalledWith(61, { answer: "yes" });
    expect(await screen.findByRole("status")).toHaveTextContent("已登记为『写一版方案』的交付物");
    fireEvent.keyDown(document.body, { key: "z", metaKey: true });
    await waitFor(() => expect(apiClient.undoRelation).toHaveBeenCalledWith(61));
  });

  it("undoRelation 回 409 时原样显示服务端那句，横幅是 role=status，不是 alert", async () => {
    const apiClient = questionClient({
      undoRelation: vi.fn(async () => {
        throw new ApiError("已超过撤销时间，请直接改回", 409, { detail: "已超过撤销时间，请直接改回" });
      }),
    });
    render(
      <WithFlags>
        <Harness apiClient={apiClient} />
      </WithFlags>,
    );
    const panel = await openFilePanel();
    await userEvent.click(within(await within(panel).findByRole("group", { name: AFFECTS_Q.text })).getByRole("button", { name: "不相关" }));
    await userEvent.click(within(await screen.findByRole("status")).getByRole("button", { name: "撤销" }));
    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("已超过撤销时间，请直接改回"));
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(apiClient.undoRelation).toHaveBeenCalledWith(57);
  });

  it("旧后台：没有 linksFlags、没有 answerRelation 或数据里没有 questions 时不画", async () => {
    const plain = render(<Harness apiClient={questionClient()} />);
    let panel = await openFilePanel();
    expect(within(panel).queryByRole("group", { name: AFFECTS_Q.text })).toBeNull();
    plain.unmount();

    forgetGraphCache();
    const noAnswer = render(
      <WithFlags>
        <Harness apiClient={questionClient({ answerRelation: undefined })} />
      </WithFlags>,
    );
    panel = await openFilePanel();
    expect(within(panel).queryByRole("group", { name: AFFECTS_Q.text })).toBeNull();
    noAnswer.unmount();

    forgetGraphCache();
    render(
      <WithFlags>
        <Harness apiClient={questionClient({ getGraphFile: vi.fn(async () => fileDetail({ deliverables: [] })) })} />
      </WithFlags>,
    );
    panel = await openFilePanel();
    expect(within(panel).queryByRole("group", { name: AFFECTS_Q.text })).toBeNull();
    expect(within(panel).queryByRole("group", { name: PRODUCED_Q.text })).toBeNull();
  });

  it("展开一场会：有 stale 的决议卡加小签「1 个文件可能过时」，点了打开第一份文件的预览抽屉", async () => {
    const onOpenPreview = vi.fn();
    const withStale = focusPayload({
      decisions: [
        { id: "dec-3f2a9c0b1d4e5f60", text: "初审规则按新口径执行", start_ms: 60_000, detail: "初审规则按新口径执行",
          later: [], earlier: [], stale: [{ relation_id: 57, file_id: 812, name: "报价单 v3.xlsx" }] },
      ],
    });
    render(
      <WithFlags>
        <Harness apiClient={focusClient({ graphMeetingFocus: vi.fn(async () => withStale) })} handlers={{ onOpenPreview }} />
      </WithFlags>,
    );
    fireEvent.doubleClick(await screen.findByRole("button", { name: /^会议：初审规则沟通 a/ }));
    const view = await screen.findByRole("application", { name: "展开的会：初审规则沟通 a" });
    await userEvent.click(await within(view).findByRole("button", { name: "决议：初审规则按新口径执行，01:00" }));
    const detail = await screen.findByRole("complementary", { name: "详情面板" });
    await userEvent.click(within(detail).getByRole("button", { name: "1 个文件可能过时" }));
    expect(onOpenPreview).toHaveBeenCalledWith(812);
  });
});

describe("ProjectGraph 第四期的线和局部图", () => {
  const AFF_LABEL = "9/21 定的『总价下调 5%』，报价单 v3 之后没改过";
  const PROD_LABEL = "会后 3 天新增在『商务/』，是任务『写一版方案』的交付物吗？";
  const AFFECTS: RelationQuestion = {
    relation_id: 57,
    kind: "affects",
    text: "可能过时：9/21 决议『总价下调 5%』",
    decision: {
      id: "dec-3f2a9c0b1d4e5f60",
      text: "总价下调 5%",
      date: "2026-09-21",
      meeting_id: "a",
      meeting_title: "报价沟通",
      start_ms: 754_000,
      audio_url: "/api/media/412",
    },
    passage: { loc: "第 2 页", text: "…总价在原基础上下调 3%，含税…" },
    file: { id: 7, name: "报价单v2.xlsx", folder: "商务/" },
    answers: ["updated", "no"],
  };
  const PRODUCED: RelationQuestion = {
    relation_id: 61,
    kind: "produced",
    text: "会后 3 天新增在『商务/』",
    ask: "是任务『写一版方案』的交付物吗？",
    task: { id: "t-new", title: "写一版方案", status: "in_progress" },
    file: { id: 7, name: "报价单v2.xlsx", folder: "商务/" },
    words: [],
    answers: ["yes", "no"],
  };

  function amberGraph(extra: Partial<GraphPayload> = {}): GraphPayload {
    const base = withFiles();
    return {
      ...base,
      requirements: [requirement("r1", { pending_tasks: 1 })],
      files: [{ ...base.files[0], stale: true, asks_deliverable: true }],
      edges: [
        ...base.edges,
        {
          id: "e:aff:57", kind: "affects", from: "m:a", to: "file:7", state: "ask", label: AFF_LABEL, relation_id: 57,
          decision_id: "dec-3f2a9c0b1d4e5f60", meeting_id: "a", at_ms: 754_000, quote: "总价下调 5%", file_id: 7,
        },
        {
          id: "e:prod:61", kind: "produced", from: "r:r1", to: "file:7", state: "ask", label: PROD_LABEL, relation_id: 61,
          relation_ids: [61], task_id: "t-new", meeting_id: "a", at_ms: 310_000, quote: "写一版方案", file_id: 7,
        },
      ],
      status: { ok: [], waiting: [{ text: "1 个文件可能过时", node_ids: ["file:7"] }], stopped: [], note: null },
      ...extra,
    };
  }

  function lineClient(graph: GraphPayload = amberGraph(), overrides: Record<string, unknown> = {}) {
    return materialClient(graph, {
      getGraphFile: vi.fn(async () => fileDetail({ deliverables: [], questions: [AFFECTS, PRODUCED] })),
      answerRelation: vi.fn(async () => ({ relation: {}, undo_until: new Date(Date.now() + 600_000).toISOString(), deliverable_id: null })),
      undoRelation: vi.fn(async () => ({ relation: {}, removed_deliverable_id: null })),
      ...overrides,
    });
  }

  it("琥珀色文件写「可能过时」，标记位画「?」；线的读屏名是线上的字", async () => {
    render(
      <WithFlags>
        <Harness apiClient={lineClient()} />
      </WithFlags>,
    );
    const node = await screen.findByRole("button", { name: "文件：报价单v2.xlsx，可能过时" });
    expect(node).toHaveTextContent("可能过时");
    expect(node.querySelector(".graph-file__mark")).toHaveTextContent("?");
    const hit = screen.getByRole("button", { name: `连线：${AFF_LABEL}` });
    expect(hit.closest(".graph-edge")).toHaveClass("graph-edge--affects", "graph-edge--ask");
    const produced = screen.getByRole("button", { name: `连线：${PROD_LABEL}` }).closest(".graph-edge")!;
    expect(produced.querySelector(".graph-edge__ask")).toHaveTextContent("?");
  });

  it("点产出线：面板标题「连线 · 产出」，这条线的问题排第一；［是］以后选中挪到新的交付物线", async () => {
    let current = amberGraph();
    const apiClient = lineClient(undefined, {
      graph: vi.fn(async () => fetched(current)),
      answerRelation: vi.fn(async () => {
        current = amberGraph({
          files: [{ ...amberGraph().files[0], stale: true, asks_deliverable: undefined }],
          edges: [
            ...amberGraph().edges.filter((edge) => edge.id !== "e:prod:61"),
            { id: "e:dlv:31", kind: "deliverable", from: "r:r1", to: "file:7", state: "ok", label: "任务『写一版方案』的交付物 · 你标的", deliverable_id: 31, task_id: "t-new", meeting_id: "a", at_ms: 310_000, quote: "写一版方案", file_id: 7 },
          ],
        });
        return { relation: {}, undo_until: new Date(Date.now() + 600_000).toISOString(), deliverable_id: 31 };
      }),
    });
    render(
      <WithFlags>
        <Harness apiClient={apiClient} />
      </WithFlags>,
    );
    await userEvent.click(await screen.findByRole("button", { name: `连线：${PROD_LABEL}` }));
    const panel = await screen.findByRole("complementary", { name: "详情面板" });
    expect(within(panel).getByText("连线 · 产出")).toBeInTheDocument();
    const produced = await within(panel).findByRole("group", { name: PRODUCED.text });
    const affects = within(panel).getByRole("group", { name: AFFECTS.text });
    expect(produced.compareDocumentPosition(affects) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    await userEvent.click(within(produced).getByRole("button", { name: "是" }));
    expect(apiClient.answerRelation).toHaveBeenCalledWith(61, { answer: "yes" });
    await waitFor(() => expect(screen.getByTestId("selection")).toHaveTextContent("e:dlv:31"));
    expect(await screen.findByText("连线 · 交付物")).toBeInTheDocument();
  });

  it("点可能过时的线：［已更新］［不相关］排第一；［已更新］发请求、提示带［撤销］，⌘Z 调 undoRelation", async () => {
    const apiClient = lineClient(undefined, {
      getGraphFile: vi.fn(async () => fileDetail({ deliverables: [], questions: [PRODUCED, AFFECTS] })),
    });
    render(
      <WithFlags>
        <Harness apiClient={apiClient} />
      </WithFlags>,
    );
    await userEvent.click(await screen.findByRole("button", { name: `连线：${AFF_LABEL}` }));
    const panel = await screen.findByRole("complementary", { name: "详情面板" });
    expect(within(panel).getByText("连线 · 可能过时")).toBeInTheDocument();
    const affects = await within(panel).findByRole("group", { name: AFFECTS.text });
    const produced = within(panel).getByRole("group", { name: PRODUCED.text });
    expect(affects.compareDocumentPosition(produced) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(within(affects).getAllByRole("button").map((button) => button.textContent)).toContain("已更新");
    await userEvent.click(within(affects).getByRole("button", { name: "已更新" }));
    expect(apiClient.answerRelation).toHaveBeenCalledWith(57, { answer: "updated" });
    const notice = await screen.findByRole("status");
    expect(within(notice).getByRole("button", { name: "撤销" })).toBeInTheDocument();
    fireEvent.keyDown(document.body, { key: "z", metaKey: true });
    await waitFor(() => expect(apiClient.undoRelation).toHaveBeenCalledWith(57));
  });

  it("［相关］默认关、不取相关线；打开后取、出点线、按项目记住；相关 404 时写旧后台那一句", async () => {
    const graphRelated = vi.fn(async () => ({
      etag: 'W/"related-1-28d"',
      related: {
        rev: 1,
        files: { "9": { file_id: 9, name: "接口文档.docx", ext: "docx", rel_path: "接口文档.docx", root_id: 1 } },
        edges: [{ id: "e:rel:5", relation_id: 5, meeting_id: "b", file_id: 9, rank: 1, words: ["报价单", "驻场"], at_ms: 1000, quote: "报价单再看一下", passage: { content_key: "k", ordinal: 0, loc: "第 1 页", text: "报价单的驻场部分" } }],
      },
    }));
    const first = render(
      <WithFlags>
        <Harness apiClient={lineClient(undefined, { graphRelated })} />
      </WithFlags>,
    );
    const related = await screen.findByRole("button", { name: "相关" });
    expect(related).toHaveAttribute("aria-pressed", "false");
    expect(related).toHaveAttribute("title", "打开后每个节点最多 3 条");
    expect(screen.getByRole("button", { name: "提到" })).toHaveAttribute("aria-pressed", "true");
    expect(graphRelated).not.toHaveBeenCalled();
    await userEvent.click(related);
    await waitFor(() => expect(graphRelated).toHaveBeenCalledWith("p", "28d", null));
    const line = await screen.findByRole("button", { name: "连线：共同词：报价单、驻场" });
    expect(line.closest(".graph-edge")).toHaveClass("graph-edge--related");
    expect(JSON.parse(window.localStorage.getItem("meeting-workbench:graph:lines.p") ?? "{}")).toEqual({ mention: true, related: true });
    // 悬停：会上、材料两处原话
    fireEvent.mouseEnter(line);
    expect(await screen.findByText("会上：『报价单再看一下』· 00:00:01")).toBeInTheDocument();
    expect(screen.getByText("材料：『报价单的驻场部分』 · 第 1 页")).toBeInTheDocument();
    first.unmount();

    forgetGraphCache();
    render(
      <WithFlags>
        <Harness
          apiClient={lineClient(undefined, {
            graphRelated: vi.fn(async () => {
              throw new ApiError("Not Found", 404, { detail: "Not Found" });
            }),
          })}
        />
      </WithFlags>,
    );
    expect(await screen.findByRole("button", { name: "相关" })).toHaveAttribute("aria-pressed", "true");
    expect(await screen.findByText("后台还是旧版本，重启声档后再试")).toBeInTheDocument();
  });

  it("［提到］关掉只藏提到线，文件节点还在；没有 linksFlags 时不出［相关］", async () => {
    const plain = render(<Harness apiClient={lineClient(undefined, { graphRelated: vi.fn() })} />);
    await screen.findByRole("button", { name: /^文件：报价单v2.xlsx/ });
    expect(screen.queryByRole("button", { name: "相关" })).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "提到" }));
    expect(screen.queryByRole("button", { name: "连线：会上说『报价单』3 次 · 00:12:34" })).toBeNull();
    expect(screen.getByRole("button", { name: /^文件：报价单v2.xlsx/ })).toBeInTheDocument();
    plain.unmount();
  });

  it("悬停讨论线出字；选中有 15 条线的节点只画 12 条，面板写「还有 3 条线没画出来」，点一行选中那条线", async () => {
    const meetings = Array.from({ length: 15 }, (_, index) => meeting(`q${String(index).padStart(2, "0")}`, index % 20));
    const edges = meetings.map((item) => ({
      id: `e:disc:r1:${item.meeting_id}`,
      kind: "discussion" as const,
      from: item.id,
      to: "r:r1",
      label: `你关联的 ${item.meeting_id}`,
      meeting_id: item.meeting_id,
      requirement_id: "r1",
    }));
    render(
      <WithFlags>
        <Harness apiClient={makeClient(payload({ meetings, edges }))} />
      </WithFlags>,
    );
    const hit = await screen.findByRole("button", { name: "连线：你关联的 q00" });
    fireEvent.mouseEnter(hit);
    expect(await screen.findByText("你关联的 q00")).toBeInTheDocument();
    fireEvent.mouseLeave(hit);
    await userEvent.click(screen.getByRole("button", { name: /^需求：需求 r1/ }));
    const panel = await screen.findByRole("complementary", { name: "详情面板" });
    expect(document.querySelectorAll(".graph-edge--discussion")).toHaveLength(12);
    const section = within(panel).getByRole("heading", { name: "还有 3 条线没画出来" }).closest("section")!;
    const rows = within(section).getAllByRole("button");
    expect(rows).toHaveLength(3);
    await userEvent.click(rows[0]);
    expect(screen.getByTestId("selection").textContent).toMatch(/^e:disc:r1:q1[234]$/);
  });

  it("［图例］点开小窗，Esc 关掉", async () => {
    render(<Harness apiClient={makeClient()} />);
    await userEvent.click(await screen.findByRole("button", { name: "图例" }));
    const dialog = screen.getByRole("dialog", { name: "图例" });
    expect(within(dialog).getByText("琥珀色虚线：在等你回答的产出和可能过时")).toBeInTheDocument();
    expect(within(dialog).getAllByRole("listitem")).toHaveLength(9);
    fireEvent.keyDown(document.body, { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "图例" })).toBeNull());
  });

  it("面板和［图例］都开着：第一次 Esc 只收图例，第二次才关面板（焦点在页脚的按钮上，不在画布也不在面板里）", async () => {
    render(<Harness apiClient={makeClient()} />);
    await userEvent.click(await screen.findByRole("button", { name: /^会议：初审规则沟通 a/ }));
    await screen.findByRole("complementary", { name: "详情面板" });
    await userEvent.click(screen.getByRole("button", { name: "图例" }));
    expect(screen.getByRole("dialog", { name: "图例" })).toBeInTheDocument();

    await userEvent.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "图例" })).toBeNull());
    expect(screen.getByRole("complementary", { name: "详情面板" })).toBeInTheDocument();

    await userEvent.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("complementary", { name: "详情面板" })).toBeNull());
  });

  it("D11：点画布空白处也关掉图例（不只是 Esc）——画布拖拽用的是 pointerdown，合成的 mousedown 不会派发", async () => {
    render(<Harness apiClient={makeClient()} />);
    await userEvent.click(await screen.findByRole("button", { name: "图例" }));
    expect(screen.getByRole("dialog", { name: "图例" })).toBeInTheDocument();
    const canvas = await screen.findByRole("application", { name: "云图AI 关系图" });
    fireEvent.pointerDown(canvas);
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "图例" })).toBeNull());
  });

  it("点状态句：在问的文件不在图上时钉上第一个并选中它；N 键在需求之后走到琥珀色文件", async () => {
    const graph = amberGraph({
      status: { ok: [], waiting: [{ text: "2 个文件可能过时", node_ids: ["file:900", "file:7"] }], stopped: [], note: null },
    });
    const apiClient = lineClient(graph, {
      getGraphFile: vi.fn(async (fileId: number) =>
        fileDetail({ deliverables: [], file: { ...fileDetail().file, id: fileId, name: fileId === 900 ? "报价单v9.xlsx" : "报价单v2.xlsx", rel_path: "x.xlsx" } }),
      ),
    });
    render(
      <WithFlags>
        <Harness apiClient={apiClient} />
      </WithFlags>,
    );
    const canvas = await screen.findByRole("application", { name: "云图AI 关系图" });
    fireEvent.keyDown(canvas, { key: "n" });
    await waitFor(() => expect(screen.getByTestId("selection")).toHaveTextContent("r:r1"));
    fireEvent.keyDown(canvas, { key: "n" });
    await waitFor(() => expect(screen.getByTestId("selection")).toHaveTextContent("file:900"));
    expect(apiClient.getGraphFile).toHaveBeenCalledWith(900);
    expect(await screen.findByRole("button", { name: "文件：报价单v9.xlsx，可能过时" })).toBeInTheDocument();
  });

  it("状态句里的文件一个都不在图上：点了钉上第一个", async () => {
    const graph = amberGraph({
      status: { ok: [], waiting: [{ text: "1 个新文件等你认交付物", node_ids: ["file:901"] }], stopped: [], note: null },
    });
    const apiClient = lineClient(graph, {
      getGraphFile: vi.fn(async () => fileDetail({ deliverables: [], file: { ...fileDetail().file, id: 901, name: "方案.key", rel_path: "方案.key" } })),
    });
    render(
      <WithFlags>
        <Harness apiClient={apiClient} />
      </WithFlags>,
    );
    await userEvent.click(await screen.findByRole("button", { name: "1 个新文件等你认交付物" }));
    await waitFor(() => expect(screen.getByTestId("selection")).toHaveTextContent("file:901"));
    expect(await screen.findByRole("button", { name: "文件：方案.key，等你认交付物" })).toBeInTheDocument();
  });

  const MAP = {
    center: { id: "file:7", kind: "file" as const, file_id: 7, name: "报价单v2.xlsx", ext: "xlsx", at: "2026-09-18T10:00:00+08:00", gone: false, copies: [], folder: "商务" },
    nodes: [
      { id: "m:a", kind: "meeting" as const, title: "初审规则沟通 a", at: "2026-09-21T10:00:00+08:00", audio_url: "/api/media/1", caption: "9/21 初审规则沟通 a" },
      { id: "dec:dec-1", kind: "decision" as const, text: "总价下调 5%", meeting_id: "a", meeting_caption: "9/21 初审规则沟通 a", start_ms: 754_000, at: "2026-09-21T10:12:34+08:00", audio_url: "/api/media/1" },
    ],
    edges: [
      { id: "e:file:7:a", kind: "mentioned" as const, from: "m:a", to: "file:7", label: "会上说『报价单』3 次 · 00:12:34", meeting_id: "a", at_ms: 754_000, quote: "报价单发给甲方" },
      { id: "e:aff:57", kind: "affects" as const, from: "dec:dec-1", to: "file:7", state: "ask", label: AFF_LABEL, relation_id: 57 },
      { id: "e:in:dec-1", kind: "in_meeting" as const, from: "m:a", to: "dec:dec-1", label: "" },
    ],
    hidden: [{ edge_id: "e:file:7:m-40", label: "会上说『报价单』2 次", node_id: "m:m-40", node_label: "7/30 周会" }],
    hidden_count: 1,
  };

  it("［以它为中心看］进局部图舞台：标题、节点、「还有 N 个没画出来」；［回到关系图］回来", async () => {
    const apiClient = lineClient(undefined, {
      graphFileMap: vi.fn(async () => MAP),
      graphTrace: vi.fn(async () => ({})),
    });
    render(
      <WithFlags>
        <Harness apiClient={apiClient} />
      </WithFlags>,
    );
    await userEvent.click(await screen.findByRole("button", { name: /^文件：报价单v2.xlsx/ }));
    const panel = await screen.findByRole("complementary", { name: "详情面板" });
    await userEvent.click(await within(panel).findByRole("button", { name: "以它为中心看" }));
    expect(await screen.findByRole("heading", { name: "以『报价单v2.xlsx』为中心" })).toBeInTheDocument();
    expect(apiClient.graphFileMap).toHaveBeenCalledWith(7, { related: false });
    expect(screen.getByRole("button", { name: "决议：总价下调 5%" })).toBeInTheDocument();
    expect(await screen.findByText("7/30 周会 · 会上说『报价单』2 次")).toBeInTheDocument();
    // 局部图里文件面板没有［以它为中心看］（它就是中心），有［来龙去脉］
    const localPanel = screen.getByRole("complementary", { name: "详情面板" });
    expect(await within(localPanel).findByRole("button", { name: "来龙去脉" })).toBeInTheDocument();
    expect(within(localPanel).queryByRole("button", { name: "以它为中心看" })).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "决议：总价下调 5%" }));
    expect(await within(screen.getByRole("complementary", { name: "详情面板" })).findByText("9/21 初审规则沟通 a 定的")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "回到关系图" }));
    expect(await screen.findByRole("application", { name: "云图AI 关系图" })).toBeInTheDocument();
  });

  it("局部图里选中一个节点：焦点落在页面上时 Esc 收掉这个节点的详情，回到中心那份文件的面板，局部图还在", async () => {
    const apiClient = lineClient(undefined, {
      graphFileMap: vi.fn(async () => MAP),
      graphTrace: vi.fn(async () => ({})),
    });
    render(
      <WithFlags>
        <Harness apiClient={apiClient} />
      </WithFlags>,
    );
    await userEvent.click(await screen.findByRole("button", { name: /^文件：报价单v2.xlsx/ }));
    const panel = await screen.findByRole("complementary", { name: "详情面板" });
    await userEvent.click(await within(panel).findByRole("button", { name: "以它为中心看" }));
    await screen.findByRole("heading", { name: "以『报价单v2.xlsx』为中心" });
    await userEvent.click(await screen.findByRole("button", { name: "决议：总价下调 5%" }));
    const localPanel = await screen.findByRole("complementary", { name: "详情面板" });
    await userEvent.click(await within(localPanel).findByText("9/21 初审规则沟通 a 定的"));
    expect(document.activeElement).toBe(document.body);

    await userEvent.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByText("9/21 初审规则沟通 a 定的")).toBeNull());
    expect(await within(screen.getByRole("complementary", { name: "详情面板" })).findByRole("button", { name: "来龙去脉" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "以『报价单v2.xlsx』为中心" })).toBeInTheDocument();
  });

  it("挪过位置：换成新 id（替换，不压历史），写「这份文件挪到了『2026』文件夹里」", async () => {
    const onLocalChange = vi.fn();
    const moved = { ...MAP, center: { ...MAP.center, id: "file:8", file_id: 8, moved_from: 7, folder: "2026" } };
    const apiClient = lineClient(undefined, { graphFileMap: vi.fn(async () => moved), graphTrace: vi.fn() });
    function LocalHarness() {
      const [local, setLocal] = useState<{ kind: "file"; fileId: number } | null>({ kind: "file", fileId: 7 });
      return (
        <ProjectGraph
          apiClient={apiClient}
          local={local}
          onBack={() => {}}
          onLocalChange={(next, options) => {
            onLocalChange(next, options);
            setLocal(next as { kind: "file"; fileId: number } | null);
          }}
          onOpenGlossary={() => {}}
          onOpenMeeting={() => {}}
          onOpenProject={() => {}}
          onOpenRequirement={() => {}}
          onSelectionChange={() => {}}
          projectId="p"
          projects={PROJECTS}
          selection={null}
        />
      );
    }
    render(
      <WithFlags>
        <LocalHarness />
      </WithFlags>,
    );
    await waitFor(() => expect(onLocalChange).toHaveBeenCalledWith({ kind: "file", fileId: 8 }, { replace: true }));
    expect(await screen.findByText("这份文件挪到了『2026』文件夹里")).toBeInTheDocument();
  });

  it("［来龙去脉］画链，到了上限写那一句；旧后台（404 Not Found）写旧后台那一句", async () => {
    const trace = {
      center: { id: "file:7", kind: "file" as const, file_id: 7, name: "报价单v2.xlsx", at: "2026-09-18T10:00:00+08:00" },
      nodes: [
        { id: "task:t-19", kind: "task" as const, title: "写一版方案", status: "in_progress", meeting_id: "a", at: "2026-09-14T10:05:00+08:00" },
        { id: "m:a", kind: "meeting" as const, title: "初审规则沟通 a", at: "2026-09-21T10:00:00+08:00", audio_url: "/api/media/1" },
      ],
      edges: [
        { id: "e:dlv:31", kind: "deliverable" as const, from: "task:t-19", to: "file:7", label: "任务『写一版方案』的交付物 · 你标的", on_chain: true },
        { id: "e:file:7:a", kind: "mentioned" as const, from: "m:a", to: "file:7", label: "会上说『报价单』· 00:12:34", at_ms: 754_000, on_chain: true },
      ],
      chain: ["task:t-19", "file:7", "m:a"],
      center_index: 1,
      cut: { back: false, forward: true },
    };
    const apiClient = lineClient(undefined, { graphFileMap: vi.fn(async () => MAP), graphTrace: vi.fn(async () => trace) });
    const first = render(
      <WithFlags>
        <Harness apiClient={apiClient} />
      </WithFlags>,
    );
    await userEvent.click(await screen.findByRole("button", { name: /^文件：报价单v2.xlsx/ }));
    const panel = await screen.findByRole("complementary", { name: "详情面板" });
    await userEvent.click(await within(panel).findByRole("button", { name: "来龙去脉" }));
    expect(await screen.findByRole("heading", { name: "『报价单v2.xlsx』的来龙去脉" })).toBeInTheDocument();
    expect(apiClient.graphTrace).toHaveBeenCalledWith("file:7");
    expect(screen.getByText("往后走到 3 步为止，更晚的没展开")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "任务：写一版方案" })).toBeInTheDocument();
    expect(document.querySelectorAll(".local-edge--chain")).toHaveLength(2);
    first.unmount();

    forgetGraphCache();
    const old = lineClient(undefined, {
      graphFileMap: vi.fn(async () => {
        throw new ApiError("Not Found", 404, { detail: "Not Found" });
      }),
      graphTrace: vi.fn(),
    });
    render(
      <WithFlags>
        <Harness apiClient={old} />
      </WithFlags>,
    );
    fireEvent.doubleClick(await screen.findByRole("button", { name: /^文件：报价单v2.xlsx/ }));
    expect(await screen.findByText("后台还是旧版本，重启声档后再试")).toBeInTheDocument();
  });

  it("悬停可能过时的线：卡片第一行线上的字，下面一行原话和时刻", async () => {
    render(
      <WithFlags>
        <Harness apiClient={lineClient()} />
      </WithFlags>,
    );
    fireEvent.mouseEnter(await screen.findByRole("button", { name: `连线：${AFF_LABEL}` }));
    expect(await screen.findByText("『总价下调 5%』· 00:12:34")).toBeInTheDocument();
  });

  it("相关线的面板：会上、材料两处原话，［不相关］以后选中挪到那份文件并钉住", async () => {
    let relatedEdges = [
      { id: "e:rel:5", relation_id: 5, meeting_id: "a", file_id: 9, rank: 1, words: ["报价单", "驻场"], at_ms: 1000, quote: "报价单再看一下", passage: { content_key: "k", ordinal: 0, loc: "第 1 页", text: "报价单的驻场部分" } },
    ];
    const graphRelated = vi.fn(async () => ({
      etag: `W/"related-${relatedEdges.length}"`,
      related: {
        rev: relatedEdges.length,
        files: relatedEdges.length ? { "9": { file_id: 9, name: "接口文档.docx", ext: "docx", rel_path: "接口文档.docx", root_id: 1 } } : {},
        edges: relatedEdges,
      },
    }));
    const apiClient = lineClient(undefined, {
      // 标「不相关」只动相关自己的版本号、不动图的：后端对着带 etag 的重取回 304，图对象不换，相关线要靠刷新自己再对一次
      graph: vi.fn(async (_project: string, _window?: GraphWindow, _focus?: string, etag?: string | null) =>
        etag === 'W/"g-amber"' ? fetched(null, etag) : fetched(amberGraph(), 'W/"g-amber"'),
      ),
      graphRelated,
      getGraphFile: vi.fn(async () => fileDetail({ deliverables: [], file: { ...fileDetail().file, id: 9, name: "接口文档.docx", rel_path: "接口文档.docx" } })),
      answerRelation: vi.fn(async () => {
        relatedEdges = [];
        return { relation: {}, undo_until: new Date(Date.now() + 600_000).toISOString(), deliverable_id: null };
      }),
    });
    render(
      <WithFlags>
        <Harness apiClient={apiClient} />
      </WithFlags>,
    );
    await userEvent.click(await screen.findByRole("button", { name: "相关" }));
    await userEvent.click(await screen.findByRole("button", { name: "连线：共同词：报价单、驻场" }));
    const panel = await screen.findByRole("complementary", { name: "详情面板" });
    expect(within(panel).getByText("连线 · 相关")).toBeInTheDocument();
    expect(within(panel).getByRole("button", { name: "从 00:00:01 播放" }).closest("p")).toHaveTextContent("『报价单再看一下』");
    expect(within(panel).getByRole("button", { name: "材料：第 1 页" }).closest("p")).toHaveTextContent("『报价单的驻场部分』");
    await userEvent.click(within(panel).getByRole("button", { name: "不相关" }));
    expect(apiClient.answerRelation).toHaveBeenCalledWith(5, { answer: "no" });
    await waitFor(() => expect(screen.getByTestId("selection")).toHaveTextContent("file:9"));
    await waitFor(() => expect(graphRelated.mock.calls.length).toBeGreaterThan(1));
    // 相关线收回以后那份文件不在相关里了，仍钉在图上、面板还在
    expect(await screen.findByRole("button", { name: /^文件：接口文档.docx/ })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "连线：共同词：报价单、驻场" })).toBeNull();
  });

  it("回答以后用户换了选中：新数据到了也不再把选中挪到交付物线", async () => {
    let current = amberGraph();
    let hold = false;
    const pending: Array<() => void> = [];
    const apiClient = lineClient(undefined, {
      graph: vi.fn(() =>
        hold ? new Promise((resolve) => pending.push(() => resolve(fetched(current)))) : Promise.resolve(fetched(current)),
      ),
      answerRelation: vi.fn(async () => {
        current = amberGraph({
          edges: [
            ...amberGraph().edges.filter((edge) => edge.id !== "e:prod:61"),
            { id: "e:dlv:31", kind: "deliverable", from: "r:r1", to: "file:7", state: "ok", label: "任务『写一版方案』的交付物 · 你标的", deliverable_id: 31, task_id: "t-new", meeting_id: "a", at_ms: 310_000, quote: "写一版方案", file_id: 7 },
          ],
        });
        hold = true;
        return { relation: {}, undo_until: new Date(Date.now() + 600_000).toISOString(), deliverable_id: 31 };
      }),
    });
    render(
      <WithFlags>
        <Harness apiClient={apiClient} />
      </WithFlags>,
    );
    await userEvent.click(await screen.findByRole("button", { name: `连线：${PROD_LABEL}` }));
    const panel = await screen.findByRole("complementary", { name: "详情面板" });
    const produced = await within(panel).findByRole("group", { name: PRODUCED.text });
    await userEvent.click(within(produced).getByRole("button", { name: "是" }));
    expect(apiClient.answerRelation).toHaveBeenCalledWith(61, { answer: "yes" });
    await waitFor(() => expect(pending.length).toBeGreaterThan(0));
    // 重取还没回来，用户点了一场会
    await userEvent.click(screen.getByRole("button", { name: /^会议：初审规则沟通 a/ }));
    expect(screen.getByTestId("selection")).toHaveTextContent("m:a");
    hold = false;
    act(() => pending.splice(0).forEach((resolve) => resolve()));
    await waitFor(() => expect(screen.queryByRole("button", { name: `连线：${PROD_LABEL}` })).toBeNull());
    expect(screen.getByTestId("selection")).toHaveTextContent("m:a");
  });

  it("局部图里在线上回答、⌘Z 撤销：舞台都整张重取；会议到决议的线读屏名是「这场会定的」", async () => {
    const graphFileMap = vi.fn(async () => MAP);
    const apiClient = lineClient(undefined, { graphFileMap, graphTrace: vi.fn() });
    render(
      <WithFlags>
        <Harness apiClient={apiClient} />
      </WithFlags>,
    );
    fireEvent.doubleClick(await screen.findByRole("button", { name: /^文件：报价单v2.xlsx/ }));
    expect(await screen.findByRole("heading", { name: "以『报价单v2.xlsx』为中心" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "连线：这场会定的" })).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: `连线：${AFF_LABEL}` }));
    const panel = await screen.findByRole("complementary", { name: "详情面板" });
    const affects = await within(panel).findByRole("group", { name: AFFECTS.text });
    await userEvent.click(within(affects).getByRole("button", { name: "已更新" }));
    await waitFor(() => expect(graphFileMap.mock.calls.length).toBeGreaterThan(1));
    // 回答以后的重取都落定了再撤销
    await act(() => new Promise((resolve) => setTimeout(resolve, 50)));
    const before = graphFileMap.mock.calls.length;
    fireEvent.keyDown(document.body, { key: "z", metaKey: true });
    await waitFor(() => expect(apiClient.undoRelation).toHaveBeenCalledWith(57));
    await waitFor(() => expect(graphFileMap).toHaveBeenCalledTimes(before + 1));
  });

  it("「还有 N 个没画出来」：会议打开它的面板，文件以它为中心，旧后台的决议行走它的来龙去脉", async () => {
    const withHidden = {
      ...MAP,
      hidden: [
        {
          edge_id: "e:file:7:m-40",
          label: "会上说『报价单』2 次",
          node_id: "m:m-40",
          node_label: "7/30 周会",
          node: { id: "m:m-40", kind: "meeting" as const, meeting_id: "m-40", title: "周会", caption: "7/30 周会", at: "2026-07-30T10:00:00+08:00", audio_url: "/api/media/40" },
          edge: { id: "e:file:7:m-40", kind: "mentioned" as const, from: "m:m-40", to: "file:7", label: "会上说『报价单』2 次", meeting_id: "m-40", at_ms: 60_000, quote: "报价单再核一下" },
        },
        { edge_id: "e:aff:99", label: "7/1 定的『先按旧价』", node_id: "dec:dec-9", node_label: "决议『先按旧价』" },
        { edge_id: "e:same:12:7", label: "同属『报价单』", node_id: "file:12", node_label: "报价单 v1.xlsx",
          node: { id: "file:12", kind: "file" as const, file_id: 12, name: "报价单 v1.xlsx", at: "2026-07-01T10:00:00+08:00" } },
      ],
      hidden_count: 3,
    };
    const onOpenMeeting = vi.fn();
    const graphFileMap = vi.fn(async () => withHidden);
    const graphTrace = vi.fn(async () => ({
      center: { id: "dec:dec-9", kind: "decision" as const, text: "先按旧价", at: "2026-07-01T10:00:00+08:00" },
      nodes: [], edges: [], chain: ["dec:dec-9"], center_index: 0, cut: { back: false, forward: false },
    }));
    render(
      <WithFlags>
        <Harness apiClient={lineClient(undefined, { graphFileMap, graphTrace })} handlers={{ onOpenMeeting }} />
      </WithFlags>,
    );
    fireEvent.doubleClick(await screen.findByRole("button", { name: /^文件：报价单v2.xlsx/ }));
    await userEvent.click(await screen.findByRole("button", { name: "7/30 周会 · 会上说『报价单』2 次" }));
    const panel = screen.getByRole("complementary", { name: "详情面板" });
    expect(within(panel).getByRole("heading", { name: "周会" })).toBeInTheDocument();
    expect(within(panel).getByText("会上说『报价单』2 次")).toBeInTheDocument();
    expect(within(panel).getByRole("button", { name: "打开会议页 →" })).toBeInTheDocument();
    expect(onOpenMeeting).not.toHaveBeenCalled();
    expect(screen.getByRole("heading", { name: "以『报价单v2.xlsx』为中心" })).toBeInTheDocument();

    await userEvent.click(within(panel).getByRole("button", { name: "← 回到中心" }));
    await userEvent.click(await screen.findByRole("button", { name: "报价单 v1.xlsx · 同属『报价单』" }));
    await waitFor(() => expect(graphFileMap).toHaveBeenLastCalledWith(12, { related: false }));

    await userEvent.click(await screen.findByRole("button", { name: "决议『先按旧价』 · 7/1 定的『先按旧价』" }));
    await waitFor(() => expect(graphTrace).toHaveBeenCalledWith("dec:dec-9"));
  });

  it("状态只写一句：找不到活文件时不再另写「还没有会提到」；挪位置换 id 时舞台留着，不先闪「正在取」", async () => {
    const lonely = { ...MAP, center: { ...MAP.center, gone: true }, nodes: [], edges: [], hidden: [], hidden_count: 0 };
    const first = render(
      <WithFlags>
        <Harness apiClient={lineClient(undefined, { graphFileMap: vi.fn(async () => lonely), graphTrace: vi.fn() })} />
      </WithFlags>,
    );
    fireEvent.doubleClick(await screen.findByRole("button", { name: /^文件：报价单v2.xlsx/ }));
    expect(await screen.findByText("这份文件已经不在资料盘里了，下面是它还在时的关系")).toBeInTheDocument();
    expect(screen.queryByText("还没有会提到这份文件，也没有任务或决议连到它")).toBeNull();
    expect(screen.getAllByRole("status").filter((node) => node.classList.contains("local-stage__note"))).toHaveLength(1);
    first.unmount();

    forgetGraphCache();
    const moved = { ...MAP, center: { ...MAP.center, id: "file:8", file_id: 8, moved_from: 7, folder: "2026" } };
    // 新 id 那次重取一直不回来：舞台仍是挪过位置那份数据
    const graphFileMap = vi.fn((fileId: number) => (fileId === 7 ? Promise.resolve(moved) : new Promise<never>(() => {})));
    render(
      <WithFlags>
        <Harness apiClient={lineClient(undefined, { graphFileMap, graphTrace: vi.fn() })} />
      </WithFlags>,
    );
    fireEvent.doubleClick(await screen.findByRole("button", { name: /^文件：报价单v2.xlsx/ }));
    expect(await screen.findByText("这份文件挪到了『2026』文件夹里")).toBeInTheDocument();
    await waitFor(() => expect(graphFileMap).toHaveBeenCalledWith(8, { related: false }));
    expect(screen.queryByText("正在取这份文件的关系")).toBeNull();
    expect(screen.getByRole("button", { name: "决议：总价下调 5%" })).toBeInTheDocument();
  });

  it("旧后台：没有 linksFlags 或客户端没有 graphFileMap 时不出［以它为中心看］［来龙去脉］", async () => {
    render(<Harness apiClient={lineClient(undefined, { graphFileMap: vi.fn(), graphTrace: vi.fn() })} />);
    await userEvent.click(await screen.findByRole("button", { name: /^文件：报价单v2.xlsx/ }));
    const panel = await screen.findByRole("complementary", { name: "详情面板" });
    await within(panel).findByText("报价单第一行：初审规则服务费");
    expect(within(panel).queryByRole("button", { name: "以它为中心看" })).toBeNull();
    expect(within(panel).queryByRole("button", { name: "来龙去脉" })).toBeNull();
  });
});
