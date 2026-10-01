import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { RequirementSource } from "../../types";
import { stubPeaksFetch } from "./peaksFixtures";
import { JD_SOURCE } from "./poolFixtures";
import { clearPeaksCache } from "./PosterWaveform";
import { RequirementSourceCard } from "./RequirementSourceCard";

/*
 * 来源照生产库逐字稿抄（会 vm-20260916-190150-2eebb406，时长 3033387 ms，和 poolFixtures、后端
 * requirement_pool_world 同一份）：00:13:45 那两句连着（825270、827350），00:31:49 那两句连着（1909360、1915910）。
 */
const MEETING_ID = "vm-20260916-190150-2eebb406";
const DURATION = 3033387;
const AUDIO = 77;

function source(overrides: Partial<RequirementSource>): RequirementSource {
  return { ...JD_SOURCE, audio_artifact_id: AUDIO, ...overrides };
}

const ORIGIN = source({
  id: 1,
  kind: "origin",
  quote: "就是这个入库单的这个单据，你得需要从你们的一米这个系统里面给我们这个库房推过来。",
  anchor_ms: 825270,
});
const MERGED = source({
  id: 2,
  kind: "merged",
  quote: "强制要求患者除了传教和随行码嗯患者手持身份证跟药盒拍照，然后还要传这个随货通行单，",
  anchor_ms: 1909360,
  via_candidate_title: "京东仓签收凭证",
});

function renderCard(props: Partial<Parameters<typeof RequirementSourceCard>[0]> = {}) {
  const onOpenMeeting = vi.fn();
  const view = render(
    <RequirementSourceCard onOpenMeeting={onOpenMeeting} origin={ORIGIN} sources={[ORIGIN, MERGED]} {...props} />,
  );
  return { onOpenMeeting, card: within(screen.getByRole("region", { name: "出自录音" })), ...view };
}

beforeEach(() => clearPeaksCache());
afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  vi.useRealTimers();
});

describe("出自录音：波形和时间签", () => {
  it("取到波形：时间签贴着锚点画（提出的实心、合并的空心），底下是 00:00 到录音时长的坐标轴", async () => {
    stubPeaksFetch({ [AUDIO]: DURATION });
    const { card } = renderCard();

    const originFlag = await card.findByTitle("00:13:45 提出");
    const mergedFlag = card.getByTitle("00:31:49 合并 · 京东仓签收凭证");
    expect(originFlag).toHaveClass("is-origin");
    expect(mergedFlag).not.toHaveClass("is-origin");
    expect(parseFloat(originFlag.style.left)).toBeCloseTo((825270 / DURATION) * 100, 3);
    expect(parseFloat(mergedFlag.style.left)).toBeCloseTo((1909360 / DURATION) * 100, 3);
    expect(card.getByText("00:00")).toBeInTheDocument();
    expect(card.getByText("50:33")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "260916 医米京东科研仓系统对接 的录音波形" })).toBeInTheDocument();
  });

  it("审查 M9：锚点在录音末尾，签的右沿贴着波形右边、不再伸出卡片（原来一律居中，伸出去 20 多像素）", async () => {
    // 让提出的那句（825270）正好落在录音末尾：峰值的时长取 825.27 秒
    stubPeaksFetch({ [AUDIO]: 825270 });
    const { card } = renderCard({ sources: [ORIGIN] });

    const flag = await card.findByTitle("00:13:45 提出");
    expect(flag.style.left).toBe("100%");
    // 左移自己宽度的 100%：右沿对齐锚点，而不是居中（居中是 translateX(-50%)）
    expect(flag.style.transform).toBe("translateX(-100%)");
  });

  it("审查 M9：锚点在录音中间，签的位置按比例过渡，既不居中也不贴边", async () => {
    stubPeaksFetch({ [AUDIO]: DURATION });
    const { card } = renderCard({ sources: [ORIGIN] });

    const flag = await card.findByTitle("00:13:45 提出");
    const percent = parseFloat(flag.style.left);
    expect(flag.style.transform).toBe(`translateX(-${flag.style.left})`);
    expect(percent).toBeGreaterThan(20);
    expect(percent).toBeLessThan(35);
  });

  describe("挨得近的两个锚点错开一行（量出来的宽度）", () => {
    // jsdom 没有布局：把签和波形的宽度量出来的那两个读数换成确定的值，按一个 1300 宽的波形算
    beforeEach(() => {
      vi.spyOn(HTMLElement.prototype, "offsetWidth", "get").mockImplementation(function (this: HTMLElement) {
        return this.classList.contains("source-card__flag") ? (this.textContent ?? "").length * 9 + 18 : 0;
      });
      vi.spyOn(Element.prototype, "clientWidth", "get").mockImplementation(function (this: Element) {
        return this.classList.contains("source-card__flags") ? 1300 : 0;
      });
    });

    it("审查 M9：00:31:49 和 00:31:55 只差 6 秒，后一个签错开到上一行，两个都读得出来", async () => {
      stubPeaksFetch({ [AUDIO]: DURATION });
      const first = source({ id: 11, kind: "origin", quote: "强制要求患者除了传教和随行码嗯患者手持身份证跟药盒拍照，", anchor_ms: 1909360 });
      const second = source({
        id: 12,
        kind: "merged",
        quote: "然后还要传这个随货通行单，",
        anchor_ms: 1915910,
        via_candidate_title: "京东仓签收凭证",
      });
      const { card } = renderCard({ origin: first, sources: [first, second] });

      const firstFlag = await card.findByTitle("00:31:49 提出");
      const secondFlag = card.getByTitle("00:31:55 合并 · 京东仓签收凭证");
      // 两行：靠近波形的一行放先到的，错开的放在它上面
      expect(firstFlag.style.top).toBe("26px");
      expect(secondFlag.style.top).toBe("0px");
    });

    it("隔得远的两个（00:13:45 和 00:31:49）还是同一行", async () => {
      stubPeaksFetch({ [AUDIO]: DURATION });
      const { card } = renderCard();

      const firstFlag = await card.findByTitle("00:13:45 提出");
      const secondFlag = card.getByTitle("00:31:49 合并 · 京东仓签收凭证");
      expect(firstFlag.style.top).toBe("0px");
      expect(secondFlag.style.top).toBe("0px");
    });

    it("错开以后波形上方多留一行的高度（--flag-rows），不压到波形", async () => {
      stubPeaksFetch({ [AUDIO]: DURATION });
      const first = source({ id: 11, kind: "origin", quote: "强制要求患者除了传教和随行码嗯患者手持身份证跟药盒拍照，", anchor_ms: 1909360 });
      const second = source({ id: 12, kind: "merged", quote: "然后还要传这个随货通行单，", anchor_ms: 1915910 });
      const { container, card } = renderCard({ origin: first, sources: [first, second] });

      await card.findByTitle("00:31:49 提出");
      const wave = container.querySelector<HTMLElement>(".source-card__wave")!;
      expect(wave.style.getPropertyValue("--flag-rows")).toBe("2");
    });
  });
});

describe("出自录音：取不到波形（审查 M10，R02 异常）", () => {
  it("这场会没有录音文件（audio_artifact_id 为空）：不画波形、时间签、00:00 坐标轴，也不留空位；会议信息和原话时间锚还在", () => {
    stubPeaksFetch({});
    const noAudio = [source({ ...ORIGIN, audio_artifact_id: null }), source({ ...MERGED, audio_artifact_id: null })];
    const { container, card } = renderCard({ origin: noAudio[0], sources: noAudio });

    expect(container.querySelector(".source-card__wave")).toBeNull();
    expect(container.querySelector(".source-card__flags")).toBeNull();
    expect(container.querySelector(".source-card__axis")).toBeNull();
    expect(container.querySelector(".poster-wave")).toBeNull();
    expect(card.queryByText("00:00")).not.toBeInTheDocument();
    expect(card.getByText("260916 医米京东科研仓系统对接")).toBeInTheDocument();
    expect(card.getByRole("button", { name: /从 00:13:45 开始放/ })).toBeInTheDocument();
    expect(card.getByRole("button", { name: /从 00:31:49 开始放/ })).toBeInTheDocument();
  });

  it("峰值接口失败：先占位，失败后整块波形撤掉（时间签、坐标轴、64px 空位都不留）", async () => {
    stubPeaksFetch({ [AUDIO]: "fail" });
    const { container, card } = renderCard();

    // 还在取的时候整块占着位置
    expect(container.querySelector(".source-card__wave.is-loading")).not.toBeNull();
    await waitFor(() => expect(container.querySelector(".source-card__wave")).toBeNull());
    expect(container.querySelector(".source-card__flags")).toBeNull();
    expect(container.querySelector(".source-card__axis")).toBeNull();
    expect(container.querySelector(".poster-wave")).toBeNull();
    expect(card.getByRole("button", { name: /从 00:13:45 开始放/ })).toBeInTheDocument();
  });

  it("还在取：整块先占住，不画时间签和坐标轴（时长要等峰值取到才知道）", () => {
    stubPeaksFetch({ [AUDIO]: "hang" });
    const { container } = renderCard();

    expect(container.querySelector(".source-card__wave.is-loading")).not.toBeNull();
    expect(container.querySelector(".source-card__flag")).toBeNull();
    expect(container.querySelector(".source-card__axis")).toBeNull();
  });
});

describe("出自录音：标签（审查 B4）", () => {
  it("只关联了会议、没挑原话的来源，标签写「提出 · 只关联了会议」，不写「会上原话」", () => {
    stubPeaksFetch({});
    const bare = source({ id: 9, kind: "origin", quote: "", anchor_ms: null, audio_artifact_id: null });
    const { card } = renderCard({ origin: bare, sources: [bare] });

    expect(card.getByText("提出 · 只关联了会议")).toBeInTheDocument();
    expect(card.queryByText(/会上原话/)).not.toBeInTheDocument();
    expect(card.getByText("（只关联了这场会，没挑原话）")).toBeInTheDocument();
  });

  it("挑了原话的来源照旧写「提出 · 会上原话」；合并进来的写合并自哪条候选", () => {
    stubPeaksFetch({});
    const { card } = renderCard({ origin: { ...ORIGIN, audio_artifact_id: null }, sources: [{ ...ORIGIN, audio_artifact_id: null }, { ...MERGED, audio_artifact_id: null }] });

    expect(card.getByText("提出 · 会上原话")).toBeInTheDocument();
    expect(card.getByText("合并自候选「京东仓签收凭证」")).toBeInTheDocument();
  });
});

describe("出自录音：点时间锚传出去的是哪一秒（R02-10）", () => {
  it("头部「打开会议」、波形、波形上的签、每条原话的时间，传的都是对应那句的锚点（毫秒）", async () => {
    stubPeaksFetch({ [AUDIO]: DURATION });
    const { card, onOpenMeeting } = renderCard();
    await card.findByTitle("00:13:45 提出");

    fireEvent.click(card.getByRole("button", { name: /打开会议/ }));
    expect(onOpenMeeting).toHaveBeenLastCalledWith(MEETING_ID, 825270);

    fireEvent.click(screen.getByRole("button", { name: "260916 医米京东科研仓系统对接 的录音波形" }));
    expect(onOpenMeeting).toHaveBeenLastCalledWith(MEETING_ID, 825270);

    fireEvent.click(card.getByTitle("00:31:49 合并 · 京东仓签收凭证"));
    expect(onOpenMeeting).toHaveBeenLastCalledWith(MEETING_ID, 1909360);
    fireEvent.click(card.getByTitle("00:13:45 提出"));
    expect(onOpenMeeting).toHaveBeenLastCalledWith(MEETING_ID, 825270);

    fireEvent.click(card.getByRole("button", { name: /从 00:31:49 开始放/ }));
    expect(onOpenMeeting).toHaveBeenLastCalledWith(MEETING_ID, 1909360);
    fireEvent.click(card.getByRole("button", { name: /从 00:13:45 开始放/ }));
    expect(onOpenMeeting).toHaveBeenLastCalledWith(MEETING_ID, 825270);
    expect(onOpenMeeting).toHaveBeenCalledTimes(6);
  });

  // 0 毫秒是边界值（录音的第一句）：原话文字取自京东那场会的逐字稿，但逐字稿里没有恰好从 0 毫秒开始的句子，
  // 所以这一条的时间是为测边界定的，不是逐字稿里的真实时间锚
  it("锚点是 0 毫秒也要传 0（不能被当成「没有锚点」）", () => {
    stubPeaksFetch({});
    const atStart = source({ id: 5, kind: "origin", quote: "就是这个入库单的这个单据，", anchor_ms: 0, audio_artifact_id: null });
    const { card, onOpenMeeting } = renderCard({ origin: atStart, sources: [atStart] });

    fireEvent.click(card.getByRole("button", { name: /打开会议/ }));
    expect(onOpenMeeting).toHaveBeenLastCalledWith(MEETING_ID, 0);
    fireEvent.click(card.getByRole("button", { name: /从 00:00:00 开始放/ }));
    expect(onOpenMeeting).toHaveBeenLastCalledWith(MEETING_ID, 0);
  });

  it("只关联了会议的来源没有锚点：点「打开会议」只打开会议，不带时间", () => {
    stubPeaksFetch({});
    const bare = source({ id: 9, kind: "origin", quote: "", anchor_ms: null, audio_artifact_id: null });
    const { card, onOpenMeeting } = renderCard({ origin: bare, sources: [bare] });

    // 头部的「打开会议 →」和那一行的「打开会议」：都没有时间可带
    const [header, row] = card.getAllByRole("button", { name: "打开会议" });
    fireEvent.click(header);
    expect(onOpenMeeting).toHaveBeenLastCalledWith(MEETING_ID, undefined);
    fireEvent.click(row);
    expect(onOpenMeeting).toHaveBeenLastCalledWith(MEETING_ID);
  });
});

describe("出自录音：撤销合并（R01-14，S03-b）", () => {
  // 合并后 10 分钟内有撤销：until 是合并时刻加 10 分钟（后端算好给过来），前端按本机时间判断
  const NOW = new Date("2026-10-01T10:00:00+08:00");
  const UNTIL = new Date(NOW.getTime() + 10 * 60_000).toISOString();
  const withUndo = { ...MERGED, audio_artifact_id: null, undo_merge: { candidate_id: "candidate-receipt", until: UNTIL } };
  const origin = { ...ORIGIN, audio_artifact_id: null };

  beforeEach(() => {
    vi.useFakeTimers({ shouldAdvanceTime: true, now: NOW });
  });

  it("合并进来的原话在时限内：那一行有「撤销合并」，点了带着候选 id 回调；提出的那句没有", () => {
    const onUndoMerge = vi.fn();
    const { card } = renderCard({ origin, sources: [origin, withUndo], onUndoMerge });

    const buttons = card.getAllByRole("button", { name: "撤销合并" });
    expect(buttons).toHaveLength(1);
    expect(buttons[0].closest("li")).toHaveTextContent("合并自候选「京东仓签收凭证」");
    fireEvent.click(buttons[0]);
    expect(onUndoMerge).toHaveBeenCalledWith("candidate-receipt");
  });

  it("到了 until：按钮自己消失，不用刷新页面（按本机时间判断）", () => {
    renderCard({ origin, sources: [origin, withUndo], onUndoMerge: vi.fn() });
    expect(screen.getByRole("button", { name: "撤销合并" })).toBeInTheDocument();

    act(() => {
      vi.advanceTimersByTime(9 * 60_000 + 59_000);
    });
    expect(screen.getByRole("button", { name: "撤销合并" })).toBeInTheDocument();
    act(() => {
      vi.advanceTimersByTime(2_000);
    });
    expect(screen.queryByRole("button", { name: "撤销合并" })).not.toBeInTheDocument();
  });

  it("页面打开时已经过了 until：不显示", () => {
    vi.setSystemTime(new Date(NOW.getTime() + 11 * 60_000));
    renderCard({ origin, sources: [origin, withUndo], onUndoMerge: vi.fn() });

    expect(screen.queryByRole("button", { name: "撤销合并" })).not.toBeInTheDocument();
  });

  it("没有 undo_merge 的、或没传 onUndoMerge 的不显示；撤销进行中按钮置灰", () => {
    const { rerender } = render(
      <RequirementSourceCard onOpenMeeting={vi.fn()} onUndoMerge={vi.fn()} origin={origin} sources={[origin, { ...MERGED, audio_artifact_id: null }]} />,
    );
    expect(screen.queryByRole("button", { name: "撤销合并" })).not.toBeInTheDocument();

    rerender(<RequirementSourceCard onOpenMeeting={vi.fn()} origin={origin} sources={[origin, withUndo]} />);
    expect(screen.queryByRole("button", { name: "撤销合并" })).not.toBeInTheDocument();

    rerender(<RequirementSourceCard onOpenMeeting={vi.fn()} onUndoMerge={vi.fn()} origin={origin} sources={[origin, withUndo]} undoBusy />);
    expect(screen.getByRole("button", { name: "撤销合并" })).toBeDisabled();
  });
});
