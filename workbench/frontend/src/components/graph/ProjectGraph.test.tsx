import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { ApiClient } from "../../api";
import type { Project } from "../../types";
import type { GraphPayload, GraphRootsPayload, MeetingBrief } from "./graphTypes";
import { ProjectGraph, forgetGraphCache } from "./ProjectGraph";
import { day, payload } from "./testFixtures";

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
