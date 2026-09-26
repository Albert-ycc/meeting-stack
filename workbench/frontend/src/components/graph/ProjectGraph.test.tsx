import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { ApiClient } from "../../api";
import type { Project } from "../../types";
import type { GraphPayload, GraphRootsPayload, MeetingBrief } from "./graphTypes";
import { ProjectGraph, forgetGraphCache } from "./ProjectGraph";
import { day, focusPayload, focusTask, meeting, payload, requirement } from "./testFixtures";

const PROJECTS: Project[] = [
  { id: "p", name: "云图AI", color: "#2c8d83" },
  { id: "q", name: "数据中台", color: "#7a5af8" },
];

const ROOTS: GraphRootsPayload = { roots: [], folders: [], loose: { count: 0, recent: [] }, checking: false };

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
    files_note: "会上提到的文件要等材料建了索引才会出现",
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

function makeClient(graph: GraphPayload = payload(), overrides: Record<string, unknown> = {}) {
  const undoUntil = new Date(Date.now() + 10 * 60_000).toISOString();
  return {
    graph: vi.fn(async () => graph),
    graphRoots: vi.fn(async () => ROOTS),
    meetingBrief: vi.fn(async (meetingId: string) => brief(meetingId)),
    updateMeeting: vi.fn(async () => ({ effects: { tasks_moved: 1, tasks_left: [], undo_until: undoUntil } })),
    undoMeetingProject: vi.fn(async () => ({ effects: { tasks_restored: 1 } })),
    projectMaterialSubfolders: vi.fn(async () => ({ roots: [] })),
    addRequirementMeeting: vi.fn(async () => ({})),
    removeRequirementMeeting: vi.fn(async () => ({})),
    ...overrides,
  } as unknown as ApiClient & Record<string, ReturnType<typeof vi.fn>>;
}

function Harness({ apiClient, initial = null }: { apiClient: ApiClient; initial?: string | null }) {
  const [selection, setSelection] = useState<string | null>(initial);
  return (
    <>
      <ProjectGraph
        apiClient={apiClient}
        focus={initial}
        onBack={() => {}}
        onOpenGlossary={() => {}}
        onOpenMeeting={() => {}}
        onOpenProject={() => {}}
        onOpenRequirement={() => {}}
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

  it("门口的［归这里］调接口、弹带撤销的提示，撤销也走接口", async () => {
    const apiClient = makeClient(withDoorstep());
    render(<Harness apiClient={apiClient} />);
    const doorstep = await screen.findByRole("group", { name: /可能是这个项目的会：门口的会/ });
    await userEvent.click(within(doorstep).getByRole("button", { name: "归这里" }));

    expect(apiClient.updateMeeting).toHaveBeenCalledWith("door0", { project_id: "p" });
    const notice = await screen.findByRole("status");
    expect(notice).toHaveTextContent("已归到 云图AI；1 条任务一起移过去");
    await waitFor(() => expect(apiClient.graph).toHaveBeenCalledTimes(2));

    await userEvent.click(within(notice).getByRole("button", { name: "撤销" }));
    expect(apiClient.undoMeetingProject).toHaveBeenCalledWith("door0");
    expect(await screen.findByText("已撤销刚才的改动")).toBeInTheDocument();
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
    expect(apiClient.graph).toHaveBeenCalledWith("p", undefined, undefined);
    await userEvent.click(screen.getByRole("button", { name: "7 天" }));
    await waitFor(() => expect(apiClient.graph).toHaveBeenLastCalledWith("p", "7d", undefined));
    expect(window.localStorage.getItem("meeting-workbench:graph:window.p")).toBe("7d");
  });

  it("自动放宽时间窗时写明原因；深链目标不在图上时说一声并清掉选中", async () => {
    const graph = payload({
      window: { requested: null, effective: "90d", days: 90, widened_reason: "自动放宽到 90 天：28 天内只有 1 场会" },
    });
    const apiClient = makeClient(graph);
    render(<Harness apiClient={apiClient} initial="r:gone" />);
    expect(await screen.findByText("自动放宽到 90 天：28 天内只有 1 场会")).toBeInTheDocument();
    expect(apiClient.graph).toHaveBeenCalledWith("p", undefined, "r:gone");
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

  it("关系图读不出来时给出原因和重试", async () => {
    const graph = vi.fn().mockRejectedValueOnce(new Error("项目不存在")).mockResolvedValue(payload());
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
    expect(await screen.findByText(/^已改到 数据中台；1 条任务一起移过去/)).toBeInTheDocument();
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
