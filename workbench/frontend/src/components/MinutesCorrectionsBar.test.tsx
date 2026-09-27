import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { ApiClient } from "../api";
import type { GlossarySuggestion, Project } from "../types";
import { MinutesCorrectionsBar } from "./MinutesCorrectionsBar";

const PROJECTS: Project[] = [
  { id: "p-yt", name: "云图AI", color: "#2c8d83", origin: "manual", created_at: "" } as Project,
  { id: "p-sj", name: "数据中台", color: "#667085", origin: "manual", created_at: "" } as Project,
];

function suggestion(overrides: Partial<GlossarySuggestion> = {}): GlossarySuggestion {
  return {
    id: "gs-1",
    wrong: "树立协会",
    correct: "数理协会",
    scope: "云图AI",
    meeting_id: "m-1",
    context: null,
    status: "pending",
    created_at: "",
    updated_at: "",
    alt_wrong: "树立",
    alt_correct: "数理",
    target_project_id: "p-yt",
    target_project_name: "云图AI",
    auto_recorded: false,
    ...overrides,
  };
}

function client(overrides: Partial<ApiClient> = {}) {
  return {
    confirmGlossarySuggestion: vi.fn().mockImplementation(async (_id: string, options: { target?: string }) => ({
      ok: true,
      created: true,
      wrong: "树立协会",
      correct: "数理协会",
      term: { project_name: options.target === "public" ? null : options.target === "p-sj" ? "数据中台" : "云图AI" },
      suggestion: null,
    })),
    rejectGlossarySuggestion: vi.fn().mockResolvedValue({ ok: true }),
    restoreGlossarySuggestion: vi.fn().mockResolvedValue({ ok: true, suggestion: null }),
    undoGlossarySuggestion: vi.fn().mockResolvedValue({ ok: true, suggestion: null }),
    ...overrides,
  } as unknown as ApiClient;
}

describe("编辑器下方的错字更正提示条", () => {
  it("记入会议所在的项目，记完可以撤销", async () => {
    const apiClient = client();
    const onChanged = vi.fn();
    render(
      <MinutesCorrectionsBar
        apiClient={apiClient}
        corrections={[suggestion(), suggestion({ id: "gs-2", wrong: "岳总", correct: "月总", alt_wrong: null, alt_correct: null })]}
        onChanged={onChanged}
        onClose={vi.fn()}
        projects={PROJECTS}
      />,
    );

    expect(screen.getByText("2 处像是错字更正")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "记入 云图AI" }));
    expect(apiClient.confirmGlossarySuggestion).toHaveBeenCalledWith("gs-1", { target: "auto", short: false });
    expect(apiClient.confirmGlossarySuggestion).toHaveBeenCalledWith("gs-2", { target: "auto", short: false });
    expect(await screen.findAllByText("已记入 云图AI")).toHaveLength(2);
    expect(onChanged).toHaveBeenCalled();

    await userEvent.click(screen.getAllByRole("button", { name: "撤销" })[0]);
    expect(apiClient.undoGlossarySuggestion).toHaveBeenCalledWith("gs-1");
    expect(await screen.findByText("1 处像是错字更正")).toBeInTheDocument();
  });

  it("▾ 里改记到公共或别的项目；勾「只记 2 字」记原来那一对", async () => {
    const apiClient = client();
    render(
      <MinutesCorrectionsBar apiClient={apiClient} corrections={[suggestion()]} onClose={vi.fn()} projects={PROJECTS} />,
    );

    await userEvent.click(screen.getByRole("checkbox", { name: "只记 2 字" }));
    expect(screen.getByText("树立")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "改记到别处" }));
    expect(screen.getAllByRole("menuitem").map((item) => item.textContent)).toEqual(["记入 公共", "记入 数据中台"]);
    await userEvent.click(screen.getByRole("menuitem", { name: "记入 公共" }));
    expect(apiClient.confirmGlossarySuggestion).toHaveBeenCalledWith("gs-1", { target: "public", short: true });
    expect(await screen.findByText("已记入 公共")).toBeInTheDocument();
  });

  it("会还没定项目时默认记公共，并说明定了项目再记", async () => {
    const apiClient = client();
    render(
      <MinutesCorrectionsBar
        apiClient={apiClient}
        corrections={[suggestion({ target_project_id: null, target_project_name: null })]}
        onClose={vi.fn()}
        projects={PROJECTS}
      />,
    );

    expect(screen.getByText(/这场会还没定项目，定了以后再记/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "记入 公共" }));
    expect(apiClient.confirmGlossarySuggestion).toHaveBeenCalledWith("gs-1", { target: "public", short: false });
  });

  it("已自动记入的给撤销；「不是错字」之后能恢复；不点就收起", async () => {
    const apiClient = client();
    const onClose = vi.fn();
    render(
      <MinutesCorrectionsBar
        apiClient={apiClient}
        corrections={[
          suggestion({ status: "confirmed", auto_recorded: true, existing_term_id: "gt-1", existing_term_project_name: "云图AI" }),
          suggestion({ id: "gs-2", wrong: "岳总", correct: "月总", alt_wrong: null, alt_correct: null }),
        ]}
        onClose={onClose}
        projects={PROJECTS}
      />,
    );

    expect(screen.getByText("已自动记入『数理协会』（云图AI）")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "不是错字" }));
    expect(apiClient.rejectGlossarySuggestion).toHaveBeenCalledWith("gs-2");
    await userEvent.click(await screen.findByRole("button", { name: "恢复" }));
    expect(apiClient.restoreGlossarySuggestion).toHaveBeenCalledWith("gs-2");
    await userEvent.click(await screen.findByRole("button", { name: "稍后在词典里处理" }));
    expect(onClose).toHaveBeenCalled();
  });

  it("词典里已有这个词时说明会加到那条", () => {
    render(
      <MinutesCorrectionsBar
        apiClient={client()}
        corrections={[suggestion({ existing_term_id: "gt-1", existing_term_project_name: null })]}
        onClose={vi.fn()}
        projects={PROJECTS}
      />,
    );
    expect(screen.getByText("词典里已有『数理协会』（公共），会加到那条")).toBeInTheDocument();
  });
});
