import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { ApiClient } from "../api";
import type { MeetingGlossary } from "../types";
import { MeetingGlossaryPanel } from "./MeetingGlossaryPanel";

function glossary(overrides: Partial<MeetingGlossary> = {}): MeetingGlossary {
  return {
    basis: "receipt",
    project: { id: "p-yt", name: "云图AI", color: "#f0783b" },
    meeting_project: { id: "p-yt", name: "云图AI", color: "#f0783b" },
    mismatch: false,
    receipt: {
      id: 1,
      job_id: "job-1",
      attempt: 1,
      project_id: "p-yt",
      project_name: "云图AI",
      project_source: "transcript",
      term_count: 12,
      project_terms: 5,
      public_terms: 7,
      snapshot_missing: false,
      generated_at: "2026-09-26T10:00:00+00:00",
    },
    minutes_version_id: "mv-1",
    stale: false,
    checked_at: "2026-09-26T10:05:00+00:00",
    corrected: [
      { kind: "corrected", term: "随访", wrong: "随方", term_project_id: null, transcript_count: 2, minutes_count: 3 },
    ],
    missed: [
      { kind: "missed", term: "数理协会", wrong: "树立协会", term_project_id: "p-yt", transcript_count: 1, minutes_count: 2 },
    ],
    applied: null,
    ...overrides,
  };
}

function client(overrides: Partial<ApiClient> = {}) {
  return {
    checkMeetingGlossary: vi.fn().mockResolvedValue({ glossary: null }),
    applyMeetingGlossary: vi.fn().mockResolvedValue({ version_id: "mv-2", replaced: 2, glossary: null }),
    undoMeetingGlossary: vi.fn().mockResolvedValue({ version_id: "mv-3", glossary: null }),
    ...overrides,
  } as unknown as ApiClient;
}

function renderPanel(value: MeetingGlossary, apiClient = client(), props: { canEdit?: boolean; isMobile?: boolean } = {}) {
  const onMinutesChanged = vi.fn().mockResolvedValue(undefined);
  render(
    <MeetingGlossaryPanel
      apiClient={apiClient}
      canEdit={props.canEdit ?? true}
      editBlockedReason={props.canEdit === false ? "先保存或放弃正在改的纪要" : undefined}
      glossary={value}
      isMobile={props.isMobile ?? false}
      meetingId="vm-1"
      onMinutesChanged={onMinutesChanged}
    />,
  );
  return { apiClient, onMinutesChanged };
}

describe("MeetingGlossaryPanel", () => {
  it("说清按哪个项目纠的错、纠正了哪些，漏纠的一键改过来", async () => {
    const { apiClient, onMinutesChanged } = renderPanel(glossary());

    expect(
      screen.getByText("出纪要时按「云图AI」的词典纠错，用了 12 条词（项目词 5 条、公共词 7 条）；项目是按逐字稿认出来的"),
    ).toBeTruthy();
    expect(screen.getByText("纠正了 1 处：随方→随访")).toBeTruthy();
    expect(screen.getByText("可能漏纠 2 处，纪要里还留着错写：")).toBeTruthy();
    expect(screen.getByText("纪要里 2 次")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "改过来" }));
    await waitFor(() => expect(apiClient.applyMeetingGlossary).toHaveBeenCalledWith("vm-1", "mv-1"));
    await waitFor(() => expect(onMinutesChanged).toHaveBeenCalledWith("已把 2 处错写改成词典里的写法，可以撤销"));
  });

  it("归属和纠错用的项目不一样时可以换个项目查，再回到默认", async () => {
    const chosen = glossary({
      basis: "chosen",
      project: { id: "p-zt", name: "数据中台", color: null },
      meeting_project: { id: "p-zt", name: "数据中台", color: null },
      mismatch: false,
      missed: [],
    });
    const apiClient = client({
      checkMeetingGlossary: vi.fn().mockResolvedValueOnce({ glossary: chosen }).mockResolvedValue({ glossary: null }),
    });
    renderPanel(
      glossary({ meeting_project: { id: "p-zt", name: "数据中台", color: null }, mismatch: true }),
      apiClient,
    );

    expect(screen.getByText("这场会现在归在「数据中台」，和纠错用的不是同一个项目。")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "按「数据中台」的词典检查" }));
    await waitFor(() => expect(apiClient.checkMeetingGlossary).toHaveBeenCalledWith("vm-1", "p-zt"));
    expect(await screen.findByText("按你选的「数据中台」的词典查了一遍")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "回到默认" }));
    await waitFor(() => expect(apiClient.checkMeetingGlossary).toHaveBeenLastCalledWith("vm-1", null));
  });

  it("自动改过的给撤销；纪要正在改时按钮不能点并说明原因", async () => {
    const auto = glossary({ missed: [], applied: { by: "auto", count: 2, at: "2026-09-26T10:06:00+00:00", can_undo: true } });
    const { apiClient, onMinutesChanged } = renderPanel(auto);
    expect(screen.getByText("出纪要后已按词典自动改了 2 处漏纠的错写")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "撤销" }));
    await waitFor(() => expect(apiClient.undoMeetingGlossary).toHaveBeenCalledWith("vm-1"));
    await waitFor(() => expect(onMinutesChanged).toHaveBeenCalledWith("已撤销，纪要回到按词典改之前"));
  });

  it("纪要有没保存的改动时不能改", () => {
    renderPanel(glossary(), client(), { canEdit: false });
    const button = screen.getByRole("button", { name: "改过来" });
    expect(button).toBeDisabled();
    expect(button).toHaveAttribute("title", "先保存或放弃正在改的纪要");
  });

  it("手机上只看结果，不出改纪要的按钮", () => {
    renderPanel(glossary({ applied: { by: "user", count: 1, at: "2026-09-26T10:06:00+00:00", can_undo: true } }), client(), {
      isMobile: true,
    });
    expect(screen.queryByRole("button", { name: "改过来" })).toBeNull();
    expect(screen.queryByRole("button", { name: "撤销" })).toBeNull();
    expect(screen.getByText("在电脑上打开可以一键改过来")).toBeTruthy();
  });

  it("纪要改过以后悄悄重查一次；没问题时说一句没发现", async () => {
    const fresh = glossary({ stale: false, corrected: [], missed: [], checked_at: "2026-09-26T11:00:00+00:00" });
    const apiClient = client({ checkMeetingGlossary: vi.fn().mockResolvedValue({ glossary: fresh }) });
    renderPanel(glossary({ stale: true }), apiClient);
    await waitFor(() => expect(apiClient.checkMeetingGlossary).toHaveBeenCalledWith("vm-1"));
    expect(await screen.findByText("没发现要改的错写")).toBeTruthy();
    expect(apiClient.checkMeetingGlossary).toHaveBeenCalledTimes(1);
  });

  it("没归项目、没有回执时说只按公共词查", () => {
    renderPanel(
      glossary({ basis: "public", project: null, meeting_project: null, receipt: null, corrected: [], missed: [] }),
    );
    expect(screen.getByText("这场会还没归项目，只按公共词查了一遍")).toBeTruthy();
  });
});
