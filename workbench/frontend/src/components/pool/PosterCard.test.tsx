import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { candidateItem, EXPORT_SOURCE, requirementItem } from "./poolFixtures";
import { PosterCard } from "./PosterCard";

describe("PosterCard", () => {
  it("需求海报：座次和项目、等级、需求名、说明、三个数、出自录音和会上提出的日子", () => {
    render(<PosterCard canWrite item={requirementItem()} />);
    const poster = screen.getByRole("article", { name: "需求：京东科研仓对接" });

    const project = within(poster).getByText("医米科研用药").parentElement!;
    expect(project).toHaveTextContent("1医米科研用药");
    expect(within(project).getByText("1")).toHaveClass("poster__seat");
    expect(within(poster).getByText("P0")).toHaveClass("poster__tag--p0");
    expect(within(poster).getByRole("heading", { name: "京东科研仓对接" })).toBeInTheDocument();
    expect(within(poster).getByText(/采购单入库、销售单/)).toBeInTheDocument();
    expect(within(poster).getByText("会议").nextElementSibling).toHaveTextContent("2");
    expect(within(poster).getByText("材料").nextElementSibling).toHaveTextContent("1");
    expect(within(poster).getByRole("button", { name: /原话 00:13:45/ })).toBeInTheDocument();
    expect(within(poster).getByText("260916 医米京东科研仓系统对接")).toBeInTheDocument();
    // 用例钉在北京时间：-07:00 的 09-16 19:01 是 09-17 10:01；51 分钟；之后又跟进 1 场
    expect(within(poster).getByText("09-17 10:01 · 51 分钟 · 跟进 1 场")).toBeInTheDocument();
    expect(within(poster).getByText("09-17").parentElement).toHaveTextContent("09-17 会上提出");
  });

  it("点原话时间打开这场会、从那一秒开始放；点海报、查看、接下进详情", async () => {
    const onOpen = vi.fn();
    const onOpenMeeting = vi.fn();
    render(<PosterCard canWrite item={requirementItem()} onOpen={onOpen} onOpenMeeting={onOpenMeeting} />);

    await userEvent.click(screen.getByRole("button", { name: /原话 00:13:45/ }));
    expect(onOpenMeeting).toHaveBeenCalledWith("vm-20260916-190150-2eebb406", 825270);
    expect(onOpen).not.toHaveBeenCalled();

    await userEvent.click(screen.getByRole("button", { name: /接下/ }));
    await userEvent.click(screen.getByRole("button", { name: "查看" }));
    await userEvent.click(screen.getByRole("heading", { name: "京东科研仓对接" }));
    expect(onOpen).toHaveBeenCalledTimes(3);
  });

  it("没有来源的需求不显示出自录音，底栏写建的日子", () => {
    render(<PosterCard canWrite item={requirementItem({ source: null, created_at: "2026-09-30T02:00:00+00:00" })} />);

    expect(screen.queryByText("出自录音")).not.toBeInTheDocument();
    expect(screen.getByText("09-30").parentElement).toHaveTextContent("09-30 新建");
  });

  it("候选海报：AI 候选、像已有需求，默认动作是合并时合并按钮在前", async () => {
    const onDrop = vi.fn();
    const onMerge = vi.fn();
    const onClaim = vi.fn();
    render(<PosterCard canWrite item={candidateItem()} onClaim={onClaim} onDrop={onDrop} onMerge={onMerge} />);
    const poster = screen.getByRole("article", { name: "候选：京东仓签收凭证" });

    expect(within(poster).getByText("AI 候选")).toBeInTheDocument();
    expect(within(poster).getByText("京东科研仓对接").parentElement).toHaveTextContent("像已有需求：京东科研仓对接");
    expect(within(poster).getByRole("button", { name: "合并" })).toHaveClass("is-primary");
    expect(within(poster).getByRole("button", { name: /认领/ })).not.toHaveClass("is-primary");

    await userEvent.click(within(poster).getByRole("button", { name: "丢掉" }));
    await userEvent.click(within(poster).getByRole("button", { name: "合并" }));
    await userEvent.click(within(poster).getByRole("button", { name: /认领/ }));
    expect(onDrop).toHaveBeenCalledTimes(1);
    expect(onMerge).toHaveBeenCalledTimes(1);
    expect(onClaim).toHaveBeenCalledTimes(1);
  });

  it("候选所属项目下没有可合并的需求时不给合并；未归项目的写「未归项目」、没有座次", () => {
    render(
      <PosterCard
        canWrite
        item={candidateItem({
          title: "医生资质 AI 审核规则",
          project_id: null,
          project_name: null,
          project_seat: null,
          similar_requirement: null,
          default_action: "claim",
          can_merge: false,
          source: EXPORT_SOURCE,
        })}
      />,
    );

    expect(screen.getByText("未归项目")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "合并" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /认领/ })).toHaveClass("is-primary");
  });

  it("墙上预览：按钮不响应，空的需求名和说明给占位", async () => {
    const onOpen = vi.fn();
    render(
      <PosterCard
        canWrite={false}
        item={requirementItem({ title: "", summary: "", source: null })}
        onOpen={onOpen}
        preview
      />,
    );

    expect(screen.getByRole("heading", { name: "需求名" })).toHaveClass("is-placeholder");
    expect(screen.getByText("说明")).toHaveClass("is-placeholder");
    await userEvent.click(screen.getByRole("button", { name: /接下/ }));
    expect(onOpen).not.toHaveBeenCalled();
  });
});
