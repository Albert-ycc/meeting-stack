import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { ApiError, type ApiClient } from "../api";
import type { MeetingAttribution, Project } from "../types";
import { AttributionBar } from "./AttributionBar";

const PROJECTS: Project[] = [
  { id: "p-a", name: "云图AI", color: "#2c8d83" },
  { id: "p-b", name: "数据中台", color: "#5090ff" },
  { id: "p-c", name: "智慧园区", color: "#aa66cc" },
];

function attribution(overrides: Partial<MeetingAttribution> = {}): MeetingAttribution {
  return {
    state: "auto",
    project_id: "p-a",
    origin: "ai",
    method: "llm_high",
    evidence: [],
    candidates: [],
    reason: "",
    new_project_name: null,
    reassigned_from: null,
    ai_configured: true,
    ...overrides,
  };
}

function detail(projectId: string | null, next: Partial<MeetingAttribution>, extra: Record<string, unknown> = {}) {
  const project = PROJECTS.find((item) => item.id === projectId);
  return {
    id: "m-1",
    project_id: projectId,
    project_name: project?.name ?? null,
    project_color: project?.color ?? null,
    project_origin: projectId ? "manual" : null,
    attribution: attribution({ project_id: projectId, ...next }),
    ...extra,
  };
}

function setup(value: MeetingAttribution, client: Partial<ApiClient> = {}, props: { lockedReason?: string } = {}) {
  const onChange = vi.fn();
  const onNotice = vi.fn();
  const onSeek = vi.fn();
  const onProjectsChanged = vi.fn();
  render(
    <AttributionBar
      apiClient={client as unknown as ApiClient}
      attribution={value}
      lockedReason={props.lockedReason}
      meetingId="m-1"
      onChange={onChange}
      onNotice={onNotice}
      onProjectsChanged={onProjectsChanged}
      onSeek={onSeek}
      projects={PROJECTS}
    />,
  );
  return { onChange, onNotice, onSeek, onProjectsChanged };
}

describe("AttributionBar 自动归属", () => {
  const value = attribution({
    evidence: [
      {
        kind: "literal",
        project_id: "p-a",
        project_name: "云图AI",
        cue: "初审规则",
        source: "term",
        count: 6,
        anchors_ms: [12_000, 75_000],
        where: { title: 0, transcript: 5, minutes: 1 },
      },
      { kind: "llm", project_id: "p-a", confidence: "high", reason: "一直在讨论云图的初审规则" },
    ],
  });

  it("点线索展开原话位置，点时间跳到播放器，末尾是 AI 的理由", async () => {
    const { onSeek } = setup(value);

    expect(screen.getByText("云图AI")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "提到「初审规则」6 次" }));
    expect(screen.getByText("逐字稿 5 次 · 纪要 1 次")).toBeInTheDocument();
    expect(screen.getByText("AI：一直在讨论云图的初审规则")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "跳到 01:15" }));
    expect(onSeek).toHaveBeenCalledWith(75_000);
  });

  it("点［对的］确认归属，只更新归属对象", async () => {
    const confirmed = attribution({ state: "manual", origin: "manual" });
    const confirmMeetingProject = vi.fn().mockResolvedValue(confirmed);
    const { onChange, onNotice } = setup(value, { confirmMeetingProject });

    await userEvent.click(screen.getByRole("button", { name: "对的" }));

    expect(confirmMeetingProject).toHaveBeenCalledWith("m-1");
    expect(onChange).toHaveBeenCalledWith({ attribution: confirmed });
    expect(onNotice).toHaveBeenCalledWith("已确认归到 云图AI");
  });

  it("点［改］再选别的项目，提示带撤销时限", async () => {
    const updateMeeting = vi.fn().mockResolvedValue(
      detail("p-b", { state: "manual", origin: "manual" }, {
        effects: { tasks_moved: 3, tasks_left: [], undo_until: "2099-01-01T00:00:00+00:00" },
      }),
    );
    const { onChange, onNotice, onProjectsChanged } = setup(value, { updateMeeting });

    await userEvent.click(screen.getByRole("button", { name: "改" }));
    await userEvent.selectOptions(screen.getByRole("combobox", { name: "选别的" }), "p-b");

    expect(updateMeeting).toHaveBeenCalledWith("m-1", { project_id: "p-b" });
    expect(onChange.mock.calls[0][0].project).toEqual({
      id: "p-b",
      name: "数据中台",
      color: "#5090ff",
      origin: "manual",
    });
    expect(onNotice).toHaveBeenCalledWith(
      "已改到 数据中台：3 条任务一起移过去",
      "2099-01-01T00:00:00+00:00",
    );
    expect(onProjectsChanged).toHaveBeenCalled();
  });

  it("检查器有没保存的项目改动时按钮都置灰并说明", () => {
    setup(value, {}, { lockedReason: "右侧有未保存的归属修改" });

    expect(screen.getByRole("button", { name: "对的" })).toBeDisabled();
    expect(screen.getByText("右侧有未保存的归属修改")).toBeInTheDocument();
  });
});

describe("AttributionBar 待你选", () => {
  it("两个候选直接点选，原来的那个走确认", async () => {
    const updateMeeting = vi.fn().mockResolvedValue(detail("p-b", { state: "manual", origin: "manual" }));
    const confirmMeetingProject = vi.fn().mockResolvedValue(attribution({ state: "manual" }));
    setup(
      attribution({
        state: "needs_review",
        candidates: [
          { project_id: "p-a", project_name: "云图AI", count: 1, llm: false, current: true },
          { project_id: "p-b", project_name: "数据中台", count: 3, llm: true, current: false },
        ],
      }),
      { updateMeeting, confirmMeetingProject },
    );

    expect(screen.getByText("云图AI 还是 数据中台？")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "数据中台" }));
    expect(updateMeeting).toHaveBeenCalledWith("m-1", { project_id: "p-b" });
    await userEvent.click(screen.getByRole("button", { name: "云图AI（原来的）" }));
    expect(confirmMeetingProject).toHaveBeenCalledWith("m-1");
  });

  it("［不归项目］发空字符串", async () => {
    const updateMeeting = vi.fn().mockResolvedValue(detail(null, { state: "manual_none", origin: "manual" }));
    const { onNotice } = setup(
      attribution({
        state: "needs_review",
        project_id: null,
        candidates: [{ project_id: "p-a", project_name: "云图AI", count: 2, llm: true, current: false }],
      }),
      { updateMeeting },
    );

    expect(screen.getByText("像是 云图AI？")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "不归项目" }));

    expect(updateMeeting).toHaveBeenCalledWith("m-1", { project_id: "" });
    expect(onNotice).toHaveBeenCalledWith("已标为不归项目", undefined);
  });
});

describe("AttributionBar 等待与像新项目", () => {
  it("没配 AI 时直接请你选项目", async () => {
    const updateMeeting = vi.fn().mockResolvedValue(detail("p-c", { state: "manual" }));
    setup(attribution({ state: "ai_pending", project_id: null, origin: null, ai_configured: false }), {
      updateMeeting,
    });

    expect(screen.getByText("没配置 AI，请在这里选项目")).toBeInTheDocument();
    await userEvent.selectOptions(screen.getByRole("combobox", { name: "选项目" }), "p-c");
    expect(updateMeeting).toHaveBeenCalledWith("m-1", { project_id: "p-c" });
  });

  it("建成项目撞上近似重名时问是不是它，［用它］就归过去", async () => {
    const createProjectWith = vi.fn().mockRejectedValue(
      new ApiError("已有「云图AI」，是不是它？", 409, {
        detail: "已有「云图AI」，是不是它？",
        suggestion: { project_id: "p-a", name: "云图AI", also_names: ["云图"], matched: "云图", match: "same" },
      }),
    );
    const updateMeeting = vi.fn().mockResolvedValue(detail("p-a", { state: "manual" }));
    setup(attribution({ state: "new_project", project_id: null, origin: null, new_project_name: "云图" }), {
      createProjectWith,
      updateMeeting,
    });

    expect(screen.getByText("像是一个新项目「云图」")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "建成项目" }));
    expect(createProjectWith).toHaveBeenCalledWith({
      name: "云图",
      color: "#667085",
      meeting_ids: ["m-1"],
      force: false,
    });
    expect(screen.getByText("已有「云图AI」（又称 云图），是不是它？")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "用它" }));
    expect(updateMeeting).toHaveBeenCalledWith("m-1", { project_id: "p-a" });
  });

  it("［仍然新建］带 force，建好后读回会议", async () => {
    const createProjectWith = vi
      .fn()
      .mockRejectedValueOnce(
        new ApiError("x", 409, {
          suggestion: { project_id: "p-a", name: "云图AI", also_names: [], matched: "云图AI", match: "similar" },
        }),
      )
      .mockResolvedValueOnce({ id: "p-new", name: "云图", color: "#667085" });
    const meeting = vi.fn().mockResolvedValue(detail("p-a", { state: "manual" }));
    const { onNotice, onProjectsChanged } = setup(
      attribution({ state: "new_project", project_id: null, origin: null, new_project_name: "云图" }),
      { createProjectWith, meeting },
    );

    await userEvent.click(screen.getByRole("button", { name: "建成项目" }));
    await userEvent.click(screen.getByRole("button", { name: "仍然新建" }));

    expect(createProjectWith).toHaveBeenLastCalledWith(expect.objectContaining({ force: true }));
    expect(meeting).toHaveBeenCalledWith("m-1");
    expect(onNotice).toHaveBeenCalledWith("已建成项目「云图」，这场会归进去了");
    expect(onProjectsChanged).toHaveBeenCalled();
  });

  it("［不是新项目］记下名字，归属变成没认出", async () => {
    const ignoreProjectName = vi.fn().mockResolvedValue({ name: "内部分享", meetings_updated: 2 });
    const value = attribution({ state: "new_project", project_id: null, origin: null, new_project_name: "内部分享" });
    const { onChange } = setup(value, { ignoreProjectName });

    await userEvent.click(screen.getByRole("button", { name: "不是新项目" }));

    expect(ignoreProjectName).toHaveBeenCalledWith("内部分享");
    expect(onChange).toHaveBeenCalledWith({
      attribution: { ...value, state: "none", new_project_name: null },
    });
  });
});

describe("AttributionBar 刚改过", () => {
  const changed = attribution({
    state: "manual",
    project_id: "p-b",
    origin: "manual",
    reassigned_from: {
      project_id: "p-a",
      project_name: "云图AI",
      origin_before: "ai",
      at: new Date().toISOString(),
      undo_until: new Date(Date.now() + 600_000).toISOString(),
      can_undo: true,
      tasks_left: [{ id: "t-1", title: "改登录页", requirement_id: "r-1", requirement_title: "登录改版" }],
      cue_hint: { term_id: "gt-1", term: "灰度方案" },
    },
  });

  it("［改回］在 10 分钟内走撤销", async () => {
    const undoMeetingProject = vi.fn().mockResolvedValue(detail("p-a", { state: "auto", origin: "ai" }));
    const { onNotice } = setup(changed, { undoMeetingProject });

    expect(screen.getByText(/由 云图AI 改来/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "改回" }));

    expect(undoMeetingProject).toHaveBeenCalledWith("m-1");
    expect(onNotice).toHaveBeenCalledWith("已撤销刚才的改动");
  });

  it("超过撤销时间后［改回］就是再改一次", async () => {
    const updateMeeting = vi.fn().mockResolvedValue(detail("p-a", { state: "manual" }));
    setup(
      { ...changed, reassigned_from: { ...changed.reassigned_from!, can_undo: false } },
      { updateMeeting },
    );

    await userEvent.click(screen.getByRole("button", { name: "改回" }));

    expect(updateMeeting).toHaveBeenCalledWith("m-1", { project_id: "p-a" });
  });

  it("留在旧需求上的任务可以也移过去", async () => {
    const updateTask = vi.fn().mockResolvedValue({});
    const { onChange } = setup(changed, { updateTask });

    expect(screen.getByText("1 条任务挂在 云图AI 的需求「登录改版」上")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "也移过去" }));

    expect(updateTask).toHaveBeenCalledWith("t-1", { project_id: "p-b", requirement_id: null });
    expect(onChange.mock.calls[0][0].attribution.reassigned_from.tasks_left).toEqual([]);
  });

  it("以后不再用这个词判断项目", async () => {
    const updateGlossaryTerm = vi.fn().mockResolvedValue({});
    const { onNotice } = setup(changed, { updateGlossaryTerm });

    await userEvent.click(screen.getByRole("button", { name: "以后不再用「灰度方案」判断项目" }));

    expect(updateGlossaryTerm).toHaveBeenCalledWith("gt-1", { is_cue: false });
    expect(onNotice).toHaveBeenCalledWith("以后不再用「灰度方案」判断项目");
  });

  it("人工归的会没有最近改动时不显示归属条", () => {
    const { container } = render(
      <AttributionBar
        apiClient={{} as ApiClient}
        attribution={attribution({ state: "manual", origin: "manual" })}
        meetingId="m-1"
        onChange={vi.fn()}
        onNotice={vi.fn()}
        projects={PROJECTS}
      />,
    );
    expect(container).toBeEmptyDOMElement();
  });
});
