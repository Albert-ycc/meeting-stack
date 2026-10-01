import { act, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiError, type ApiClient } from "../api";
import type { MeetingAttribution, NameCandidatesPayload, NameHint, Project } from "../types";
import { AttributionBar } from "./AttributionBar";
import { CHECKING_WAIT_MS } from "./NewNamePrompt";

const PROJECTS: Project[] = [
  { id: "p-a", name: "云图AI", color: "#2c8d83" },
  { id: "p-b", name: "数据中台", color: "#5090ff" },
  { id: "p-c", name: "智慧园区", color: "#aa66cc" },
];
const PARENT = "/Volumes/资料盘/项目";
const PROJECT_HINT: NameHint = { kind: "project", name: "云图数据看板", spoken: ["数据看板"] };
const REQUIREMENT_HINT: NameHint = {
  kind: "requirement",
  name: "数据看板",
  spoken: [],
  project_id: "p-a",
  project_name: "云图AI",
};

function attribution(overrides: Partial<MeetingAttribution> = {}): MeetingAttribution {
  return {
    state: "new_project",
    project_id: null,
    origin: null,
    method: "llm_new",
    evidence: [],
    candidates: [],
    reason: "",
    new_project_name: "云图数据看板",
    name_hint: PROJECT_HINT,
    reassigned_from: null,
    ai_configured: true,
    ...overrides,
  };
}

function payload(overrides: Partial<NameCandidatesPayload> = {}): NameCandidatesPayload {
  return {
    hint: PROJECT_HINT,
    candidates: [
      { name: "云图看板", folder_path: `${PARENT}/云图看板`, spoken: null, ai: false, similar_folder_path: null },
      {
        name: "数据看板",
        folder_path: null,
        spoken: { count: 7, first_ms: 12_000, anchors_ms: [12_000, 65_000] },
        ai: false,
        similar_folder_path: null,
      },
      { name: "云图数据看板", folder_path: null, spoken: null, ai: true, similar_folder_path: null },
      { name: "云图看板2026", folder_path: null, spoken: null, ai: false, similar_folder_path: `${PARENT}/云图看板2026` },
    ],
    meetings: [
      { id: "m-1", title: "看板评审", date: "2026-09-20", said_ms: 12_000 },
      { id: "m-2", title: "看板二次沟通", date: "2026-09-22", said_ms: 30_000 },
    ],
    default_action: "create_project",
    project: null,
    requirement_projects: [
      { id: "p-b", name: "数据中台", color: "#5090ff", suggested: true },
      { id: "p-a", name: "云图AI", color: "#2c8d83", suggested: false },
    ],
    folder: { mode: "mount", path: `${PARENT}/云图看板` },
    folders_state: "ready",
    create_parent: PARENT,
    create_parent_source: "setting",
    create_parent_state: "online",
    ...overrides,
  };
}

function meetingDetail(projectId: string | null, next: Partial<MeetingAttribution>) {
  return {
    id: "m-1",
    project_id: projectId,
    project_name: PROJECTS.find((project) => project.id === projectId)?.name ?? null,
    project_color: null,
    project_origin: projectId ? "manual" : null,
    attribution: attribution({ project_id: projectId, name_hint: null, ...next }),
    requirements: [],
  };
}

function setup(
  value: MeetingAttribution,
  client: Partial<ApiClient> = {},
  props: { isMobile?: boolean; lockedReason?: string } = {},
) {
  const onChange = vi.fn();
  const onNotice = vi.fn();
  const onSeek = vi.fn();
  const onPlayMeeting = vi.fn();
  const onProjectsChanged = vi.fn();
  const onOpenRequirement = vi.fn();
  const apiClient = {
    meeting: vi.fn().mockResolvedValue(meetingDetail(null, { state: "manual" })),
    ...client,
  } as unknown as ApiClient;
  render(
    <AttributionBar
      apiClient={apiClient}
      attribution={value}
      isMobile={props.isMobile}
      lockedReason={props.lockedReason}
      meetingId="m-1"
      onChange={onChange}
      onNotice={onNotice}
      onOpenRequirement={onOpenRequirement}
      onPlayMeeting={onPlayMeeting}
      onProjectsChanged={onProjectsChanged}
      onSeek={onSeek}
      projects={PROJECTS}
      // 按关系图会议面板的用法挂：会议页从 R01-11 起不出「像是新需求」
      requirementHints
    />,
  );
  return { onChange, onNotice, onSeek, onPlayMeeting, onProjectsChanged, onOpenRequirement, apiClient };
}

/** onNotice 第 n 次调用带的按钮 */
function noticeActions(onNotice: ReturnType<typeof vi.fn>, call = -1) {
  const calls = onNotice.mock.calls;
  const args = calls[call < 0 ? calls.length + call : call];
  return (args[3] ?? []) as { label: string; onClick: () => void }[];
}

describe("NewNamePrompt 像是一个新项目", () => {
  it("预填第一个候选；候选小标签写证据；同名的会点［看看］列出来，▶ 放那场会", async () => {
    const nameCandidates = vi.fn().mockResolvedValue(payload());
    const { onPlayMeeting } = setup(attribution(), { nameCandidates });

    const input = screen.getByRole("textbox", { name: "名字" });
    expect(await screen.findByDisplayValue("云图看板")).toBe(input);
    expect(nameCandidates).toHaveBeenCalledWith("m-1");
    expect(screen.getByText("像是一个新项目")).toBeInTheDocument();
    for (const label of [
      "云图看板 · 磁盘上有这个文件夹",
      "数据看板 · 会上说过 7 次",
      "云图数据看板 · AI 起的名字",
      "云图看板2026 · 名字相近的文件夹",
    ]) {
      expect(screen.getByRole("button", { name: label })).toBeInTheDocument();
    }

    // 默认动作是建成项目：它是实心的
    expect(screen.getByRole("button", { name: "建成项目" })).toHaveClass("name-prompt__main--solid");
    expect(screen.getByRole("button", { name: "建成需求" })).not.toHaveClass("name-prompt__main--solid");
    expect(screen.getByText(`会挂上 ${PARENT}/云图看板`)).toBeInTheDocument();

    expect(screen.getByText(/另有 1 场会也像是它/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "看看" }));
    const list = screen.getByRole("list", { name: "同名的会" });
    expect(within(list).getByText("看板二次沟通")).toBeInTheDocument();
    expect(within(list).getByText("2026-09-22")).toBeInTheDocument();
    await userEvent.click(within(list).getByRole("button", { name: "从 00:30 播放「看板二次沟通」" }));
    expect(onPlayMeeting).toHaveBeenCalledWith("m-2", 30_000);
  });

  it("会上叫法的 ▶ 先放第一次说到的地方，再点跳到下一处", async () => {
    const { onSeek } = setup(attribution(), { nameCandidates: vi.fn().mockResolvedValue(payload()) });

    await userEvent.click(await screen.findByRole("button", { name: "从 00:12 播放「数据看板」" }));
    expect(onSeek).toHaveBeenLastCalledWith(12_000);
    await userEvent.click(screen.getByRole("button", { name: "从 01:05 播放「数据看板」" }));
    expect(onSeek).toHaveBeenLastCalledWith(65_000);
  });

  it("换名字后文件夹说明跟着变；名字相近的文件夹要点「改成挂上这个文件夹」才挂", async () => {
    setup(attribution(), { nameCandidates: vi.fn().mockResolvedValue(payload()) });
    await screen.findByDisplayValue("云图看板");

    await userEvent.click(screen.getByRole("button", { name: "数据看板 · 会上说过 7 次" }));
    expect(screen.getByRole("textbox", { name: "名字" })).toHaveValue("数据看板");
    expect(screen.getByText(`会新建 ${PARENT}/数据看板`)).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "云图看板2026 · 名字相近的文件夹" }));
    expect(screen.getByText(`会新建 ${PARENT}/云图看板2026`)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "改成挂上这个文件夹" }));
    expect(screen.getByText(`会挂上 ${PARENT}/云图看板2026`)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "改成挂上这个文件夹" })).not.toBeInTheDocument();

    // 手输的名字：exFAT 不认的字符换成 -
    const input = screen.getByRole("textbox", { name: "名字" });
    await userEvent.clear(input);
    await userEvent.type(input, "看板/v2");
    expect(screen.getByText(`会新建 ${PARENT}/看板-v2`)).toBeInTheDocument();
  });

  it("［建成项目］带上同名的会、AI 起的名字和文件夹；记进也叫的叫法能撤销", async () => {
    const createProjectWith = vi.fn().mockResolvedValue({
      id: "p-new",
      name: "云图看板",
      color: "#5090ff",
      meetings_assigned: 2,
      spoken_added: { name: "数据看板", count: 7, event_id: 41, undo_until: "2099-01-01T00:00:00Z" },
    });
    const undoSpokenAlsoName = vi.fn().mockResolvedValue({ ok: true, removed: true });
    const { onNotice, onChange, onProjectsChanged, apiClient } = setup(attribution(), {
      nameCandidates: vi.fn().mockResolvedValue(payload()),
      createProjectWith,
      undoSpokenAlsoName,
    });
    await screen.findByDisplayValue("云图看板");

    await userEvent.click(screen.getByRole("button", { name: "建成项目" }));

    expect(createProjectWith).toHaveBeenCalledWith({
      name: "云图看板",
      color: "#5090ff",
      source_name: "云图数据看板",
      meeting_ids: ["m-1", "m-2"],
      folder: { mode: "mount", path: `${PARENT}/云图看板` },
    });
    expect(onNotice).toHaveBeenCalledWith(
      `已建成项目『云图看板』，2 场会归进去了，挂上了 ${PARENT}/云图看板。以后会上说『数据看板』也会认成『云图看板』（记进了它的也叫）`,
      undefined,
      "success",
      [expect.objectContaining({ label: "撤销" })],
    );
    expect(apiClient.meeting).toHaveBeenCalledWith("m-1");
    expect(onChange).toHaveBeenCalled();
    expect(onProjectsChanged).toHaveBeenCalled();

    await act(async () => noticeActions(onNotice)[0].onClick());
    expect(undoSpokenAlsoName).toHaveBeenCalledWith("p-new", 41);
    expect(onNotice).toHaveBeenLastCalledWith("已撤销：以后会上说『数据看板』不再认成『云图看板』");
  });

  it("推荐位置：写「会新建」和灰字，［设为项目总文件夹］后重取候选；建的时候在那里新建", async () => {
    const nameCandidates = vi
      .fn()
      .mockResolvedValueOnce(payload({ candidates: payload().candidates.slice(1), create_parent_source: "suggested" }))
      .mockResolvedValueOnce(payload({ candidates: payload().candidates.slice(1) }));
    const setProjectParent = vi.fn().mockResolvedValue({});
    const createProjectWith = vi.fn().mockResolvedValue({
      id: "p-new",
      name: "数据看板",
      color: "#5090ff",
      meetings_assigned: 2,
      material_roots: [{ id: 1, project_id: "p-new", path: `${PARENT}/数据看板`, exists: true, created_at: "" }],
      spoken_added: null,
    });
    const { onNotice } = setup(attribution(), { nameCandidates, setProjectParent, createProjectWith });

    expect(await screen.findByText(`会新建 ${PARENT}/数据看板`)).toBeInTheDocument();
    expect(screen.getByText("这是你多数项目文件夹所在的位置")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "设为项目总文件夹" }));
    expect(setProjectParent).toHaveBeenCalledWith(PARENT);
    expect(nameCandidates).toHaveBeenCalledTimes(2);
    await screen.findByText(`会新建 ${PARENT}/数据看板`);
    expect(screen.queryByText("这是你多数项目文件夹所在的位置")).not.toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "建成项目" }));
    expect(createProjectWith).toHaveBeenCalledWith(
      expect.objectContaining({ name: "数据看板", folder: { mode: "create", path: PARENT, name: "数据看板" } }),
    );
    expect(onNotice).toHaveBeenCalledWith(
      `已建成项目『数据看板』，2 场会归进去了，新建并挂上了 ${PARENT}/数据看板`,
      undefined,
      "success",
      undefined,
    );
  });

  it("资料盘没连接：项目照常建，插上后自动建", async () => {
    const createProjectWith = vi.fn().mockResolvedValue({
      id: "p-new",
      name: "云图数据看板",
      color: "#5090ff",
      meetings_assigned: 1,
      folder_pending: { path: `${PARENT}/云图数据看板`, reason: "资料盘未连接，插上后再建文件夹" },
    });
    const { onNotice } = setup(attribution(), {
      nameCandidates: vi.fn().mockResolvedValue(
        payload({
          candidates: [payload().candidates[2]],
          meetings: [payload().meetings[0]],
          create_parent_state: "volume_offline",
        }),
      ),
      createProjectWith,
    });

    expect(
      await screen.findByText(`资料盘没连接：项目照常建，插上后自动建 ${PARENT}/云图数据看板`),
    ).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "建成项目" }));
    expect(onNotice).toHaveBeenCalledWith(
      `已建成项目『云图数据看板』，1 场会归进去了；资料盘没连接，插上后自动建 ${PARENT}/云图数据看板`,
      undefined,
      "success",
      undefined,
    );
  });

  it("还没有可参照的项目文件夹：这次先不建，［选项目总文件夹…］就地打开取径器", async () => {
    const nameCandidates = vi
      .fn()
      .mockResolvedValueOnce(payload({ candidates: payload().candidates.slice(1), create_parent: null, create_parent_source: null, create_parent_state: null }))
      .mockResolvedValueOnce(payload({ candidates: payload().candidates.slice(1) }));
    const setProjectParent = vi.fn().mockResolvedValue({});
    setup(attribution(), {
      nameCandidates,
      setProjectParent,
      browseMaterials: vi.fn().mockResolvedValue({
        base: "/Volumes/资料盘",
        path: "/Volumes/资料盘",
        parent: null,
        breadcrumbs: [{ name: "资料盘", path: "/Volumes/资料盘" }],
        dirs: [{ name: "项目", path: PARENT }],
      }),
    });

    expect(await screen.findByText("还没有可参照的项目文件夹，这次先不建文件夹")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "选项目总文件夹…" }));
    const picker = screen.getByRole("dialog", { name: "选项目总文件夹" });
    await userEvent.click(await within(picker).findByText("项目"));
    await userEvent.click(within(picker).getByRole("button", { name: "确定" }));

    expect(setProjectParent).toHaveBeenCalledWith(PARENT);
    expect(await screen.findByText(`会新建 ${PARENT}/数据看板`)).toBeInTheDocument();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("文件夹缓存还没好：［建成项目］等一下，2 秒后再取", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const nameCandidates = vi
      .fn()
      .mockResolvedValueOnce(payload({ folders_state: "checking", candidates: payload().candidates.slice(2) }))
      .mockResolvedValueOnce(payload());
    setup(attribution(), { nameCandidates });

    expect(await screen.findByText("正在看磁盘上有没有同名文件夹…")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "建成项目" })).toBeDisabled();
    expect(screen.getByRole("textbox", { name: "名字" })).toHaveValue("云图数据看板");

    await act(async () => {
      await vi.advanceTimersByTimeAsync(2000);
    });
    expect(nameCandidates).toHaveBeenCalledTimes(2);
    // 名字没动过：换成新的第一个候选（同名文件夹）
    expect(screen.getByRole("textbox", { name: "名字" })).toHaveValue("云图看板");
    expect(screen.getByRole("button", { name: "建成项目" })).toBeEnabled();
    expect(screen.getByText(`会挂上 ${PARENT}/云图看板`)).toBeInTheDocument();
  });

  it("等了 15 秒还没好：不再等，［建成项目］照常能点", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const nameCandidates = vi.fn().mockResolvedValue(payload({ folders_state: "checking" }));
    setup(attribution(), { nameCandidates });

    expect(await screen.findByText("正在看磁盘上有没有同名文件夹…")).toBeInTheDocument();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(CHECKING_WAIT_MS + 100);
    });
    expect(screen.queryByText("正在看磁盘上有没有同名文件夹…")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "建成项目" })).toBeEnabled();
    expect(screen.getByText(`会挂上 ${PARENT}/云图看板`)).toBeInTheDocument();
    // 已经排上的那一次还会问，之后就不再问了
    await act(async () => {
      await vi.advanceTimersByTimeAsync(4000);
    });
    const calls = nameCandidates.mock.calls.length;
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10_000);
    });
    expect(nameCandidates).toHaveBeenCalledTimes(calls);
  });

  it("近似重名时［用它］把同名的几场会都改过去", async () => {
    const createProjectWith = vi.fn().mockRejectedValue(
      new ApiError("已有「云图AI」，是不是它？", 409, {
        suggestion: { id: "p-a", project_id: "p-a", name: "云图AI", also_names: [], matched: "云图", match: "similar", exact: false },
      }),
    );
    const updateMeeting = vi.fn().mockResolvedValue(meetingDetail("p-a", { state: "manual" }));
    const { onNotice } = setup(attribution(), {
      nameCandidates: vi.fn().mockResolvedValue(payload()),
      createProjectWith,
      updateMeeting,
    });
    await screen.findByDisplayValue("云图看板");

    await userEvent.click(screen.getByRole("button", { name: "建成项目" }));
    expect(screen.getByRole("button", { name: "仍然新建" })).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "用它" }));

    expect(updateMeeting).toHaveBeenCalledWith("m-1", { project_id: "p-a" });
    expect(updateMeeting).toHaveBeenCalledWith("m-2", { project_id: "p-a" });
    expect(onNotice).toHaveBeenCalledWith("已改到 云图AI；同名的另 1 场会也改过去了", undefined);
  });

  it("［建成需求］：没归项目时从下拉选项目（推荐的在前、默认选上），提示里能打开需求和撤销", async () => {
    const nameAsRequirement = vi.fn().mockResolvedValue({
      requirement_id: "r-9",
      requirement_title: "云图看板",
      project_id: "p-b",
      project_name: "数据中台",
      existing: false,
      priority: "P2",
      meetings_linked: 2,
      meetings_assigned: 2,
      meeting_ids: ["m-1", "m-2"],
      folder_attached: null,
      folder_error: null,
      event_id: 77,
      undo_until: "2099-01-01T00:00:00Z",
    });
    const undoNameAsRequirement = vi
      .fn()
      .mockResolvedValue({ ok: true, requirement_deleted: true, meetings_restored: 2, meeting_ids: ["m-1", "m-2"] });
    const { onNotice, onOpenRequirement, apiClient } = setup(attribution(), {
      nameCandidates: vi.fn().mockResolvedValue(payload()),
      nameAsRequirement,
      undoNameAsRequirement,
    });
    await screen.findByDisplayValue("云图看板");

    const select = screen.getByRole("combobox", { name: "建在哪个项目" });
    expect(select).toHaveValue("p-b");
    expect(within(select).getAllByRole("option").map((option) => option.textContent)).toEqual([
      "建在哪个项目…",
      "数据中台",
      "云图AI",
    ]);
    await userEvent.click(screen.getByRole("button", { name: "建成需求" }));

    // 项目提示的候选文件夹是项目文件夹，不挂成需求文件夹
    expect(nameAsRequirement).toHaveBeenCalledWith("m-1", { title: "云图看板", project_id: "p-b" });
    expect(onNotice).toHaveBeenCalledWith(
      "已在『数据中台』建好需求『云图看板』（P2），2 场会已关联",
      undefined,
      "success",
      [expect.objectContaining({ label: "打开需求" }), expect.objectContaining({ label: "撤销" })],
    );
    expect(apiClient.meeting).toHaveBeenCalledWith("m-1");

    const [open, undo] = noticeActions(onNotice);
    open.onClick();
    expect(onOpenRequirement).toHaveBeenCalledWith("r-9");
    await act(async () => undo.onClick());
    expect(undoNameAsRequirement).toHaveBeenCalledWith("m-1");
    expect(onNotice).toHaveBeenLastCalledWith("已撤销：需求『云图看板』删掉了，2 场会改回了原来的归属");
  });

  it("没选项目时［建成需求］点不了", async () => {
    setup(attribution(), {
      nameCandidates: vi.fn().mockResolvedValue(
        payload({ requirement_projects: [{ id: "p-a", name: "云图AI", color: "#2c8d83", suggested: false }] }),
      ),
    });
    await screen.findByDisplayValue("云图看板");

    expect(screen.getByRole("combobox", { name: "建在哪个项目" })).toHaveValue("");
    expect(screen.getByRole("button", { name: "建成需求" })).toBeDisabled();
    await userEvent.selectOptions(screen.getByRole("combobox", { name: "建在哪个项目" }), "p-a");
    expect(screen.getByRole("button", { name: "建成需求" })).toBeEnabled();
  });

  it("归属条锁住时提示里的按钮也都置灰", async () => {
    setup(attribution(), { nameCandidates: vi.fn().mockResolvedValue(payload()) }, { lockedReason: "右侧有未保存的归属修改" });
    await screen.findByDisplayValue("云图看板");

    expect(screen.getByText("右侧有未保存的归属修改")).toBeInTheDocument();
    for (const name of ["建成项目", "建成需求", "不是新项目"]) {
      expect(screen.getByRole("button", { name })).toBeDisabled();
    }
  });
});

describe("NewNamePrompt 像是新需求", () => {
  const requirementPayload = () =>
    payload({
      hint: REQUIREMENT_HINT,
      candidates: [
        {
          name: "数据看板",
          folder_path: "/Volumes/资料盘/云图AI/数据看板",
          spoken: { count: 3, first_ms: 5_000, anchors_ms: [5_000] },
          ai: true,
          similar_folder_path: null,
        },
      ],
      meetings: [{ id: "m-1", title: "看板评审", date: "2026-09-20", said_ms: 5_000 }],
      default_action: "create_requirement",
      project: { id: "p-a", name: "云图AI" },
      requirement_projects: [],
      folder: { mode: "mount", path: "/Volumes/资料盘/云图AI/数据看板" },
    });
  const autoWithHint = attribution({ state: "auto", project_id: "p-a", origin: "ai", new_project_name: null, name_hint: REQUIREMENT_HINT });

  it("自动归属的会下面出提示：建成需求是默认，挂根目录下的同名子文件夹", async () => {
    const nameAsRequirement = vi.fn().mockResolvedValue({
      requirement_id: "r-1",
      requirement_title: "数据看板",
      project_id: "p-a",
      project_name: "云图AI",
      existing: false,
      priority: "P2",
      meetings_linked: 1,
      meetings_assigned: 0,
      meeting_ids: ["m-1"],
      folder_attached: null,
      folder_error: "资料盘未连接",
      event_id: 5,
      undo_until: "2099-01-01T00:00:00Z",
    });
    const { onNotice } = setup(autoWithHint, {
      nameCandidates: vi.fn().mockResolvedValue(requirementPayload()),
      nameAsRequirement,
    });

    expect(screen.getByRole("button", { name: "对的" })).toBeInTheDocument();
    expect(screen.getByText("像是『云图AI』里的一个新需求")).toBeInTheDocument();
    await screen.findByText("会把 云图AI/数据看板/ 挂成需求文件夹");
    const requirement = screen.getByRole("button", { name: "建成需求" });
    expect(requirement).toHaveClass("name-prompt__main--solid");
    expect(screen.getByRole("button", { name: "建成项目" })).not.toHaveClass("name-prompt__main--solid");
    // 已归项目：不再给项目下拉，也没有［不是新项目］［选已有项目］
    expect(screen.queryByRole("combobox", { name: "建在哪个项目" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "不是新项目" })).not.toBeInTheDocument();
    expect(screen.queryByRole("combobox", { name: "选已有项目" })).not.toBeInTheDocument();
    // 需求提示的文件夹候选在项目根目录里面，不拿来挂成新项目的文件夹
    expect(screen.getByText(`会新建 ${PARENT}/数据看板`)).toBeInTheDocument();

    await userEvent.click(requirement);
    expect(nameAsRequirement).toHaveBeenCalledWith("m-1", {
      title: "数据看板",
      folder_path: "/Volumes/资料盘/云图AI/数据看板",
    });
    expect(onNotice).toHaveBeenCalledWith(
      "已在『云图AI』建好需求『数据看板』（P2），1 场会已关联。需求文件夹没挂上：资料盘未连接",
      undefined,
      "warning",
      expect.any(Array),
    );
  });

  it("已有同名需求：只把会关联过去", async () => {
    const nameAsRequirement = vi.fn().mockResolvedValue({
      requirement_id: "r-1",
      requirement_title: "数据看板",
      project_id: "p-a",
      project_name: "云图AI",
      existing: true,
      priority: "P2",
      meetings_linked: 1,
      meetings_assigned: 0,
      meeting_ids: ["m-1"],
      folder_attached: null,
      folder_error: null,
      event_id: 6,
      undo_until: "2099-01-01T00:00:00Z",
    });
    const { onNotice } = setup(autoWithHint, {
      nameCandidates: vi.fn().mockResolvedValue(requirementPayload()),
      nameAsRequirement,
    });
    await screen.findByDisplayValue("数据看板");

    await userEvent.click(screen.getByRole("button", { name: "建成需求" }));
    expect(onNotice.mock.calls[0][0]).toBe("『云图AI』里已有需求『数据看板』，1 场会已关联过去");
  });

  it("［不算新需求］带上项目，提示能撤销", async () => {
    const ignoreProjectName = vi.fn().mockResolvedValue({
      name: "数据看板",
      norm_key: "数据看板",
      kind: "requirement",
      meetings_updated: 1,
      event_id: 51,
      undo_until: "2099-01-01T00:00:00Z",
    });
    const undoNameDecision = vi.fn().mockResolvedValue({ ok: true, meetings_restored: 1 });
    const { onChange, onNotice } = setup(autoWithHint, {
      nameCandidates: vi.fn().mockResolvedValue(requirementPayload()),
      ignoreProjectName,
      undoNameDecision,
    });

    await userEvent.click(await screen.findByRole("button", { name: "不算新需求" }));
    expect(ignoreProjectName).toHaveBeenCalledWith("数据看板", { kind: "requirement", project_id: "p-a", meeting_id: "m-1" });
    expect(onChange).toHaveBeenCalledWith({ attribution: { ...autoWithHint, name_hint: null } });
    expect(onNotice.mock.calls[0][0]).toBe("以后不再把『数据看板』当成新需求提示");
    await act(async () => noticeActions(onNotice, 0)[0].onClick());
    expect(undoNameDecision).toHaveBeenCalledWith(51);
  });

  it("你归的会平时不显示归属条，有新需求提示时只显示提示", async () => {
    setup(attribution({ state: "manual", project_id: "p-a", origin: "manual", new_project_name: null, name_hint: REQUIREMENT_HINT }), {
      nameCandidates: vi.fn().mockResolvedValue(requirementPayload()),
    });

    expect(screen.getByText("像是『云图AI』里的一个新需求")).toBeInTheDocument();
    expect(await screen.findByDisplayValue("数据看板")).toBeInTheDocument();
    expect(screen.getByRole("group", { name: "项目归属" })).toHaveClass("attribution-bar--new");
  });
});

describe("NewNamePrompt 待你选和手机上", () => {
  const reviewing = attribution({
    state: "needs_review",
    new_project_name: null,
    candidates: [{ project_id: "p-a", project_name: "云图AI", count: 1, llm: false, current: false }],
    name_hint: { kind: "project", name: "云图看板", spoken: [] },
  });

  it("待你选的会：候选按钮下面加一行「也可能是一个新项目」，点了展开同一个提示", async () => {
    const nameCandidates = vi.fn().mockResolvedValue(payload());
    setup(reviewing, { nameCandidates });

    expect(screen.getByText("也可能是一个新项目『云图看板』")).toBeInTheDocument();
    expect(nameCandidates).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole("button", { name: "建成项目" }));

    expect(nameCandidates).toHaveBeenCalledWith("m-1");
    expect(screen.getByRole("group", { name: "像是一个新项目" })).toBeInTheDocument();
    expect(await screen.findByDisplayValue("云图看板")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "收起" }));
    expect(screen.queryByRole("group", { name: "像是一个新项目" })).not.toBeInTheDocument();
    expect(screen.getByText("也可能是一个新项目『云图看板』")).toBeInTheDocument();
  });

  it("手机上只读：不显示会动磁盘和建东西的按钮，保留［选已有项目］", async () => {
    const updateMeeting = vi.fn().mockResolvedValue(meetingDetail("p-c", { state: "manual" }));
    setup(attribution(), { nameCandidates: vi.fn().mockResolvedValue(payload()), updateMeeting }, { isMobile: true });

    expect(screen.getByText("在电脑上打开可以建项目并建文件夹")).toBeInTheDocument();
    expect(await screen.findByText("『云图看板』")).toBeInTheDocument();
    for (const name of ["建成项目", "建成需求", "不是新项目", "设为项目总文件夹", "选项目总文件夹…"]) {
      expect(screen.queryByRole("button", { name })).not.toBeInTheDocument();
    }
    expect(screen.queryByRole("textbox", { name: "名字" })).not.toBeInTheDocument();
    await userEvent.selectOptions(screen.getByRole("combobox", { name: "选已有项目" }), "p-c");
    expect(updateMeeting).toHaveBeenCalledWith("m-1", { project_id: "p-c" });
  });

  it("手机上待你选的那一行只写一句，不给［建成项目］", () => {
    setup(reviewing, { nameCandidates: vi.fn().mockResolvedValue(payload()) }, { isMobile: true });

    expect(screen.getByText("也可能是一个新项目『云图看板』")).toBeInTheDocument();
    expect(screen.getByText("在电脑上打开可以建项目并建文件夹")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "建成项目" })).not.toBeInTheDocument();
  });
});

afterEach(() => vi.useRealTimers());
