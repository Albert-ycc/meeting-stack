import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { DecisionRow, type DecisionRowData } from "./DecisionRow";

const MEETING = { id: "vm-1", title: "周会", audio_url: "/api/media/412" };
const LATE = { id: "vm-2", title: "周会", date: "2026-09-28" };

function decision(overrides: Partial<DecisionRowData> = {}): DecisionRowData {
  return {
    id: "dec-a",
    text: "阈值先按 0.8 执行",
    start_ms: 754_000,
    later: [{ relation_id: 17, decision_id: "dec-b", meeting: LATE, text: "阈值改成 0.7", start_ms: 30_000,
              quote: "改成 0.7", audio_url: "/api/media/430" }],
    earlier: [],
    restated: [{ relation_id: 21, decision_id: "dec-c", meeting: { id: "vm-3", title: "周会", date: "2026-09-30" },
                 start_ms: 12_000, audio_url: null }],
    dismissed: [],
    ...overrides,
  };
}

function setup(overrides: Partial<DecisionRowData> = {}, props: Record<string, unknown> = {}) {
  const player = { play: vi.fn() };
  const meetingQuotes = vi.fn().mockResolvedValue({
    quotes: [{ at_ms: 754_000, segments: [{ segment_id: "s1", start_ms: 750_000, end_ms: 760_000, text: "先按 0.8 来", speaker: null }] }],
  });
  const handlers = { onDismiss: vi.fn(), onRestore: vi.fn(), onOpenMeeting: vi.fn() };
  render(
    <DecisionRow
      apiClient={{ meetingQuotes }}
      canWrite
      decision={decision(overrides)}
      meeting={MEETING}
      player={player}
      {...handlers}
      {...props}
    />,
  );
  return { player, meetingQuotes, ...handlers };
}

describe("DecisionRow", () => {
  it("▶ 调宿主传进来的 player.play(audio_url, start_ms, label)；标记行里别的会的 ▶ 用那一条的录音", async () => {
    const { player } = setup();
    await userEvent.click(screen.getByRole("button", { name: "从 12:34 听这条" }));
    expect(player.play).toHaveBeenCalledWith("/api/media/412", 754_000, "周会");
    await userEvent.click(screen.getByRole("button", { name: "从 00:30 听这条" }));
    expect(player.play).toHaveBeenLastCalledWith("/api/media/430", 30_000, "周会");
    // 没有录音的那一头不出 ▶
    expect(screen.queryByRole("button", { name: "从 00:12 听这条" })).not.toBeInTheDocument();
  });

  it("标记行：后来改了、后来又提到都用［不是一回事］；后来改了的这条字用浅色", async () => {
    const { onDismiss } = setup();
    expect(screen.getByText("后来改了：9月28日 周会『阈值改成 0.7』")).toBeInTheDocument();
    expect(screen.getByText("后来又提到：9月30日 周会")).toBeInTheDocument();
    const buttons = screen.getAllByRole("button", { name: "不是一回事" });
    expect(buttons).toHaveLength(2);
    await userEvent.click(buttons[1]);
    expect(onDismiss).toHaveBeenCalledWith(21, "restated", "dec-a");
    expect(document.querySelector(".decision-row")?.classList.contains("is-changed")).toBe(true);
  });

  it("这次改了的那一条和标过的灰字行", async () => {
    const { onRestore } = setup({
      later: [],
      restated: [],
      earlier: [{ relation_id: 17, decision_id: "dec-0", meeting: { id: "vm-0", title: "周会", date: "2026-09-20" },
                  text: "阈值先按 0.9", start_ms: 1_000, quote: "", audio_url: null }],
      dismissed: [{ relation_id: 9, kind: "restated", other: { date: "2026-09-28", meeting_title: "周会", text: "照旧" },
                    decided_at: null }],
    });
    expect(screen.getByText("这次改了 9月20日 周会定的『阈值先按 0.9』")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "撤销" }));
    expect(onRestore).toHaveBeenCalledWith(expect.objectContaining({ relation_id: 9 }));
  });

  it("原话展开前后各 20 秒，打开会议带时间", async () => {
    const { meetingQuotes, onOpenMeeting } = setup();
    await userEvent.click(screen.getByRole("button", { name: "原话" }));
    expect(meetingQuotes).toHaveBeenCalledWith("vm-1", [754_000], "wide");
    expect(await screen.findByText("先按 0.8 来")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "打开会议" }));
    expect(onOpenMeeting).toHaveBeenCalledWith("vm-1", 754_000);
  });

  it("现读兜底（id 为 null）和手机不能写时不出标记和按钮", () => {
    setup({ id: null });
    expect(screen.queryByText(/后来改了/)).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "不是一回事" })).not.toBeInTheDocument();
  });

  it("canWrite 为假时标记照样显示，只是没有按钮", () => {
    setup({}, { canWrite: false });
    expect(screen.getByText("后来改了：9月28日 周会『阈值改成 0.7』")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "不是一回事" })).not.toBeInTheDocument();
  });
});
