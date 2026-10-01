import { fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { ApiError, type ApiClient } from "../../api";
import type { CandidateDetail, MeetingSummary, Project, RequirementDetail, Segment } from "../../types";
import { candidateItem, CVM, EXPORT_SOURCE, HENGRUI, JD_SOURCE, RECEIPT_SOURCE, YIMI } from "./poolFixtures";
import { RequirementFormPage, summaryLength } from "./RequirementFormPage";
import { quoteOf } from "./SourcePickerDialog";

const PROJECTS: Project[] = [
  { id: CVM, name: "CVM 云讲堂", color: "#5090ff", seat: null },
  { id: HENGRUI, name: "恒瑞健康", color: "#549c8a", seat: 2 },
  { id: YIMI, name: "医米科研用药", color: "#2c8d83", seat: 1 },
];

function candidateDetail(overrides: Partial<CandidateDetail> = {}): CandidateDetail {
  return { ...candidateItem(), status: "pending", sources: [RECEIPT_SOURCE], ...overrides };
}

function renderForm(
  api: Partial<ApiClient>,
  props: Partial<Parameters<typeof RequirementFormPage>[0]> = {},
) {
  const handlers = { onCancel: vi.fn(), onDone: vi.fn() };
  render(
    <RequirementFormPage
      apiClient={api as ApiClient}
      canPickFolders={false}
      candidateId="candidate-receipt"
      mode="claim"
      projects={PROJECTS}
      {...handlers}
      {...props}
    />,
  );
  return handlers;
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
    folders: [
      {
        id: 1,
        name: "对接京东科研仓",
        path: "/Volumes/外置中枢/医朵云/医米科研用药/对接京东科研仓",
        exists: true,
        file_count: 2,
        file_count_capped: false,
        modified_at: "2026-09-14T08:00:00+00:00",
        preview_files: [],
      },
    ],
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

function submitButton(name: "认领" | "创建" | "保存") {
  return screen.getByRole("button", { name });
}

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

    const select = screen.getByRole("combobox", { name: /所属项目/ });
    expect(select).toHaveValue(YIMI);
    // 排了座次的按名次在前，其余按名字
    expect(within(select).getAllByRole("option").map((option) => option.textContent)).toEqual([
      "选择项目",
      "1  医米科研用药",
      "2  恒瑞健康",
      "CVM 云讲堂",
    ]);

    const sourceField = screen.getByText("出自会议纪要，不可改").closest(".form-field") as HTMLElement;
    expect(within(sourceField).getByText("260916 医米京东科研仓系统对接")).toBeInTheDocument();
    expect(within(sourceField).getByText("09-17 10:01 · 51 分钟")).toBeInTheDocument();
    expect(within(sourceField).getByText("▶ 00:31:49")).toBeInTheDocument();
    expect(within(sourceField).getByText(`「${RECEIPT_SOURCE.quote}」`)).toBeInTheDocument();
    expect(within(sourceField).queryByRole("textbox")).not.toBeInTheDocument();

    await userEvent.type(title, "与对账");
    await userEvent.click(within(screen.getByRole("group", { name: "优先级" })).getByRole("button", { name: "P1" }));
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

  it("认领时撞上同项目的同名需求：不新建，改为合并到那一条（按页面上改选的项目）", async () => {
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

    await userEvent.selectOptions(screen.getByRole("combobox", { name: /所属项目/ }), HENGRUI);
    await userEvent.click(submitButton("认领"));

    const alert = await screen.findByText(/里已有同名需求/);
    expect(alert).toHaveTextContent("「恒瑞健康」里已有同名需求「京东仓签收凭证」（已搁置），不会重复新建。");
    expect(handlers.onDone).not.toHaveBeenCalled();

    await userEvent.click(screen.getByRole("button", { name: "改为合并到这条需求" }));
    expect(mergeCandidate).toHaveBeenCalledWith("candidate-receipt", "requirement-hr-receipt", HENGRUI);
    expect(handlers.onDone).toHaveBeenCalledWith({ kind: "merged", requirement: merged });
  });

  it("撞上的那条已完成：不能合并，只能改名；一改名提示就收起", async () => {
    const existing = { id: "requirement-done", title: "京东仓签收凭证", status: "done" as const };
    renderForm({
      requirementCandidate: vi.fn().mockResolvedValue(candidateDetail()),
      claimCandidate: vi.fn().mockRejectedValue(new ApiError("同名", 409, { detail: "同名", existing })),
    });
    const title = await screen.findByDisplayValue("京东仓签收凭证");
    await userEvent.click(submitButton("认领"));

    expect(await screen.findByText("它已完成，不能合并，请改个名字")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "改为合并到这条需求" })).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "改个名字" }));
    expect(title).toHaveFocus();

    await userEvent.type(title, "（物流）");
    expect(screen.queryByText(/里已有同名需求/)).not.toBeInTheDocument();
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
    await userEvent.selectOptions(screen.getByRole("combobox", { name: /所属项目/ }), CVM);
    expect(within(screen.getByRole("group", { name: "优先级" })).getByRole("button", { name: "P2" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );

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
    await userEvent.selectOptions(screen.getByRole("combobox", { name: /所属项目/ }), CVM);
    await userEvent.click(screen.getByRole("button", { name: "＋ 选来源会议，再挑会上原话" }));
    const dialog = await screen.findByRole("dialog", { name: "选来源" });
    // 默认只列需求所属项目的会
    expect(meetings).toHaveBeenLastCalledWith({ q: undefined, project_id: CVM, limit: 30 });
    await userEvent.click(await within(dialog).findByRole("button", { name: /260929 云课堂直播运营问题对齐/ }));

    expect(meeting).toHaveBeenCalledWith(CVM_MEETING.id);
    await userEvent.click(await within(dialog).findByRole("button", { name: /那我有办法导出 excel 吗/ }));
    fireEvent.click(within(dialog).getByRole("button", { name: /是没有办法/ }), { shiftKey: true });
    expect(within(dialog).getByText("▶ 00:09:36「那我有办法导出 excel 吗？是没有办法，」")).toBeInTheDocument();
    await userEvent.click(within(dialog).getByRole("button", { name: "用这句" }));

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
    expect(meetings).toHaveBeenLastCalledWith({ q: undefined, project_id: undefined, limit: 30 });
    await userEvent.click(await within(dialog).findByRole("button", { name: /CVM 云讲堂/ }));
    await within(dialog).findByRole("button", { name: /预约审核查看/ });
    await userEvent.click(within(dialog).getByRole("button", { name: "只关联这场会" }));

    const sourceField = screen.getByText("选定的会同时加进关联会议").closest(".form-field") as HTMLElement;
    expect(within(sourceField).getByText(CVM_MEETING.title)).toBeInTheDocument();
    expect(within(sourceField).queryByText(/▶/)).not.toBeInTheDocument();

    await userEvent.click(within(sourceField).getByRole("button", { name: "清空来源" }));
    expect(screen.getByRole("button", { name: "＋ 选来源会议，再挑会上原话" })).toBeInTheDocument();
  });

  it("从逐字稿选句进来（S10）：来源带上，所属项目随会议归属，可改", async () => {
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
    expect(screen.getByRole("combobox", { name: /所属项目/ })).toHaveValue(CVM);
    expect(screen.getByText("随会议归属，可改")).toBeInTheDocument();
    expect(screen.getByText("来自逐字稿选句")).toBeInTheDocument();
    expect(submitButton("创建")).toBeDisabled();

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
    const handlers = renderForm(
      { requirement, updateRequirement },
      { mode: "edit", candidateId: undefined, requirementId: "requirement-jd", canPickFolders: true },
    );

    expect(await screen.findByDisplayValue("京东科研仓对接")).toBeInTheDocument();
    expect(requirement).toHaveBeenCalledWith("requirement-jd");
    expect(screen.getByRole("heading", { name: "修改需求" })).toBeInTheDocument();
    expect(screen.getByText(/需求池 \/ 京东科研仓对接 \//)).toBeInTheDocument();
    expect(screen.getByText("改完保存，墙上的海报会跟着变。")).toBeInTheDocument();
    expect(screen.getByRole("combobox", { name: /所属项目/ })).toHaveValue(YIMI);
    expect(screen.getByText("对接京东科研仓")).toBeInTheDocument();
    const statuses = within(screen.getByRole("group", { name: "状态" }));
    expect(statuses.getAllByRole("button").map((button) => button.textContent)).toEqual(["进行中", "已完成", "已搁置"]);
    expect(statuses.getByRole("button", { name: "进行中" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByText("另有 1 句原话合并自候选「京东仓签收凭证」 · 00:31:49")).toBeInTheDocument();

    await userEvent.click(statuses.getByRole("button", { name: "已完成" }));
    await userEvent.click(submitButton("保存"));
    const patch = updateRequirement.mock.calls[0][1];
    expect(updateRequirement.mock.calls[0][0]).toBe("requirement-jd");
    expect(patch).toEqual({
      title: "京东科研仓对接",
      summary: jdDetail().summary,
      project_id: YIMI,
      priority: "P0",
      status: "done",
      folder_paths: ["/Volumes/外置中枢/医朵云/医米科研用药/对接京东科研仓"],
    });
    expect(handlers.onDone).toHaveBeenCalledWith({ kind: "edited", requirement: updated });
  });

  it("修改时清空来源就传 null（R04-5：来源可改）", async () => {
    const updateRequirement = vi.fn().mockResolvedValue(jdDetail({ source: null }));
    renderForm(
      { requirement: vi.fn().mockResolvedValue(jdDetail()), updateRequirement },
      { mode: "edit", candidateId: undefined, requirementId: "requirement-jd" },
    );
    await screen.findByDisplayValue("京东科研仓对接");

    await userEvent.click(screen.getByRole("button", { name: "清空来源" }));
    await userEvent.click(submitButton("保存"));
    expect(updateRequirement.mock.calls[0][1]).toEqual(expect.objectContaining({ source: null }));
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

describe("quoteOf", () => {
  it("连着的几句拼成原话，时间锚取第一句；倒着选也一样", () => {
    expect(quoteOf(CVM_SEGMENTS, 1, 2)).toEqual({ quote: "那我有办法导出 excel 吗？是没有办法，", anchor_ms: 576900 });
    expect(quoteOf(CVM_SEGMENTS, 2, 1)).toEqual({ quote: "那我有办法导出 excel 吗？是没有办法，", anchor_ms: 576900 });
  });
});
