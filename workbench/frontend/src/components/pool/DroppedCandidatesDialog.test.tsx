import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { ApiClient } from "../../api";
import type { DroppedCandidates } from "../../types";
import { DroppedCandidatesDialog, restoreDaysLeft } from "./DroppedCandidatesDialog";
import { candidateItem, EXPORT_SOURCE } from "./poolFixtures";

/*
 * 候选和来源会议照 poolFixtures（生产库逐字稿抄的）：京东仓签收凭证出自 260916 医米京东科研仓系统对接，
 * 科室会预约后台导出出自 260929 云课堂直播运营问题对齐。丢掉的时刻和 30 天后的截止时刻是用例里定的，
 * 时钟钉在丢掉之后，不让结果取决于跑用例的那天。用例的时区钉在 Asia/Shanghai（vite.config.ts）。
 */
const DROPPED_AT = "2026-10-01T02:00:00+00:00"; // 北京时间 10-01 10:00
const RESTORE_UNTIL = "2026-10-31T02:00:00+00:00";

function dropped(overrides: Partial<ReturnType<typeof candidateItem>> = {}): DroppedCandidates["items"][number] {
  return {
    ...candidateItem({ dropped_at: DROPPED_AT, ...overrides }),
    status: "dropped",
    restore_until: RESTORE_UNTIL,
  };
}

function renderDrawer(items: DroppedCandidates["items"], api: Partial<ApiClient> = {}) {
  const droppedCandidates = vi.fn().mockResolvedValue({ items, total: items.length, undo_days: 30 });
  const onClose = vi.fn();
  const onRestored = vi.fn();
  render(
    <DroppedCandidatesDialog
      apiClient={{ droppedCandidates, ...api } as unknown as ApiClient}
      onClose={onClose}
      onRestored={onRestored}
    />,
  );
  return { droppedCandidates, onClose, onRestored };
}

beforeEach(() => {
  vi.useFakeTimers({ shouldAdvanceTime: true, now: new Date("2026-10-01T02:00:30Z") });
});
afterEach(() => {
  vi.useRealTimers();
});

describe("已丢掉抽屉（S01-c）", () => {
  it("每条写候选名、项目（带座次）、来源会议和那场会的日子、丢掉时间、剩余天数，还有「撤销」", async () => {
    renderDrawer([dropped()]);

    const item = (await screen.findByText("京东仓签收凭证")).closest("li") as HTMLElement;
    expect(within(item).getByText("医米科研用药").querySelector(".dropped-item__seat")).toHaveTextContent("1");
    // 来源会议录音时间 -07:00 的 09-16 19:01，在北京时间是 09-17
    expect(within(item).getByText("出自 260916 医米京东科研仓系统对接 · 09-17")).toBeInTheDocument();
    // 刚丢掉半分钟：写「刚刚丢掉」；30 天的期限还没走一天
    expect(within(item).getByText(/刚刚丢掉 · 还剩 30 天/)).toBeInTheDocument();
    expect(within(item).getByRole("button", { name: "撤销" })).toBeEnabled();
    expect(screen.getByRole("dialog", { name: "已丢掉的候选" })).toHaveTextContent("丢掉的候选保留 30 天，期间可以撤销回待认领");
    expect(screen.getByRole("heading", { name: "已丢掉 1" })).toBeInTheDocument();
  });

  it("过了一阵子：丢掉时间写日期和钟点，剩余天数由 restore_until 和现在算（往上取整）", async () => {
    vi.setSystemTime(new Date("2026-10-04T02:00:00Z"));
    renderDrawer([dropped()]);
    const item = (await screen.findByText("京东仓签收凭证")).closest("li") as HTMLElement;
    expect(within(item).getByText(/10-01 10:00 丢掉 · 还剩 27 天/)).toBeInTheDocument();
  });

  it("最后一天不到 24 小时也算还剩 1 天；到点了写「已过期」", async () => {
    vi.setSystemTime(new Date("2026-10-30T12:00:00Z"));
    renderDrawer([dropped()]);
    const item = (await screen.findByText("京东仓签收凭证")).closest("li") as HTMLElement;
    expect(within(item).getByText(/还剩 1 天/)).toBeInTheDocument();
  });

  it("剩余天数的算法：整 30 天、差半分钟、不满一天、已过", () => {
    const until = Date.parse(RESTORE_UNTIL);
    expect(restoreDaysLeft(RESTORE_UNTIL, until - 30 * 86_400_000)).toBe(30);
    expect(restoreDaysLeft(RESTORE_UNTIL, until - 30 * 86_400_000 + 30_000)).toBe(30);
    expect(restoreDaysLeft(RESTORE_UNTIL, until - 12 * 3_600_000)).toBe(1);
    expect(restoreDaysLeft(RESTORE_UNTIL, until + 1_000)).toBeLessThanOrEqual(0);
  });

  it("没归项目的候选写「未归项目」、没有座次；没有来源的不写「出自」", async () => {
    renderDrawer([dropped({ title: "医生资质 AI 审核规则", project_id: null, project_name: null, project_seat: null, source: null })]);

    const item = (await screen.findByText("医生资质 AI 审核规则")).closest("li") as HTMLElement;
    expect(within(item).getByText("未归项目")).toBeInTheDocument();
    expect(item.querySelector(".dropped-item__seat")).toBeNull();
    expect(within(item).queryByText(/出自/)).not.toBeInTheDocument();
  });

  it("多条按后端给的顺序排；标题上的数是条数", async () => {
    renderDrawer([
      dropped(),
      dropped({
        id: "candidate-export",
        title: "科室会预约后台导出",
        project_name: "CVM 云讲堂",
        project_seat: 4,
        source: EXPORT_SOURCE,
      }),
    ]);

    await screen.findByText("京东仓签收凭证");
    const titles = Array.from(document.querySelectorAll(".dropped-item__title")).map((node) => node.textContent);
    expect(titles).toEqual(["京东仓签收凭证", "科室会预约后台导出"]);
    expect(screen.getByRole("heading", { name: "已丢掉 2" })).toBeInTheDocument();
    expect(screen.getByText("出自 260929 云课堂直播运营问题对齐 · 09-30")).toBeInTheDocument();
  });

  it("没有可撤销的候选：写一句话，标题上不带数", async () => {
    renderDrawer([]);
    expect(await screen.findByText("没有可以撤销的候选")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "已丢掉" })).toBeInTheDocument();
  });

  it("点「撤销」：调 restoreCandidate，告诉需求池是哪一条，列表重新取；撤销中其余按钮置灰", async () => {
    let finish: () => void = () => undefined;
    const restoreCandidate = vi.fn(() => new Promise<unknown>((resolve) => (finish = () => resolve({}))));
    const { droppedCandidates, onRestored } = renderDrawer(
      [dropped(), dropped({ id: "candidate-export", title: "科室会预约后台导出", source: EXPORT_SOURCE })],
      { restoreCandidate } as Partial<ApiClient>,
    );
    await screen.findByText("京东仓签收凭证");

    fireEvent.click(screen.getAllByRole("button", { name: "撤销" })[0]);
    expect(restoreCandidate).toHaveBeenCalledWith("candidate-receipt");
    expect(screen.getByRole("button", { name: "撤销中…" })).toBeDisabled();
    expect(screen.getAllByRole("button", { name: "撤销" })[0]).toBeDisabled();

    finish();
    await waitFor(() => expect(onRestored).toHaveBeenCalledWith("京东仓签收凭证"));
    await waitFor(() => expect(droppedCandidates).toHaveBeenCalledTimes(2));
  });

  it("撤销失败（超过 30 天）：抽屉里写后端给的原因，列表不动", async () => {
    const restoreCandidate = vi.fn().mockRejectedValue(new Error("丢掉超过 30 天，不能撤销了"));
    const { onRestored } = renderDrawer([dropped()], { restoreCandidate } as Partial<ApiClient>);
    await screen.findByText("京东仓签收凭证");

    fireEvent.click(screen.getByRole("button", { name: "撤销" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("丢掉超过 30 天，不能撤销了");
    expect(onRestored).not.toHaveBeenCalled();
    expect(screen.getByText("京东仓签收凭证")).toBeInTheDocument();
  });

  it("读取失败：写原因，不当成「没有可撤销的」以外的东西卡住", async () => {
    const droppedCandidates = vi.fn().mockRejectedValue(new Error("读取失败（500）"));
    render(<DroppedCandidatesDialog apiClient={{ droppedCandidates } as unknown as ApiClient} onClose={vi.fn()} onRestored={vi.fn()} />);
    expect(await screen.findByRole("alert")).toHaveTextContent("读取失败（500）");
  });

  it("关闭：✕、Esc、点抽屉外的背景都能关；点抽屉里面不关", async () => {
    const { onClose } = renderDrawer([dropped()]);
    await screen.findByText("京东仓签收凭证");

    fireEvent.mouseDown(screen.getByText("京东仓签收凭证"));
    fireEvent.click(screen.getByText("京东仓签收凭证"));
    expect(onClose).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: "关闭" }));
    expect(onClose).toHaveBeenCalledTimes(1);
    fireEvent.keyDown(document, { key: "Escape" });
    expect(onClose).toHaveBeenCalledTimes(2);

    const overlay = document.querySelector(".pool-drawer__overlay") as HTMLElement;
    fireEvent.mouseDown(overlay);
    fireEvent.click(overlay);
    expect(onClose).toHaveBeenCalledTimes(3);
  });
});
