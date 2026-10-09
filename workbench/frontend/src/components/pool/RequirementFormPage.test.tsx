import { configure, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiError, type ApiClient } from "../../api";
import type { CandidateDetail, MeetingSummary, Project, RequirementDetail, Segment } from "../../types";
import { candidateItem, CVM, EXPORT_SOURCE, HENGRUI, HUAXIA, JD_SOURCE, RECEIPT_SOURCE, taskItem, YIMI } from "./poolFixtures";
import { cleanTitle, LEAVE_FORM_CONFIRM, RequirementFormPage, splitProjects, summaryLength } from "./RequirementFormPage";

// 项目的座次和最近会议时间照生产库：座次 1～3 是「我的方向」，其余按最近一场会排
const ANXIN = "project-02500313fb494b2d";
const COPY = "project-036a983d51b849f1";
const ORAL = "project-a9eab7580a614e5b";
const PAGER = "project-e12e757109954848";

const PROJECTS: Project[] = [
  { id: CVM, name: "CVM 云讲堂", color: "#5090ff", seat: null, latest_meeting_date: "2026-09-29T19:26:37-07:00" },
  { id: HENGRUI, name: "恒瑞健康", color: "#549c8a", seat: 2, latest_meeting_date: "2026-09-21T19:21:49-07:00" },
  { id: YIMI, name: "医米科研用药", color: "#2c8d83", seat: 1, latest_meeting_date: "2026-09-28T18:37:26-07:00" },
];

const ALL_PROJECTS: Project[] = [
  { id: PAGER, name: "寻呼随访项目", color: "#3f51b5", seat: null, latest_meeting_date: null },
  { id: ORAL, name: "口服药到店领取配置方案", color: "#2c8d83", seat: null, latest_meeting_date: "2026-09-16T21:55:00-07:00" },
  { id: HUAXIA, name: "华夏基金会科普同行", color: "#2c8d83", seat: 3, latest_meeting_date: "2026-09-14T00:30:09-07:00" },
  { id: COPY, name: "项目复制与名单一键转移", color: "#2c8d83", seat: null, latest_meeting_date: "2026-09-19T23:54:21-07:00" },
  ...PROJECTS,
  { id: ANXIN, name: "安心四季", color: "#2c8d83", seat: null, latest_meeting_date: "2026-09-20T02:19:44-07:00" },
];

function candidateDetail(overrides: Partial<CandidateDetail> = {}): CandidateDetail {
  return { ...candidateItem(), status: "pending", sources: [RECEIPT_SOURCE], ...overrides };
}

function renderForm(api: Partial<ApiClient>, props: Partial<Parameters<typeof RequirementFormPage>[0]> = {}) {
  const handlers = { onCancel: vi.fn(), onDone: vi.fn(), onDirtyChange: vi.fn() };
  const client = { requirementTitleCheck: vi.fn().mockResolvedValue({ existing: null }), ...api } as unknown as ApiClient & {
    requirementTitleCheck: ReturnType<typeof vi.fn>;
  };
  render(
    <RequirementFormPage
      apiClient={client}
      canPickFolders={false}
      candidateId="candidate-receipt"
      mode="claim"
      projects={PROJECTS}
      {...handlers}
      {...props}
    />,
  );
  return { ...handlers, client };
}

// 云课堂那场会：会名、时间、时长和逐字稿照生产库（见后端 requirement_pool_world）
const CVM_MEETING: MeetingSummary = {
  id: EXPORT_SOURCE.meeting_id,
  title: EXPORT_SOURCE.meeting_title,
  recording_date: EXPORT_SOURCE.recording_date,
  duration_ms: EXPORT_SOURCE.duration_ms,
  status: "published",
  project_id: CVM,
  project_name: "CVM 云讲堂",
  audio_artifact_id: null,
  tags: [],
};
const CVM_SEGMENTS: Segment[] = [
  [568390, "预约审核查看。"],
  [576900, "那我有办法导出 excel 吗？"],
  [581000, "是没有办法，"],
  [582060, "我看到导出是一个 OKOK。"],
].map(([start, text], ordinal) => ({
  id: `seg-${start}`,
  ordinal,
  start_ms: start as number,
  end_ms: (start as number) + 1000,
  text: text as string,
}));

const JD_FOLDER = {
  id: 1,
  name: "对接京东科研仓",
  path: "/Volumes/外置中枢/医朵云/医米科研用药/对接京东科研仓",
  exists: true,
  file_count: 2,
  file_count_capped: false,
  modified_at: "2026-09-14T08:00:00+00:00",
  preview_files: [],
};

function jdDetail(overrides: Partial<RequirementDetail> = {}): RequirementDetail {
  return {
    id: "requirement-jd",
    project_id: YIMI,
    project_name: "医米科研用药",
    project_color: "#2c8d83",
    project_seat: 1,
    title: "京东科研仓对接",
    summary: "把京东科研仓当作一个药房接进医米：采购单入库、销售单、订单取消、物流轨迹四类接口必须对上，签收凭证怎么拿还悬着。",
    priority: "P0",
    status: "active",
    created_at: "2026-09-16T06:48:37+00:00",
    updated_at: "2026-09-16T06:48:37+00:00",
    open_task_count: 0,
    meeting_count: 1,
    latest_meeting_date: JD_SOURCE.recording_date,
    folder_count: 1,
    folders: [JD_FOLDER],
    meetings: [
      {
        id: JD_SOURCE.meeting_id,
        title: JD_SOURCE.meeting_title,
        recording_date: JD_SOURCE.recording_date,
        duration_ms: JD_SOURCE.duration_ms,
        canonical_dir: null,
      },
    ],
    tasks: [],
    source: JD_SOURCE,
    sources: [JD_SOURCE, { ...RECEIPT_SOURCE, kind: "merged", via_candidate_title: "京东仓签收凭证" }],
    follow_up_count: 0,
    ...overrides,
  };
}

const EDIT = { mode: "edit", candidateId: undefined, requirementId: "requirement-jd" } as const;

function renderEdit(api: Partial<ApiClient> = {}, props: Partial<Parameters<typeof RequirementFormPage>[0]> = {}) {
  return renderForm({ requirement: vi.fn().mockResolvedValue(jdDetail()), ...api }, { ...EDIT, ...props });
}

function submitButton(name: "认领" | "创建" | "保存") {
  return screen.getByRole("button", { name });
}

function projectSelect() {
  return screen.getByRole("combobox", { name: /所属项目/ });
}

async function chooseProject(name: RegExp | string) {
  await userEvent.click(projectSelect());
  await userEvent.click(await screen.findByRole("option", { name }));
}

function priorityButton(name: "P0" | "P1" | "P2" | "P3") {
  return within(screen.getByRole("group", { name: "优先级" })).getByRole("button", { name });
}

function fireBeforeUnload() {
  const event = new Event("beforeunload", { cancelable: true });
  window.dispatchEvent(event);
  return event;
}

// 防抖 300ms 加上渲染，机器忙的时候会超过默认的 1 秒
configure({ asyncUtilTimeout: 4000 });

afterEach(() => {
  vi.restoreAllMocks();
});

describe("RequirementFormPage", () => {
  it("认领：需求名和说明 AI 预填、来源只读，改好后按页面上的值认领", async () => {
    const requirementCandidate = vi.fn().mockResolvedValue(candidateDetail());
    const claimed = { id: "requirement-receipt", title: "京东仓签收凭证与对账" };
    const claimCandidate = vi.fn().mockResolvedValue(claimed);
    const handlers = renderForm({ requirementCandidate, claimCandidate });

    const title = await screen.findByDisplayValue("京东仓签收凭证");
    expect(requirementCandidate).toHaveBeenCalledWith("candidate-receipt");
    expect(screen.getByRole("heading", { name: "认领候选" })).toBeInTheDocument();
    expect(screen.getAllByText("AI 预填")).toHaveLength(2);
    expect(screen.getByText("42 / 70")).not.toHaveClass("is-over");

    expect(projectSelect()).toHaveTextContent("医米科研用药");
    const sourceField = screen.getByText("出自会议纪要，不可改").closest(".form-field") as HTMLElement;
    expect(within(sourceField).getByText("260916 医米京东科研仓系统对接")).toBeInTheDocument();
    expect(within(sourceField).getByText("09-17 10:01 · 51 分钟")).toBeInTheDocument();
    expect(within(sourceField).getByText("▶ 00:31:49")).toBeInTheDocument();
    expect(within(sourceField).getByText(`「${RECEIPT_SOURCE.quote}」`)).toBeInTheDocument();
    expect(within(sourceField).queryByRole("textbox")).not.toBeInTheDocument();

    await userEvent.type(title, "与对账");
    await userEvent.click(priorityButton("P1"));
    // 右边的墙上预览跟着变
    expect(screen.getByRole("article", { name: "需求：京东仓签收凭证与对账" })).toBeInTheDocument();

    await userEvent.click(submitButton("认领"));
    expect(claimCandidate).toHaveBeenCalledWith("candidate-receipt", {
      title: "京东仓签收凭证与对账",
      summary: candidateDetail().summary,
      project_id: YIMI,
      priority: "P1",
      folder_paths: [],
    });
    expect(handlers.onDone).toHaveBeenCalledWith({ kind: "claimed", requirement: claimed });
  });

  it("认领时保存才撞上同项目的同名需求：不新建，提示在需求名下方，可以改为合并到那一条（按页面上改选的项目）", async () => {
    const existing = { id: "requirement-hr-receipt", title: "京东仓签收凭证", status: "shelved" as const };
    const claimCandidate = vi
      .fn()
      .mockRejectedValue(new ApiError("「恒瑞健康」里已有同名需求", 409, { detail: "同名", existing }));
    const merged = { id: existing.id, title: existing.title };
    const mergeCandidate = vi.fn().mockResolvedValue(merged);
    const handlers = renderForm({
      requirementCandidate: vi.fn().mockResolvedValue(candidateDetail()),
      claimCandidate,
      mergeCandidate,
    });
    await screen.findByDisplayValue("京东仓签收凭证");

    await chooseProject(/恒瑞健康/);
    await userEvent.click(submitButton("认领"));

    const alert = await screen.findByText(/里已经有一条叫/);
    expect(alert).toHaveTextContent("恒瑞健康里已经有一条叫「京东仓签收凭证」的需求");
    // 红字在需求名输入框下面，不是底栏里的一句通用文字
    expect(screen.getByRole("textbox", { name: /需求名/ })).toHaveAttribute("aria-invalid", "true");
    expect(alert.closest(".form-actions")).toBeNull();
    expect(handlers.onDone).not.toHaveBeenCalled();
    expect(submitButton("认领")).toBeDisabled();

    await userEvent.click(screen.getByRole("button", { name: "改为合并到这条需求" }));
    expect(mergeCandidate).toHaveBeenCalledWith("candidate-receipt", "requirement-hr-receipt", HENGRUI);
    expect(handlers.onDone).toHaveBeenCalledWith({ kind: "merged", requirement: merged });
  });

  it("撞上的那条已完成：也给「改为合并到这条需求」（D13，并进去后它重新打开）；改个名字照样能走，一改名提示就收起", async () => {
    const existing = { id: "requirement-done", title: "京东仓签收凭证", status: "done" as const };
    renderForm({
      requirementCandidate: vi.fn().mockResolvedValue(candidateDetail()),
      // 边填边查和保存时的 409 说的是同一件事：不会中途改口，机器再忙也不会在两步之间把提示收掉
      requirementTitleCheck: vi.fn().mockResolvedValue({ existing }),
      claimCandidate: vi.fn().mockRejectedValue(new ApiError("同名", 409, { detail: "同名", existing })),
    });
    const title = await screen.findByDisplayValue("京东仓签收凭证");
    await userEvent.click(submitButton("认领"));

    expect(await screen.findByRole("button", { name: "改为合并到这条需求" })).toBeInTheDocument();
    expect(screen.queryByText("它已完成，不能合并，请改个名字")).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "改个名字" }));
    expect(title).toHaveFocus();

    await userEvent.type(title, "（物流）");
    expect(screen.queryByText(/里已经有一条叫/)).not.toBeInTheDocument();
  });

  it("撞名撞到已完成的需求、改为合并：合并到那一条，返回的 reopened 原样交给 App 去提示（D13）", async () => {
    const existing = { id: "requirement-done", title: "京东仓签收凭证", status: "done" as const };
    const merged = { id: existing.id, title: existing.title, status: "active", reopened: true };
    const mergeCandidate = vi.fn().mockResolvedValue(merged);
    const handlers = renderForm({
      requirementCandidate: vi.fn().mockResolvedValue(candidateDetail()),
      requirementTitleCheck: vi.fn().mockResolvedValue({ existing }),
      mergeCandidate,
    });
    await screen.findByDisplayValue("京东仓签收凭证");

    await userEvent.click(await screen.findByRole("button", { name: "改为合并到这条需求" }));
    expect(mergeCandidate).toHaveBeenCalledWith("candidate-receipt", "requirement-done", YIMI);
    expect(handlers.onDone).toHaveBeenCalledWith({ kind: "merged", requirement: merged });
  });

  it("候选已经被处理过：提示一句，表单不能再提交", async () => {
    renderForm({
      requirementCandidate: vi.fn().mockResolvedValue(candidateDetail({ status: "merged", requirement_id: "requirement-jd" })),
    });

    expect(await screen.findByText("这条候选已经合并了。")).toBeInTheDocument();
    expect(screen.getByDisplayValue("京东仓签收凭证")).toBeDisabled();
    expect(submitButton("认领")).toBeDisabled();
  });

  it("新增：说明和来源选填；填齐需求名和项目才能创建，默认 P2", async () => {
    const requirementCandidate = vi.fn();
    const created = { id: "requirement-export", title: "科室会预约后台导出" };
    const createRequirement = vi.fn().mockResolvedValue(created);
    const handlers = renderForm({ requirementCandidate, createRequirement }, { mode: "create", candidateId: undefined });

    expect(screen.getByRole("heading", { name: "新增需求" })).toBeInTheDocument();
    expect(screen.getAllByText("选填")).toHaveLength(2);
    expect(screen.getByRole("button", { name: "＋ 选来源会议，再挑会上原话" })).toBeInTheDocument();
    expect(screen.queryByText("AI 预填")).not.toBeInTheDocument();
    expect(screen.queryByText("出自会议纪要，不可改")).not.toBeInTheDocument();
    expect(requirementCandidate).not.toHaveBeenCalled();
    expect(submitButton("创建")).toBeDisabled();

    await userEvent.type(screen.getByRole("textbox", { name: /需求名/ }), "科室会预约后台导出");
    expect(submitButton("创建")).toBeDisabled();
    await userEvent.type(
      screen.getByRole("textbox", { name: /说明/ }),
      "科室会后台按日期导出预约记录（Excel），运营不用再一条条截图对账。",
    );
    expect(screen.getByText("35 / 70")).toBeInTheDocument();
    await chooseProject(/CVM 云讲堂/);
    expect(priorityButton("P2")).toHaveAttribute("aria-pressed", "true");

    await userEvent.click(submitButton("创建"));
    expect(createRequirement).toHaveBeenCalledWith({
      title: "科室会预约后台导出",
      summary: "科室会后台按日期导出预约记录（Excel），运营不用再一条条截图对账。",
      project_id: CVM,
      priority: "P2",
      folder_paths: [],
    });
    expect(handlers.onDone).toHaveBeenCalledWith({ kind: "created", requirement: created });
  });

  it("说明按字算（表情也是一个字），超过 70 字不能保存", async () => {
    renderForm({ requirementCandidate: vi.fn().mockResolvedValue(candidateDetail({ summary: "" })) });
    await screen.findByDisplayValue("京东仓签收凭证");
    const summary = screen.getByRole("textbox", { name: /说明/ });

    await userEvent.click(summary);
    await userEvent.paste("👍".repeat(70));
    expect(screen.getByText("70 / 70")).not.toHaveClass("is-over");
    expect(submitButton("认领")).toBeEnabled();

    await userEvent.paste("。");
    expect(screen.getByText("71 / 70")).toHaveClass("is-over");
    expect(submitButton("认领")).toBeDisabled();
    expect(summaryLength("  👍👍  ")).toBe(2);
  });

  it("取消回到进入前的页面", async () => {
    const handlers = renderForm({ requirementCandidate: vi.fn().mockResolvedValue(candidateDetail()) });
    await screen.findByDisplayValue("京东仓签收凭证");

    await userEvent.click(screen.getByRole("button", { name: "取消" }));
    expect(handlers.onCancel).toHaveBeenCalledTimes(1);
  });

  it("新增时选来源：先选会、再挑原话，按住 Shift 连着选的几句拼成一句，时间锚取第一句", async () => {
    const meetings = vi.fn().mockResolvedValue({ items: [CVM_MEETING], total: 1, limit: 30, offset: 0 });
    const meeting = vi.fn().mockResolvedValue({ id: CVM_MEETING.id, title: CVM_MEETING.title, segments: CVM_SEGMENTS });
    const createRequirement = vi.fn().mockResolvedValue({ id: "requirement-export", title: "科室会预约后台导出" });
    renderForm({ meetings, meeting, createRequirement }, { mode: "create", candidateId: undefined });

    await userEvent.type(screen.getByRole("textbox", { name: /需求名/ }), "科室会预约后台导出");
    await chooseProject(/CVM 云讲堂/);
    await userEvent.click(screen.getByRole("button", { name: "＋ 选来源会议，再挑会上原话" }));
    const dialog = await screen.findByRole("dialog", { name: "选来源" });
    // 默认只列需求所属项目的会，从第一页取
    expect(meetings).toHaveBeenLastCalledWith({ q: undefined, project_id: CVM, limit: 30, offset: 0 });
    await userEvent.click(await within(dialog).findByRole("button", { name: /260929 云课堂直播运营问题对齐/ }));

    expect(meeting).toHaveBeenCalledWith(CVM_MEETING.id);
    await userEvent.click(await within(dialog).findByRole("button", { name: /那我有办法导出 excel 吗/ }));
    fireEvent.click(within(dialog).getByRole("button", { name: /是没有办法/ }), { shiftKey: true });
    expect(within(dialog).getByText("已选 2 句")).toBeInTheDocument();
    expect(within(dialog).getByText("▶ 00:09:36")).toBeInTheDocument();
    expect(within(dialog).getByText("「那我有办法导出 excel 吗？是没有办法，」")).toBeInTheDocument();
    await userEvent.click(within(dialog).getByRole("button", { name: "确定" }));

    const sourceField = screen.getByText("选定的会同时加进关联会议").closest(".form-field") as HTMLElement;
    expect(within(sourceField).getByText("▶ 00:09:36")).toBeInTheDocument();
    expect(within(sourceField).getByText("「那我有办法导出 excel 吗？是没有办法，」")).toBeInTheDocument();
    // 墙上预览也带上出自录音
    expect(screen.getByRole("article", { name: "需求：科室会预约后台导出" })).toHaveTextContent("原话 00:09:36");

    await userEvent.click(submitButton("创建"));
    expect(createRequirement).toHaveBeenCalledWith(
      expect.objectContaining({
        title: "科室会预约后台导出",
        project_id: CVM,
        source: { meeting_id: CVM_MEETING.id, quote: "那我有办法导出 excel 吗？是没有办法，", anchor_ms: 576900 },
      }),
    );
  });

  it("选来源时可以只关联这场会、不挑原话；清空来源后不带来源", async () => {
    const meetings = vi.fn().mockResolvedValue({ items: [CVM_MEETING], total: 1, limit: 30, offset: 0 });
    const meeting = vi.fn().mockResolvedValue({ id: CVM_MEETING.id, title: CVM_MEETING.title, segments: CVM_SEGMENTS });
    const createRequirement = vi.fn().mockResolvedValue({ id: "r", title: "t" });
    renderForm({ meetings, meeting, createRequirement }, { mode: "create", candidateId: undefined });

    await userEvent.click(screen.getByRole("button", { name: "＋ 选来源会议，再挑会上原话" }));
    const dialog = await screen.findByRole("dialog", { name: "选来源" });
    // 没选项目时列全部的会，会议行写项目
    expect(meetings).toHaveBeenLastCalledWith({ q: undefined, project_id: undefined, limit: 30, offset: 0 });
    await userEvent.click(await within(dialog).findByRole("button", { name: /CVM 云讲堂/ }));
    await within(dialog).findByRole("button", { name: /预约审核查看/ });
    await userEvent.click(within(dialog).getByRole("button", { name: "只关联这场会" }));

    const sourceField = screen.getByText("选定的会同时加进关联会议").closest(".form-field") as HTMLElement;
    expect(within(sourceField).getByText(CVM_MEETING.title)).toBeInTheDocument();
    expect(within(sourceField).queryByText(/▶/)).not.toBeInTheDocument();

    await userEvent.click(within(sourceField).getByRole("button", { name: "清空来源" }));
    expect(screen.getByRole("button", { name: "＋ 选来源会议，再挑会上原话" })).toBeInTheDocument();
  });

  it("从逐字稿选句进来（S10）：来源带上，所属项目随会议归属，可改；焦点在需求名上", async () => {
    const createRequirement = vi.fn().mockResolvedValue({ id: "requirement-export", title: "科室会预约后台导出" });
    const { id: _id, kind: _kind, via_candidate_title: _via, ...exportDraft } = EXPORT_SOURCE;
    renderForm(
      { createRequirement },
      {
        mode: "create",
        candidateId: undefined,
        prefill: {
          source: { ...exportDraft, quote: "那我有办法导出 excel 吗？是没有办法，" },
          projectId: CVM,
        },
      },
    );

    expect(screen.getByText(/录音档案 \/ 260929 云课堂直播运营问题对齐 \//)).toBeInTheDocument();
    expect(screen.getByText("来源已从逐字稿带入，补上需求名就能建。")).toBeInTheDocument();
    expect(projectSelect()).toHaveTextContent("CVM 云讲堂");
    expect(screen.getByText("随会议归属，可改")).toBeInTheDocument();
    expect(screen.getByText("来自逐字稿选句")).toBeInTheDocument();
    expect(submitButton("创建")).toBeDisabled();
    expect(screen.getByRole("textbox", { name: /需求名/ })).toHaveFocus();

    await userEvent.type(screen.getByRole("textbox", { name: /需求名/ }), "科室会预约后台导出");
    await userEvent.click(submitButton("创建"));
    expect(createRequirement).toHaveBeenCalledWith(
      expect.objectContaining({
        project_id: CVM,
        source: { meeting_id: EXPORT_SOURCE.meeting_id, quote: "那我有办法导出 excel 吗？是没有办法，", anchor_ms: 576900 },
      }),
    );
  });

  it("修改（S11）：读出需求填好，状态三态切换；来源没动时不传来源，合并进来的原话列在来源底下", async () => {
    const requirement = vi.fn().mockResolvedValue(jdDetail());
    const updated = jdDetail({ status: "done" });
    const updateRequirement = vi.fn().mockResolvedValue(updated);
    const handlers = renderForm({ requirement, updateRequirement }, { ...EDIT, canPickFolders: true });

    expect(await screen.findByDisplayValue("京东科研仓对接")).toBeInTheDocument();
    expect(requirement).toHaveBeenCalledWith("requirement-jd");
    expect(screen.getByRole("heading", { name: "修改需求" })).toBeInTheDocument();
    expect(screen.getByText(/需求池 \/ 京东科研仓对接 \//)).toBeInTheDocument();
    expect(screen.getByText("改完保存，墙上的海报会跟着变。")).toBeInTheDocument();
    expect(projectSelect()).toHaveTextContent("医米科研用药");
    expect(screen.getByText("对接京东科研仓")).toBeInTheDocument();
    const statuses = within(screen.getByRole("group", { name: "状态" }));
    expect(statuses.getAllByRole("button").map((button) => button.textContent)).toEqual(["进行中", "已完成", "已搁置"]);
    expect(statuses.getByRole("button", { name: "进行中" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByText("另有 1 句原话合并自候选「京东仓签收凭证」 · 00:31:49")).toBeInTheDocument();

    await userEvent.click(statuses.getByRole("button", { name: "已完成" }));
    await userEvent.click(submitButton("保存"));
    expect(updateRequirement).toHaveBeenCalledTimes(1);
    // 只提交改过的：状态；名字、说明、优先级、项目、文件夹、来源都没动，都不带
    expect(updateRequirement).toHaveBeenCalledWith("requirement-jd", { status: "done" });
    expect(handlers.onDone).toHaveBeenCalledWith({ kind: "edited", requirement: updated });
  });

  it("修改时清空来源就传 null（R04-5：来源可改），其余没动的不带", async () => {
    const updateRequirement = vi.fn().mockResolvedValue(jdDetail({ source: null }));
    renderEdit({ updateRequirement });
    await screen.findByDisplayValue("京东科研仓对接");

    await userEvent.click(screen.getByRole("button", { name: "清空来源" }));
    await userEvent.click(submitButton("保存"));
    expect(updateRequirement).toHaveBeenCalledWith("requirement-jd", { source: null });
  });
});

describe("RequirementFormPage 修改页把状态改成已完成、已搁置（D3、D10）", () => {
  const TASKS = [
    taskItem(),
    taskItem({ id: "task-fields", title: "整理四类接口的字段对照表", status: "pending_confirm" }),
    taskItem({ id: "task-kickoff", title: "约京东科研仓开对接启动会", status: "done" }),
  ];

  async function saveAs(label: "已完成" | "已搁置", api: Partial<ApiClient>) {
    const handlers = renderEdit({ requirement: vi.fn().mockResolvedValue(jdDetail({ tasks: TASKS, open_task_count: 2 })), ...api });
    await screen.findByDisplayValue("京东科研仓对接");
    await userEvent.click(within(screen.getByRole("group", { name: "状态" })).getByRole("button", { name: label }));
    await userEvent.click(submitButton("保存"));
    return handlers;
  }

  it("名下有没做完的待办：保存前同样弹「还有 N 条待办没做完」，［一起关掉］是默认，随 PATCH 发 close_open_tasks: true", async () => {
    const updated = jdDetail({ status: "done", closed_task_count: 2 });
    const updateRequirement = vi.fn().mockResolvedValue(updated);
    const handlers = await saveAs("已完成", { updateRequirement });

    const dialog = await screen.findByRole("alertdialog", { name: "还有 2 条待办没做完" });
    expect(within(dialog).getAllByRole("listitem").map((row) => row.textContent)).toEqual([
      "跟京东确认签收凭证怎么回传给医米",
      "整理四类接口的字段对照表",
    ]);
    expect(updateRequirement).not.toHaveBeenCalled();
    await waitFor(() => expect(within(dialog).getByRole("button", { name: "一起关掉" })).toHaveFocus());
    await userEvent.click(within(dialog).getByRole("button", { name: "一起关掉" }));

    expect(updateRequirement).toHaveBeenCalledWith("requirement-jd", { status: "done", close_open_tasks: true });
    await waitFor(() => expect(handlers.onDone).toHaveBeenCalledWith({ kind: "edited", requirement: updated }));
  });

  it("［待办留着］：发 close_open_tasks: false；和别的改动一起提交", async () => {
    const updateRequirement = vi.fn().mockResolvedValue(jdDetail({ status: "shelved" }));
    renderEdit({ requirement: vi.fn().mockResolvedValue(jdDetail({ tasks: TASKS, open_task_count: 2 })), updateRequirement });
    await screen.findByDisplayValue("京东科研仓对接");
    await userEvent.click(priorityButton("P1"));
    await userEvent.click(within(screen.getByRole("group", { name: "状态" })).getByRole("button", { name: "已搁置" }));
    await userEvent.click(submitButton("保存"));

    const dialog = await screen.findByRole("alertdialog", { name: "还有 2 条待办没做完" });
    await userEvent.click(within(dialog).getByRole("button", { name: "待办留着" }));
    expect(updateRequirement).toHaveBeenCalledWith("requirement-jd", {
      priority: "P1",
      status: "shelved",
      close_open_tasks: false,
    });
  });

  it("弹窗里点 ✕：不保存，留在修改页，改动还在", async () => {
    const updateRequirement = vi.fn();
    const handlers = await saveAs("已完成", { updateRequirement });
    const dialog = await screen.findByRole("alertdialog", { name: "还有 2 条待办没做完" });
    await userEvent.click(within(dialog).getByRole("button", { name: "关闭" }));

    expect(updateRequirement).not.toHaveBeenCalled();
    expect(handlers.onDone).not.toHaveBeenCalled();
    expect(within(screen.getByRole("group", { name: "状态" })).getByRole("button", { name: "已完成" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    expect(submitButton("保存")).toBeEnabled();
  });

  it("改回进行中、或者没改状态：不弹，也不带 close_open_tasks", async () => {
    const updateRequirement = vi.fn().mockResolvedValue(jdDetail());
    renderEdit({
      requirement: vi.fn().mockResolvedValue(jdDetail({ status: "done", tasks: TASKS, open_task_count: 2 })),
      updateRequirement,
    });
    await screen.findByDisplayValue("京东科研仓对接");
    await userEvent.click(within(screen.getByRole("group", { name: "状态" })).getByRole("button", { name: "进行中" }));
    await userEvent.click(submitButton("保存"));
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
    expect(updateRequirement).toHaveBeenCalledWith("requirement-jd", { status: "active" });
  });

  it("墙上预览跟着状态盖章，底栏写今天完成（北京日历）", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true, now: new Date("2026-10-08T17:30:00+00:00") });
    try {
      renderEdit();
      await screen.findByDisplayValue("京东科研仓对接");
      await userEvent.click(within(screen.getByRole("group", { name: "状态" })).getByRole("button", { name: "已完成" }));
      const preview = screen.getByRole("article", { name: "需求：京东科研仓对接" });
      expect(within(preview).getByText("已完成")).toHaveClass("poster__stamp");
      expect(within(preview).getByText("10-09").parentElement).toHaveTextContent("10-09 完成");
    } finally {
      vi.useRealTimers();
    }
  });
});

describe("RequirementFormPage 修改链接打开被并掉的需求（F3）", () => {
  it("显示「这条需求已并入「X」」和［去看］［返回］，不出空白表单", async () => {
    const onOpenRequirement = vi.fn();
    const requirement = vi.fn().mockRejectedValue(
      new ApiError("这条需求已并入「京东仓签收凭证」", 404, {
        detail: "这条需求已并入「京东仓签收凭证」",
        merged_into: { id: "requirement-receipt", title: "京东仓签收凭证" },
      }),
    );
    const handlers = renderEdit({ requirement }, { onOpenRequirement });

    expect(await screen.findByText("这条需求已并入「京东仓签收凭证」")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "修改需求" })).toBeInTheDocument();
    expect(screen.queryByRole("textbox", { name: /需求名/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "保存" })).not.toBeInTheDocument();
    expect(screen.queryByRole("article")).not.toBeInTheDocument();
    expect(handlers.onDirtyChange).not.toHaveBeenCalledWith(true);

    await userEvent.click(screen.getByRole("button", { name: "去看" }));
    expect(onOpenRequirement).toHaveBeenCalledWith("requirement-receipt");
    await userEvent.click(screen.getByRole("button", { name: "返回" }));
    expect(handlers.onCancel).toHaveBeenCalled();
  });

  it("别的读取失败照旧在表单上方写原因", async () => {
    renderEdit({ requirement: vi.fn().mockRejectedValue(new ApiError("需求不存在", 404, { detail: "需求不存在" })) });
    expect(await screen.findByRole("alert")).toHaveTextContent("需求不存在");
    expect(screen.queryByRole("button", { name: "去看" })).not.toBeInTheDocument();
  });
});

describe("RequirementFormPage 修改页只提交改过的字段（审查 B：两个标签页互相覆盖）", () => {
  it("另一个标签页刚改了名字，这边只点了 P1 就保存：只提交优先级，不会把名字改回原样", async () => {
    const updateRequirement = vi.fn().mockResolvedValue(jdDetail({ priority: "P1" }));
    renderEdit({ updateRequirement }, { canPickFolders: true });
    await screen.findByDisplayValue("京东科研仓对接");

    await userEvent.click(priorityButton("P1"));
    await userEvent.click(submitButton("保存"));
    expect(updateRequirement).toHaveBeenCalledWith("requirement-jd", { priority: "P1" });
  });

  it("改了哪几项就提交哪几项：名字、说明、文件夹各自单独算", async () => {
    const updateRequirement = vi.fn().mockResolvedValue(jdDetail());
    renderEdit({ updateRequirement }, { canPickFolders: true });
    const title = await screen.findByDisplayValue("京东科研仓对接");

    await userEvent.type(title, "（二期）");
    await userEvent.clear(screen.getByRole("textbox", { name: /说明/ }));
    await userEvent.type(screen.getByRole("textbox", { name: /说明/ }), "按四类接口对上，签收凭证还悬着。");
    await userEvent.click(screen.getByRole("button", { name: "移除" }));
    await userEvent.click(submitButton("保存"));

    expect(updateRequirement).toHaveBeenCalledWith("requirement-jd", {
      title: "京东科研仓对接（二期）",
      summary: "按四类接口对上，签收凭证还悬着。",
      folder_paths: [],
    });
  });

  it("一处都没改时保存置灰；改了又改回去，也回到置灰", async () => {
    renderEdit({}, { canPickFolders: true });
    const title = await screen.findByDisplayValue("京东科研仓对接");
    expect(submitButton("保存")).toBeDisabled();

    await userEvent.click(priorityButton("P3"));
    expect(submitButton("保存")).toBeEnabled();
    await userEvent.click(priorityButton("P0"));
    expect(submitButton("保存")).toBeDisabled();

    // 末尾多出的空白、零宽字符不算改动
    await userEvent.type(title, " ​");
    expect(submitButton("保存")).toBeDisabled();
  });

  it("换了所属项目：连同材料文件夹（清空，后端要求同批重选）一起提交", async () => {
    const updateRequirement = vi.fn().mockResolvedValue(jdDetail({ project_id: HENGRUI }));
    renderEdit({ updateRequirement }, { canPickFolders: true });
    await screen.findByDisplayValue("京东科研仓对接");

    await chooseProject(/恒瑞健康/);
    expect(screen.queryByText("对接京东科研仓")).not.toBeInTheDocument();
    await userEvent.click(submitButton("保存"));
    expect(updateRequirement).toHaveBeenCalledWith("requirement-jd", { project_id: HENGRUI, folder_paths: [] });
  });

  it("不能选文件夹的环境里换了项目：后端仍要求同批带上 folder_paths，带空数组", async () => {
    const updateRequirement = vi.fn().mockResolvedValue(jdDetail({ project_id: HENGRUI }));
    renderEdit({ updateRequirement }, { canPickFolders: false });
    await screen.findByDisplayValue("京东科研仓对接");

    await chooseProject(/恒瑞健康/);
    await userEvent.click(submitButton("保存"));
    expect(updateRequirement).toHaveBeenCalledWith("requirement-jd", { project_id: HENGRUI, folder_paths: [] });
  });

  it("换了项目再换回原项目：材料文件夹原样回来，保存时不再提交文件夹（审查：文件夹被清空）", async () => {
    const updateRequirement = vi.fn().mockResolvedValue(jdDetail({ priority: "P1" }));
    renderEdit({ updateRequirement }, { canPickFolders: true });
    await screen.findByDisplayValue("京东科研仓对接");
    expect(screen.getByText("对接京东科研仓")).toBeInTheDocument();

    await chooseProject(/恒瑞健康/);
    expect(screen.queryByText("对接京东科研仓")).not.toBeInTheDocument();
    await chooseProject(/医米科研用药/);
    expect(screen.getByText("对接京东科研仓")).toBeInTheDocument();
    // 绕了一圈什么都没变
    expect(submitButton("保存")).toBeDisabled();

    await userEvent.click(priorityButton("P1"));
    await userEvent.click(submitButton("保存"));
    expect(updateRequirement).toHaveBeenCalledWith("requirement-jd", { priority: "P1" });
  });

  it("换回原项目时，换走之前在原项目下移除过的文件夹仍是移除后的样子", async () => {
    const second = { ...JD_FOLDER, id: 2, name: "京东仓接口字段", path: "/Volumes/外置中枢/医朵云/医米科研用药/京东仓接口字段" };
    const updateRequirement = vi.fn().mockResolvedValue(jdDetail());
    renderEdit({ requirement: vi.fn().mockResolvedValue(jdDetail({ folders: [JD_FOLDER, second] })), updateRequirement }, { canPickFolders: true });
    await screen.findByDisplayValue("京东科研仓对接");

    await userEvent.click(screen.getAllByRole("button", { name: "移除" })[0]);
    await chooseProject(/恒瑞健康/);
    await chooseProject(/医米科研用药/);
    expect(screen.queryByText("对接京东科研仓")).not.toBeInTheDocument();
    expect(screen.getByText("京东仓接口字段")).toBeInTheDocument();

    await userEvent.click(submitButton("保存"));
    expect(updateRequirement).toHaveBeenCalledWith("requirement-jd", { folder_paths: [second.path] });
  });
});

describe("RequirementFormPage 需求名边填边查重（R04-9）", () => {
  const HENGTIAO = { id: "requirement-hengtiao", title: "赠药横跳拦截", status: "active" as const };

  it("需求名或所属项目变了，停 300ms 才查：参数是项目、需求名；新增时不传 exclude_id", async () => {
    const { client } = renderForm({}, { mode: "create", candidateId: undefined });

    await userEvent.type(screen.getByRole("textbox", { name: /需求名/ }), "赠药横跳拦截");
    await chooseProject(/医米科研用药/);
    const lastChangeAt = performance.now();
    // 没停够 300ms：还没查
    expect(client.requirementTitleCheck).not.toHaveBeenCalled();
    await waitFor(() => expect(client.requirementTitleCheck).toHaveBeenCalledTimes(1));
    expect(performance.now() - lastChangeAt).toBeGreaterThanOrEqual(250);
    expect(client.requirementTitleCheck).toHaveBeenCalledWith(YIMI, "赠药横跳拦截", undefined);

    // 换项目也重查
    await chooseProject(/恒瑞健康/);
    await waitFor(() => expect(client.requirementTitleCheck).toHaveBeenCalledTimes(2));
    expect(client.requirementTitleCheck).toHaveBeenLastCalledWith(HENGRUI, "赠药横跳拦截", undefined);
  });

  it("没填需求名、没选项目时不查；只含零宽字符的需求名发给后端的是清理后的（空，不查）", async () => {
    const { client } = renderForm({}, { mode: "create", candidateId: undefined });

    await chooseProject(/医米科研用药/);
    await userEvent.type(screen.getByRole("textbox", { name: /需求名/ }), "​ ");
    await new Promise((resolve) => setTimeout(resolve, 450));
    expect(client.requirementTitleCheck).not.toHaveBeenCalled();

    await userEvent.type(screen.getByRole("textbox", { name: /需求名/ }), "赠药​横跳拦截");
    await waitFor(() => expect(client.requirementTitleCheck).toHaveBeenCalledTimes(1));
    expect(client.requirementTitleCheck).toHaveBeenCalledWith(YIMI, "赠药横跳拦截", undefined);
  });

  it("撞名：需求名红框，下方红字指出撞的是哪一条，创建置灰；改名后恢复（S11-b）", async () => {
    const check = vi.fn().mockImplementation(async (_project: string, title: string) => ({
      existing: title === "赠药横跳拦截" ? HENGTIAO : null,
    }));
    renderForm({ requirementTitleCheck: check }, { mode: "create", candidateId: undefined });
    const title = screen.getByRole("textbox", { name: /需求名/ });

    await chooseProject(/医米科研用药/);
    await userEvent.type(title, "赠药横跳拦截");
    expect(await screen.findByText(/里已经有一条叫/)).toHaveTextContent("医米科研用药里已经有一条叫「赠药横跳拦截」的需求");
    expect(title).toHaveAttribute("aria-invalid", "true");
    expect(title).toHaveClass("is-invalid");
    expect(screen.getByRole("alert")).toHaveTextContent("医米科研用药里已经有一条叫「赠药横跳拦截」的需求");
    expect(submitButton("创建")).toBeDisabled();
    // 新增页没有「合并」入口，也不重复写一句通用文字
    expect(screen.queryByRole("button", { name: "改为合并到这条需求" })).not.toBeInTheDocument();

    await userEvent.type(title, "（二期）");
    await waitFor(() => expect(screen.queryByText(/里已经有一条叫/)).not.toBeInTheDocument());
    expect(title).not.toHaveAttribute("aria-invalid");
    await waitFor(() => expect(submitButton("创建")).toBeEnabled());
  });

  it("换到别的项目：同名不撞了，提示和置灰一起收起", async () => {
    const check = vi.fn().mockImplementation(async (project: string) => ({ existing: project === YIMI ? HENGTIAO : null }));
    renderForm({ requirementTitleCheck: check }, { mode: "create", candidateId: undefined });

    await chooseProject(/医米科研用药/);
    await userEvent.type(screen.getByRole("textbox", { name: /需求名/ }), "赠药横跳拦截");
    await screen.findByText(/里已经有一条叫/);

    await chooseProject(/恒瑞健康/);
    // 项目一换，上一个项目的查重结果立刻作废，不用等新结果
    expect(screen.queryByText(/里已经有一条叫/)).not.toBeInTheDocument();
    await waitFor(() => expect(check).toHaveBeenLastCalledWith(HENGRUI, "赠药横跳拦截", undefined));
    expect(screen.queryByText(/里已经有一条叫/)).not.toBeInTheDocument();
  });

  it("修改：只在名字或项目动过以后查，传本需求的 id 不和自己比；撞名时保存置灰", async () => {
    const check = vi.fn().mockImplementation(async (_project: string, title: string) => ({
      existing: title === "赠药横跳拦截" ? HENGTIAO : null,
    }));
    renderEdit({ requirementTitleCheck: check });
    const title = await screen.findByDisplayValue("京东科研仓对接");
    await new Promise((resolve) => setTimeout(resolve, 450));
    expect(check).not.toHaveBeenCalled();

    await userEvent.clear(title);
    await userEvent.type(title, "赠药横跳拦截");
    expect(await screen.findByText(/里已经有一条叫/)).toHaveTextContent("医米科研用药里已经有一条叫「赠药横跳拦截」的需求");
    expect(check).toHaveBeenLastCalledWith(YIMI, "赠药横跳拦截", "requirement-jd");
    expect(submitButton("保存")).toBeDisabled();
  });

  it("查重请求本身失败：不拦保存", async () => {
    const createRequirement = vi.fn().mockResolvedValue({ id: "r", title: "t" });
    const check = vi.fn().mockRejectedValue(new Error("查重接口 500"));
    renderForm({ requirementTitleCheck: check, createRequirement }, { mode: "create", candidateId: undefined });

    await chooseProject(/医米科研用药/);
    await userEvent.type(screen.getByRole("textbox", { name: /需求名/ }), "赠药横跳拦截");
    await waitFor(() => expect(check).toHaveBeenCalled());
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(submitButton("创建")).toBeEnabled();
    await userEvent.click(submitButton("创建"));
    expect(createRequirement).toHaveBeenCalledTimes(1);
  });

  it("保存时才撞名（409 带 existing）：新建和修改同样标在需求名下方，不只是底栏一句通用文字", async () => {
    const conflict = new ApiError("同项目已有同名需求", 409, { detail: "同项目已有同名需求", existing: HENGTIAO });
    const createRequirement = vi.fn().mockRejectedValue(conflict);
    const first = renderForm({ createRequirement }, { mode: "create", candidateId: undefined });
    await chooseProject(/医米科研用药/);
    await userEvent.type(screen.getByRole("textbox", { name: /需求名/ }), "赠药横跳拦截");
    await userEvent.click(submitButton("创建"));
    expect(await screen.findByText(/里已经有一条叫/)).toHaveTextContent("医米科研用药里已经有一条叫「赠药横跳拦截」的需求");
    expect(screen.getByRole("textbox", { name: /需求名/ })).toHaveAttribute("aria-invalid", "true");
    expect(screen.queryByText("同项目已有同名需求")).not.toBeInTheDocument();
    expect(first.onDone).not.toHaveBeenCalled();
    expect(submitButton("创建")).toBeDisabled();
    expect(createRequirement).toHaveBeenCalledTimes(1);
  });

  it("修改保存时撞名（409 带 existing）：标在需求名下方", async () => {
    const updateRequirement = vi
      .fn()
      .mockRejectedValue(new ApiError("同项目已有同名需求", 409, { detail: "同名", existing: HENGTIAO }));
    renderEdit({ updateRequirement });
    const title = await screen.findByDisplayValue("京东科研仓对接");

    await userEvent.clear(title);
    await userEvent.type(title, "赠药横跳拦截");
    // 边填边查那一路这次没查到（别处刚建了同名的），保存时 409 才发现
    await userEvent.click(submitButton("保存"));
    expect(await screen.findByText(/里已经有一条叫/)).toHaveTextContent("医米科研用药里已经有一条叫「赠药横跳拦截」的需求");
    expect(submitButton("保存")).toBeDisabled();
  });

  it("保存时 409 比更早发出去、还在路上的查重结果新：旧的查重回来也盖不掉撞名提示", async () => {
    let answerEarlyCheck: (value: { existing: null }) => void = () => undefined;
    const check = vi.fn().mockReturnValue(
      new Promise((resolve) => {
        answerEarlyCheck = resolve;
      }),
    );
    const createRequirement = vi.fn().mockRejectedValue(new ApiError("同名", 409, { detail: "同名", existing: HENGTIAO }));
    renderForm({ requirementTitleCheck: check, createRequirement }, { mode: "create", candidateId: undefined });
    await chooseProject(/医米科研用药/);
    await userEvent.type(screen.getByRole("textbox", { name: /需求名/ }), "赠药横跳拦截");
    // 查重请求发出去了，还没回来；这时保存，后端说撞名
    await waitFor(() => expect(check).toHaveBeenCalledTimes(1));
    await userEvent.click(submitButton("创建"));
    expect(await screen.findByText(/里已经有一条叫/)).toBeInTheDocument();

    // 更早发出去的查重这时回来，说「没撞名」：已经过时，不能把提示盖掉
    answerEarlyCheck({ existing: null });
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(screen.getByText(/里已经有一条叫/)).toBeInTheDocument();
    expect(submitButton("创建")).toBeDisabled();
  });

  it("认领页：候选名一读回来就查，撞名时红字、认领置灰，仍有「改为合并到这条需求」", async () => {
    const existing = { id: "requirement-jd", title: "京东仓签收凭证", status: "active" as const };
    const check = vi.fn().mockResolvedValue({ existing });
    const mergeCandidate = vi.fn().mockResolvedValue({ id: existing.id, title: existing.title });
    const handlers = renderForm({
      requirementCandidate: vi.fn().mockResolvedValue(candidateDetail()),
      requirementTitleCheck: check,
      mergeCandidate,
    });

    expect(await screen.findByText(/里已经有一条叫/)).toHaveTextContent("医米科研用药里已经有一条叫「京东仓签收凭证」的需求");
    expect(check).toHaveBeenCalledWith(YIMI, "京东仓签收凭证", undefined);
    expect(submitButton("认领")).toBeDisabled();

    await userEvent.click(screen.getByRole("button", { name: "改为合并到这条需求" }));
    expect(mergeCandidate).toHaveBeenCalledWith("candidate-receipt", "requirement-jd", YIMI);
    expect(handlers.onDone).toHaveBeenCalledWith(expect.objectContaining({ kind: "merged" }));
  });
});

describe("RequirementFormPage 所属项目下拉（R01-12、S02-b）", () => {
  it("按「我的方向」座次列在前，未排座次的按最近一场会由近到远，一场会都没有的在最后", async () => {
    renderForm({ requirementCandidate: vi.fn().mockResolvedValue(candidateDetail()) }, { projects: ALL_PROJECTS });
    await screen.findByDisplayValue("京东仓签收凭证");

    await userEvent.click(projectSelect());
    const list = screen.getByRole("listbox", { name: "项目" });
    expect(within(list).getAllByRole("option").map((option) => option.textContent)).toEqual([
      "1医米科研用药✓",
      "2恒瑞健康",
      "3华夏基金会科普同行",
      "CVM 云讲堂",
      "安心四季",
      "项目复制与名单一键转移",
      "口服药到店领取配置方案",
      "寻呼随访项目",
    ]);
    expect(within(list).getByText("我的方向")).toBeInTheDocument();
    expect(within(list).getByText("未排座次 · 按最近会议排")).toBeInTheDocument();
    expect(within(list).getByRole("option", { name: /医米科研用药/ })).toHaveAttribute("aria-selected", "true");
  });

  it("项目多到要滚动时，底下提醒一句共几个、可以搜；搜索时不显示", async () => {
    const more: Project[] = Array.from({ length: 4 }, (_, index) => ({
      id: `project-extra-${index}`,
      name: `示意项目 ${index + 1}`,
      color: "#2c8d83",
      seat: null,
      latest_meeting_date: null,
    }));
    renderForm({ requirementCandidate: vi.fn().mockResolvedValue(candidateDetail()) }, { projects: [...ALL_PROJECTS, ...more] });
    await screen.findByDisplayValue("京东仓签收凭证");

    await userEvent.click(projectSelect());
    expect(screen.getByText("共 12 个项目，滚动查看或输入名字搜索")).toBeInTheDocument();
    await userEvent.type(screen.getByRole("textbox", { name: "搜项目" }), "示意");
    expect(screen.queryByText(/个项目，滚动查看/)).not.toBeInTheDocument();
  });

  it("项目不多时没有这句提醒", async () => {
    renderForm({ requirementCandidate: vi.fn().mockResolvedValue(candidateDetail()) }, { projects: ALL_PROJECTS });
    await screen.findByDisplayValue("京东仓签收凭证");

    await userEvent.click(projectSelect());
    expect(screen.queryByText(/个项目，滚动查看/)).not.toBeInTheDocument();
  });

  it("没有最近会议时间的未排座次项目按名字排", () => {
    const quiet = [
      { id: PAGER, name: "寻呼随访项目", color: "#3f51b5", seat: null, latest_meeting_date: null },
      { id: HUAXIA, name: "华夏基金会科普同行", color: "#2c8d83", seat: null, latest_meeting_date: null },
    ];
    expect(splitProjects(quiet).others.map((project) => project.name)).toEqual(["华夏基金会科普同行", "寻呼随访项目"]);
    expect(splitProjects(quiet).seated).toEqual([]);
  });

  it("能按名字搜；搜不到时说一句；回车选第一个匹配的，选完收起、焦点回到下拉上", async () => {
    renderForm({ requirementCandidate: vi.fn().mockResolvedValue(candidateDetail()) }, { projects: ALL_PROJECTS });
    await screen.findByDisplayValue("京东仓签收凭证");

    await userEvent.click(projectSelect());
    const search = screen.getByRole("textbox", { name: "搜项目" });
    expect(search).toHaveFocus();
    await userEvent.type(search, "项目");
    expect(screen.getAllByRole("option").map((option) => option.textContent)).toEqual(["项目复制与名单一键转移", "寻呼随访项目"]);
    await userEvent.clear(search);
    await userEvent.type(search, "没有这个项目");
    expect(screen.queryAllByRole("option")).toHaveLength(0);
    expect(screen.getByText("没有匹配的项目")).toBeInTheDocument();

    await userEvent.clear(search);
    await userEvent.type(search, "华夏{Enter}");
    expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
    expect(projectSelect()).toHaveTextContent("华夏基金会科普同行");
    expect(projectSelect()).toHaveFocus();
  });

  it("方向键移动高亮、回车选中；Esc 收起不改选择；点到外面也收起", async () => {
    renderForm({ requirementCandidate: vi.fn().mockResolvedValue(candidateDetail()) }, { projects: ALL_PROJECTS });
    await screen.findByDisplayValue("京东仓签收凭证");

    await userEvent.click(projectSelect());
    // 一打开高亮在当前选中的医米上；往下两格是「华夏基金会科普同行」
    await userEvent.keyboard("{ArrowDown}{ArrowDown}{Enter}");
    expect(projectSelect()).toHaveTextContent("华夏基金会科普同行");

    await userEvent.click(projectSelect());
    await userEvent.keyboard("{Escape}");
    expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
    expect(projectSelect()).toHaveTextContent("华夏基金会科普同行");

    await userEvent.click(projectSelect());
    expect(screen.getByRole("listbox")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("heading", { name: "认领候选" }));
    expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
  });

  it("下面放不下时往上展开，列表高度跟着剩下的地方走；地方够就往下、列表最高 320", async () => {
    const innerHeight = window.innerHeight;
    const rectAt = (top: number, bottom: number) => ({ top, bottom, left: 0, right: 300, width: 300, height: bottom - top, x: 0, y: top, toJSON: () => ({}) });
    try {
      renderForm({ requirementCandidate: vi.fn().mockResolvedValue(candidateDetail()) }, { projects: ALL_PROJECTS });
      await screen.findByDisplayValue("京东仓签收凭证");
      const trigger = projectSelect();

      // 窗口 400 高、字段在 160～200：下面剩 108、上面剩 148，都放不下 260，取地方大的上面；列表最矮 140
      Object.defineProperty(window, "innerHeight", { configurable: true, value: 400 });
      vi.spyOn(trigger, "getBoundingClientRect").mockReturnValue(rectAt(160, 200));
      await userEvent.click(trigger);
      expect(document.querySelector(".project-select__menu")).toHaveAttribute("data-side", "above");
      expect(screen.getByRole("listbox", { name: "项目" })).toHaveStyle({ maxHeight: "140px" });
      await userEvent.keyboard("{Escape}");

      // 窗口 900 高、字段在 200～244：下面剩 564，往下，列表最高 320
      Object.defineProperty(window, "innerHeight", { configurable: true, value: 900 });
      vi.spyOn(trigger, "getBoundingClientRect").mockReturnValue(rectAt(200, 244));
      await userEvent.click(trigger);
      expect(document.querySelector(".project-select__menu")).toHaveAttribute("data-side", "below");
      expect(screen.getByRole("listbox", { name: "项目" })).toHaveStyle({ maxHeight: "320px" });
    } finally {
      Object.defineProperty(window, "innerHeight", { configurable: true, value: innerHeight });
    }
  });

  it("Tab 把焦点带出下拉就收起，选项还没点到时不会因为失焦被收掉", async () => {
    renderForm({ requirementCandidate: vi.fn().mockResolvedValue(candidateDetail()) }, { projects: ALL_PROJECTS });
    await screen.findByDisplayValue("京东仓签收凭证");

    await userEvent.click(projectSelect());
    expect(screen.getByRole("listbox")).toBeInTheDocument();
    await userEvent.tab();
    expect(screen.queryByRole("listbox")).not.toBeInTheDocument();

    // 点选项时，输入框先失焦（Safari 里目标为空）、click 才到：这一下不能把下拉收掉
    await userEvent.click(projectSelect());
    const option = screen.getByRole("option", { name: /恒瑞健康/ });
    fireEvent.blur(screen.getByRole("textbox", { name: "搜项目" }), { relatedTarget: null });
    expect(screen.getByRole("listbox")).toBeInTheDocument();
    await userEvent.click(option);
    expect(projectSelect()).toHaveTextContent("恒瑞健康");
  });

  it("候选是未归项目时下拉里没有选中项，必须先选一个才能认领", async () => {
    renderForm({ requirementCandidate: vi.fn().mockResolvedValue(candidateDetail({ project_id: null, project_name: null })) });
    await screen.findByDisplayValue("京东仓签收凭证");

    expect(projectSelect()).toHaveTextContent("选择项目");
    expect(submitButton("认领")).toBeDisabled();
    await chooseProject(/医米科研用药/);
    expect(submitButton("认领")).toBeEnabled();
  });
});

describe("RequirementFormPage 未保存的改动（审查 B1）", () => {
  it("没有改动：刷新、关页面不拦，点取消直接走", async () => {
    const confirm = vi.spyOn(window, "confirm");
    const handlers = renderForm({}, { mode: "create", candidateId: undefined });

    expect(fireBeforeUnload().defaultPrevented).toBe(false);
    await userEvent.click(screen.getByRole("button", { name: "取消" }));
    expect(confirm).not.toHaveBeenCalled();
    expect(handlers.onCancel).toHaveBeenCalledTimes(1);
  });

  it("有改动：刷新、关页面要拦；点取消先问「放弃这些修改并离开吗？」，不放弃就留在页面上", async () => {
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
    const handlers = renderForm({}, { mode: "create", candidateId: undefined });

    await userEvent.type(screen.getByRole("textbox", { name: /需求名/ }), "科室会预约后台导出");
    expect(fireBeforeUnload().defaultPrevented).toBe(true);

    await userEvent.click(screen.getByRole("button", { name: "取消" }));
    expect(confirm).toHaveBeenCalledWith("当前需求仍有未保存修改。放弃这些修改并离开吗？");
    expect(LEAVE_FORM_CONFIRM).toBe("当前需求仍有未保存修改。放弃这些修改并离开吗？");
    expect(handlers.onCancel).not.toHaveBeenCalled();
    expect(screen.getByDisplayValue("科室会预约后台导出")).toBeInTheDocument();

    confirm.mockReturnValue(true);
    await userEvent.click(screen.getByRole("button", { name: "取消" }));
    expect(handlers.onCancel).toHaveBeenCalledTimes(1);
  });

  it("认领、修改页改过也一样；改回原样就不算改动", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(false);
    const claim = renderForm({ requirementCandidate: vi.fn().mockResolvedValue(candidateDetail()) });
    await screen.findByDisplayValue("京东仓签收凭证");
    expect(fireBeforeUnload().defaultPrevented).toBe(false);
    await userEvent.click(priorityButton("P1"));
    expect(fireBeforeUnload().defaultPrevented).toBe(true);
    await userEvent.click(priorityButton("P2"));
    expect(fireBeforeUnload().defaultPrevented).toBe(false);
    expect(claim.onDirtyChange.mock.calls.map(([dirty]) => dirty)).toEqual([false, true, false]);
  });

  it("修改页改过：拦刷新，取消要确认", async () => {
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
    const handlers = renderEdit();
    await screen.findByDisplayValue("京东科研仓对接");
    expect(fireBeforeUnload().defaultPrevented).toBe(false);

    await userEvent.click(priorityButton("P3"));
    expect(fireBeforeUnload().defaultPrevented).toBe(true);
    await userEvent.click(screen.getByRole("button", { name: "取消" }));
    expect(confirm).toHaveBeenCalledTimes(1);
    expect(handlers.onCancel).not.toHaveBeenCalled();
  });

  it("从逐字稿选句进来、还没动手：来源和项目是带进来的，不算未保存的改动", async () => {
    const { id: _id, kind: _kind, via_candidate_title: _via, ...exportDraft } = EXPORT_SOURCE;
    const handlers = renderForm(
      {},
      { mode: "create", candidateId: undefined, prefill: { source: exportDraft, projectId: CVM } },
    );

    expect(fireBeforeUnload().defaultPrevented).toBe(false);
    expect(handlers.onDirtyChange).not.toHaveBeenCalledWith(true);
    await userEvent.type(screen.getByRole("textbox", { name: /需求名/ }), "科");
    expect(handlers.onDirtyChange).toHaveBeenLastCalledWith(true);
  });

  it("改动状态一变就报给 App；保存成功时先报「没有」再 onDone，App 随后跳转读到的不再是脏的", async () => {
    const createRequirement = vi.fn().mockResolvedValue({ id: "r", title: "t" });
    const handlers = renderForm({ createRequirement }, { mode: "create", candidateId: undefined });
    // 把两个回调的先后顺序记下来（回调里直接断言会被保存流程的 try/catch 吞掉）
    const order: string[] = [];
    handlers.onDirtyChange.mockImplementation((dirty: boolean) => order.push(`dirty:${dirty}`));
    handlers.onDone.mockImplementation(() => order.push("done"));

    await chooseProject(/医米科研用药/);
    await userEvent.type(screen.getByRole("textbox", { name: /需求名/ }), "赠药横跳拦截");
    await waitFor(() => expect(submitButton("创建")).toBeEnabled());
    expect(order).toEqual(["dirty:true"]);

    await userEvent.click(submitButton("创建"));
    expect(order).toEqual(["dirty:true", "dirty:false", "done"]);
  });

  it("点取消确认放弃：先报「没有」再退出", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(true);
    const view = renderForm({}, { mode: "create", candidateId: undefined });
    const order: string[] = [];
    view.onDirtyChange.mockImplementation((dirty: boolean) => order.push(`dirty:${dirty}`));
    view.onCancel.mockImplementation(() => order.push("cancel"));
    await userEvent.type(screen.getByRole("textbox", { name: /需求名/ }), "科");

    await userEvent.click(screen.getByRole("button", { name: "取消" }));
    expect(order).toEqual(["dirty:true", "dirty:false", "cancel"]);
  });

  it("页面卸载时也报一次「没有」，不在 App 里留下过期的脏标记", async () => {
    const onDirtyChange = vi.fn();
    const client = { requirementTitleCheck: vi.fn().mockResolvedValue({ existing: null }) } as unknown as ApiClient;
    const { unmount } = render(
      <RequirementFormPage
        apiClient={client}
        canPickFolders={false}
        mode="create"
        onCancel={vi.fn()}
        onDirtyChange={onDirtyChange}
        onDone={vi.fn()}
        projects={PROJECTS}
      />,
    );
    await userEvent.type(screen.getByRole("textbox", { name: /需求名/ }), "科");
    expect(onDirtyChange).toHaveBeenLastCalledWith(true);

    unmount();
    expect(onDirtyChange).toHaveBeenLastCalledWith(false);
  });

  it("没传 onDirtyChange 时照常工作", async () => {
    render(
      <RequirementFormPage
        apiClient={{ requirementTitleCheck: vi.fn().mockResolvedValue({ existing: null }) } as unknown as ApiClient}
        canPickFolders={false}
        mode="create"
        onCancel={vi.fn()}
        onDone={vi.fn()}
        projects={PROJECTS}
      />,
    );
    await userEvent.type(screen.getByRole("textbox", { name: /需求名/ }), "科");
    expect(fireBeforeUnload().defaultPrevented).toBe(true);
  });
});

describe("RequirementFormPage 进页面时的焦点（审查 B5）", () => {
  it("新增：焦点在需求名输入框上", () => {
    renderForm({}, { mode: "create", candidateId: undefined });
    expect(screen.getByRole("textbox", { name: /需求名/ })).toHaveFocus();
  });

  it("认领、修改：数据读回来、输入框能用了，焦点放到需求名上", async () => {
    renderForm({ requirementCandidate: vi.fn().mockResolvedValue(candidateDetail()) });
    // 焦点是数据读回来那次渲染之后的 effect 里才放上去的，机器一忙断言会早一拍
    const title = await screen.findByDisplayValue("京东仓签收凭证");
    await waitFor(() => expect(title).toHaveFocus());
  });

  it("修改页读回来以后焦点也在需求名上", async () => {
    renderEdit();
    const title = await screen.findByDisplayValue("京东科研仓对接");
    await waitFor(() => expect(title).toHaveFocus());
  });

  it("候选已经被处理过：输入框是灰的，不抢焦点", async () => {
    renderForm({
      requirementCandidate: vi.fn().mockResolvedValue(candidateDetail({ status: "claimed", requirement_id: "requirement-jd" })),
    });
    const title = await screen.findByDisplayValue("京东仓签收凭证");
    expect(title).toBeDisabled();
    expect(title).not.toHaveFocus();
  });
});

describe("RequirementFormPage 空名（审查 B12）", () => {
  it("需求名只有零宽字符或空白：和后端一样当空名，创建置灰", async () => {
    renderForm({}, { mode: "create", candidateId: undefined });
    await chooseProject(/医米科研用药/);
    const title = screen.getByRole("textbox", { name: /需求名/ });

    await userEvent.type(title, "​‍⁠");
    expect(submitButton("创建")).toBeDisabled();
    await userEvent.clear(title);
    await userEvent.type(title, "   ");
    expect(submitButton("创建")).toBeDisabled();
    await userEvent.type(title, "赠药横跳拦截");
    expect(submitButton("创建")).toBeEnabled();
  });

  it("需求名里夹着零宽字符：发给后端的是去掉以后的，墙上预览也是", async () => {
    const createRequirement = vi.fn().mockResolvedValue({ id: "r", title: "t" });
    renderForm({ createRequirement }, { mode: "create", candidateId: undefined });
    await chooseProject(/医米科研用药/);

    await userEvent.type(screen.getByRole("textbox", { name: /需求名/ }), "赠药​横跳拦截​");
    expect(screen.getByRole("article", { name: "需求：赠药横跳拦截" })).toBeInTheDocument();
    await userEvent.click(submitButton("创建"));
    expect(createRequirement).toHaveBeenCalledWith(expect.objectContaining({ title: "赠药横跳拦截" }));
  });

  it("认领时把 AI 预填的名字改成只剩空白：不能认领", async () => {
    renderForm({ requirementCandidate: vi.fn().mockResolvedValue(candidateDetail()) });
    const title = await screen.findByDisplayValue("京东仓签收凭证");

    await userEvent.clear(title);
    await userEvent.type(title, "​");
    expect(submitButton("认领")).toBeDisabled();
  });

  it("cleanTitle 和后端 clean_title 一致：不换行空格、全角空格统一成一个普通空格，连着的空格并成一个", () => {
    expect(cleanTitle("EDC\u00a0系统选型")).toBe("EDC 系统选型");
    expect(cleanTitle("EDC\u3000系统选型")).toBe("EDC 系统选型");
    expect(cleanTitle("EDC  系统选型")).toBe("EDC 系统选型");
    expect(cleanTitle("\u00a0科室会预约\u00a0后台导出\u3000")).toBe("科室会预约 后台导出");
  });

  it("cleanTitle 和后端 clean_title 一致：去掉 Unicode Cf 类字符，再去首尾空白", () => {
    expect(cleanTitle("​ 赠药‍横跳拦截⁠ ")).toBe("赠药横跳拦截");
    expect(cleanTitle("﻿")).toBe("");
    expect(cleanTitle("  A B  ")).toBe("A B");
  });
});

describe("RequirementFormPage 候选在别处处理了", () => {
  it("认领时候选已在别处合并：失败原因写在底部操作栏，重读后显示它已合并，能打开那条需求", async () => {
    const requirementCandidate = vi
      .fn()
      .mockResolvedValueOnce(candidateDetail())
      .mockResolvedValue(candidateDetail({ status: "merged", requirement_id: "requirement-jd", source: null, sources: [] }));
    const claimCandidate = vi.fn().mockRejectedValue(new ApiError("这条候选已经合并了", 409, { detail: "这条候选已经合并了" }));
    const onOpenRequirement = vi.fn();
    renderForm({ requirementCandidate, claimCandidate }, { onOpenRequirement });
    await screen.findByDisplayValue("京东仓签收凭证");

    await userEvent.click(submitButton("认领"));

    const footerAlert = await screen.findByText("这条候选已经合并了");
    expect(footerAlert.closest(".form-actions")).not.toBeNull();
    expect(await screen.findByText("这条候选已经合并了。")).toBeInTheDocument();
    expect(screen.getByText("来源已经跟着候选挂到需求上了")).toBeInTheDocument();
    expect(submitButton("认领")).toBeDisabled();
    await userEvent.click(screen.getByRole("button", { name: "打开那条需求" }));
    expect(onOpenRequirement).toHaveBeenCalledWith("requirement-jd");
  });

  it("认领时候选已经不在了（纪要重抽换掉了）：说人话，不露内部 id", async () => {
    renderForm({
      requirementCandidate: vi.fn().mockResolvedValue(candidateDetail()),
      claimCandidate: vi.fn().mockRejectedValue(new ApiError("候选不存在：candidate-476af978", 404)),
    });
    await screen.findByDisplayValue("京东仓签收凭证");

    await userEvent.click(submitButton("认领"));
    const alert = await screen.findByText(/这条候选已经不在了/);
    expect(alert).toHaveTextContent("回需求池看看新的候选");
    expect(alert).not.toHaveTextContent("candidate-476af978");
  });

  it("读不到候选时来源一栏不再一直写「正在读取」", async () => {
    renderForm({ requirementCandidate: vi.fn().mockRejectedValue(new Error("候选不存在：candidate-x")) });

    expect(await screen.findByText("读不到这条候选")).toBeInTheDocument();
    expect(screen.queryByText("正在读取…")).not.toBeInTheDocument();
  });
});
