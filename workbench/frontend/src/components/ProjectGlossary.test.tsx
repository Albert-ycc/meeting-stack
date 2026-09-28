import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { ApiClient } from "../api";
import type { MaterialWord } from "../types";
import { LinksFlagsContext } from "./links/LinksFlagsContext";
import { ProjectGlossary } from "./ProjectGlossary";

const WORD: MaterialWord = {
  key: "驻场服务",
  term: "驻场服务",
  existing_term: null,
  wrongs: [],
  files: 15,
  spoken: 3,
  heard: [],
  file_names: [],
  file_quote: null,
};

function client() {
  return {
    createGlossaryTerm: vi.fn(),
    mergeGlossaryTerm: vi.fn(),
    acceptGlossaryCandidate: vi.fn(),
    rejectGlossaryCandidate: vi.fn(),
    undoGlossaryCandidate: vi.fn(),
  } as unknown as ApiClient;
}

function renderGlossary(candidates: MaterialWord[] | undefined, total = 1) {
  const onOpenGlossary = vi.fn();
  render(
    <LinksFlagsContext.Provider value={{ linksEnabled: true, semanticEnabled: false, llmConfigured: false }}>
      <ProjectGlossary
        apiClient={client()}
        candidateTotal={total}
        candidates={candidates}
        canWrite
        onChanged={vi.fn()}
        onOpenGlossary={onOpenGlossary}
        projectId="p-yt"
        projectName="云图AI"
        publicCount={2}
        terms={[{ id: "gt-1", term: "能耗看板", aliases: ["能耗看版"], category: "其他", also: [] }]}
        total={1}
      />
    </LinksFlagsContext.Provider>,
  );
  return { onOpenGlossary };
}

describe("ProjectGlossary", () => {
  it("puts the words found in materials under the term list and before the links", () => {
    renderGlossary([WORD]);
    const block = screen.getByRole("region", { name: "从材料里找到的词" });
    const list = screen.getByText("能耗看板").closest("ul")!;
    const publicLink = screen.getByRole("button", { name: "另有 2 条公共词也会用于本项目 →" });
    expect(list.compareDocumentPosition(block) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(block.compareDocumentPosition(publicLink) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it("opens the glossary page with this project selected from 还有 N 个", async () => {
    const { onOpenGlossary } = renderGlossary([WORD], 9);
    await userEvent.click(screen.getByRole("button", { name: "还有 8 个" }));
    expect(onOpenGlossary).toHaveBeenCalledWith("p-yt");
  });

  it("shows nothing when the board has no such field", () => {
    renderGlossary(undefined);
    expect(screen.queryByText("从材料里找到的词")).not.toBeInTheDocument();
  });
});
