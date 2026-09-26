import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { Project, SearchItem, SearchPayload } from "../types";
import { SearchPage } from "./SearchPage";

function hit(overrides: Partial<SearchItem> = {}): SearchItem {
  return {
    segment_id: "seg-1",
    meeting_id: "vm-1",
    title: "周会",
    start_ms: 1_000,
    end_ms: 2_000,
    text: "会上说要成立树立协会",
    match_kind: "segment",
    matched: "树立协会",
    ...overrides,
  };
}

const projects = [
  { id: "p-yt", name: "云图AI" },
  { id: "p-zt", name: "数据中台" },
] as Project[];

function renderPage(result: SearchPayload | null, props: { scope?: string; state?: "ready" | "loading" } = {}) {
  const onScopeChange = vi.fn();
  const onSearchWord = vi.fn();
  const onOpen = vi.fn();
  render(
    <SearchPage
      onOpen={onOpen}
      onScopeChange={onScopeChange}
      onSearchWord={onSearchWord}
      projects={projects}
      query="数理协会"
      result={result}
      scope={props.scope ?? ""}
      state={props.state ?? "ready"}
    />,
  );
  return { onScopeChange, onSearchWord, onOpen };
}

describe("SearchPage", () => {
  it("说清同时搜了哪些写法，两个字的写法点一下再搜", () => {
    const { onSearchWord } = renderPage({
      mode: "hybrid",
      items: [hit()],
      similar: [],
      expanded: ["树立协会"],
      expand_hints: ["数协"],
    });

    expect(screen.getByText("同时搜了：树立协会")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "数协" }));
    expect(onSearchWord).toHaveBeenCalledWith("数协");
  });

  it("原词命中在前，意思相近的另列一段且不高亮", () => {
    renderPage({
      mode: "hybrid",
      items: [hit()],
      similar: [hit({ segment_id: "seg-2", text: "协会筹备进度", matched: undefined, score: 0.7 })],
    });

    const similar = screen.getByRole("region", { name: "意思相近的" });
    expect(similar).toHaveTextContent("协会筹备进度");
    expect(similar.querySelector("mark")).toBeNull();
    expect(screen.getAllByText("树立协会", { selector: "mark" })).toHaveLength(1);
  });

  it("换范围；在项目里搜时补一行没归项目的会还有几条", () => {
    const { onScopeChange } = renderPage(
      { mode: "hybrid", items: [], similar: [], unattributed_hits: 2 },
      { scope: "p-yt" },
    );

    expect(screen.getByText("这个范围里没有包含「数理协会」的内容。")).toBeInTheDocument();
    expect(screen.getByText(/还有 2 条来自没归项目的会/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "看看" }));
    expect(onScopeChange).toHaveBeenLastCalledWith("none");

    fireEvent.change(screen.getByLabelText("搜索范围"), { target: { value: "p-zt" } });
    expect(onScopeChange).toHaveBeenLastCalledWith("p-zt");
  });

  it("意思相近的搜不了时说明原因，原词命中照常列", () => {
    renderPage({
      mode: "hybrid",
      items: [hit()],
      similar: [],
      semantic_unavailable: "正在转写，意思相近的结果等转写完再搜",
    });

    expect(screen.getByText("意思相近的这次没搜：正在转写，意思相近的结果等转写完再搜")).toBeInTheDocument();
    expect(screen.getByText("逐字稿命中")).toBeInTheDocument();
  });

  it("什么都没搜到时给一句建议", () => {
    renderPage({ mode: "hybrid", items: [], similar: [] });
    expect(screen.getByText("没搜到。试试更短的词，或者把范围换成全部项目。")).toBeInTheDocument();
  });
});
