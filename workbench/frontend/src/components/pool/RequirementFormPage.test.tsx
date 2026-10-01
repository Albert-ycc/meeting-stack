import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { ApiError, type ApiClient } from "../../api";
import type { CandidateDetail, Project } from "../../types";
import { candidateItem, CVM, HENGRUI, RECEIPT_SOURCE, YIMI } from "./poolFixtures";
import { RequirementFormPage, summaryLength } from "./RequirementFormPage";

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

function submitButton(name: "认领" | "创建") {
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

  it("新增：说明选填，没有来源一栏；填齐需求名和项目才能创建，默认 P2", async () => {
    const requirementCandidate = vi.fn();
    const created = { id: "requirement-export", title: "科室会预约后台导出" };
    const createRequirement = vi.fn().mockResolvedValue(created);
    const handlers = renderForm({ requirementCandidate, createRequirement }, { mode: "create", candidateId: undefined });

    expect(screen.getByRole("heading", { name: "新增需求" })).toBeInTheDocument();
    expect(screen.getByText("选填")).toBeInTheDocument();
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
});
