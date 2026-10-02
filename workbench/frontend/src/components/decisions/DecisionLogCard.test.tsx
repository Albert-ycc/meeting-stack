import { act, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { DecisionLogEntry, RequirementDecisionLog } from "../../api";
import { LinksFlagsContext } from "../links/LinksFlagsContext";
import { DecisionLogCard } from "./DecisionLogCard";
import { PLACED_NONE_NOTICE } from "./decisionText";

const FLAGS = { linksEnabled: true, semanticEnabled: false, llmConfigured: false };

function entry(id: string, text: string): DecisionLogEntry {
  return {
    id,
    text,
    detail: text,
    start_ms: null,
    end_ms: null,
    placement: { how: "auto", requirement_id: null },
    later: [],
    earlier: [],
    restated: [],
    dismissed: [],
  } as unknown as DecisionLogEntry;
}

/** 每个需求挂一场会、一条决议；会议 id 和标题都带上需求 id，串了一眼能看出来 */
function decisionLog(requirementId: string, decisions: DecisionLogEntry[]): RequirementDecisionLog {
  return {
    requirement: { id: requirementId, title: `需求 ${requirementId}` },
    counts: { decisions: decisions.length, later_changed: 0, unplaced: 0 },
    state: { kind: "ok", text: null, action: null },
    meetings: [
      {
        meeting: { id: `m-${requirementId}`, title: `${requirementId} 的周会`, date: "2026-09-28", audio_url: null },
        note: decisions.length ? null : "这场纪要没有决议段",
        decisions,
        unplaced: [],
      },
    ],
  };
}

function card(apiClient: Parameters<typeof DecisionLogCard>[0]["apiClient"], requirementId: string) {
  return (
    <LinksFlagsContext.Provider value={FLAGS}>
      <DecisionLogCard apiClient={apiClient} canWrite onOpenMeeting={() => undefined} requirementId={requirementId} />
    </LinksFlagsContext.Provider>
  );
}

describe("DecisionLogCard 换需求不卸载", () => {
  it("在 A 放走一条决议后换到 B，A 的灰字行和那场会不跟到 B 上", async () => {
    let placedA = false;
    const apiClient = {
      meetingQuotes: vi.fn(),
      requirementDecisions: vi.fn(async (id: string) =>
        id === "A" ? decisionLog("A", placedA ? [] : [entry("dec-a", "A 的决议")]) : decisionLog("B", [entry("dec-b", "B 的决议")]),
      ),
      placeDecision: vi.fn(async () => {
        placedA = true;
        return {
          decision: { id: "dec-a", placement: "none", requirement_id: null },
          undo: { placement: "auto", requirement_id: "A" },
          undo_until: "2099-01-01T00:00:00Z",
        };
      }),
    } as unknown as Parameters<typeof DecisionLogCard>[0]["apiClient"];
    const view = render(card(apiClient, "A"));
    const section = await screen.findByRole("region", { name: "决议" });
    await within(section).findByText("A 的决议");
    await userEvent.click(within(section).getByRole("button", { name: "不属于这个需求" }));
    expect((await within(section).findAllByText(PLACED_NONE_NOTICE)).length).toBeGreaterThan(0);

    view.rerender(card(apiClient, "B"));
    await within(section).findByText("B 的决议");
    expect(within(section).queryByText(/A 的周会/)).toBeNull();
    expect(section.querySelector(".decision-log__placed")).toBeNull();
  });

  it("A 的读取比 B 晚回来时，B 的卡里不出现 A 的决议", async () => {
    let releaseA: (log: RequirementDecisionLog) => void = () => undefined;
    const apiClient = {
      meetingQuotes: vi.fn(),
      requirementDecisions: vi.fn((id: string) =>
        id === "A"
          ? new Promise<RequirementDecisionLog>((resolve) => (releaseA = resolve))
          : Promise.resolve(decisionLog("B", [entry("dec-b", "B 的决议")])),
      ),
    } as unknown as Parameters<typeof DecisionLogCard>[0]["apiClient"];
    const view = render(card(apiClient, "A"));
    view.rerender(card(apiClient, "B"));
    const section = await screen.findByRole("region", { name: "决议" });
    await within(section).findByText("B 的决议");
    await act(async () => releaseA(decisionLog("A", [entry("dec-a", "A 的决议")])));
    expect(within(section).queryByText("A 的决议")).toBeNull();
    expect(within(section).getByText("B 的决议")).toBeInTheDocument();
  });
});
