import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { MaterialSearchItem, Project, SearchItem, SearchPayload } from "../types";
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

function renderPage(
  result: SearchPayload | null,
  props: { scope?: string; state?: "ready" | "loading"; query?: string } = {},
) {
  const onScopeChange = vi.fn();
  const onSearchWord = vi.fn();
  const onOpen = vi.fn();
  const onOpenMaterial = vi.fn();
  render(
    <SearchPage
      onOpen={onOpen}
      onOpenMaterial={onOpenMaterial}
      onScopeChange={onScopeChange}
      onSearchWord={onSearchWord}
      projects={projects}
      query={props.query ?? "数理协会"}
      result={result}
      scope={props.scope ?? ""}
      state={props.state ?? "ready"}
    />,
  );
  return { onScopeChange, onSearchWord, onOpen, onOpenMaterial };
}

function material(overrides: Partial<MaterialSearchItem> = {}): MaterialSearchItem {
  return {
    file_id: 7,
    content_key: "k-1",
    name: "协会方案.docx",
    ext: "docx",
    path: "/Volumes/资料盘/云图/协会方案.docx",
    rel_path: "协会方案.docx",
    folder_path: "/Volumes/资料盘/云图",
    root_id: 1,
    project_id: "p-yt",
    project_name: "云图AI",
    project_color: "#123456",
    modified_at: "2026-09-20T08:00:00Z",
    root_online: true,
    playable: false,
    copies: 0,
    name_hit: false,
    hits: [{ kind: "pdf", loc: "第 3 页", start_ms: null, text: "成立数理协会的方案", matched: "数理协会" }],
    more_hits: 2,
    state_text: null,
    mentioned_meetings: 3,
    ...overrides,
  };
}

const EMPTY_STATE = { pending: 0, rebuilding: false, partial: false };

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

describe("SearchPage 材料（3f）", () => {
  it("「材料里的」一节在会议命中下面、意思相近上面；计数写会议几条、材料几份", () => {
    renderPage({
      mode: "hybrid",
      items: [hit()],
      similar: [hit({ segment_id: "seg-9", text: "协会的事", matched: "" })],
      materials: [material()],
      material_similar: [],
      material_state: EMPTY_STATE,
    });

    expect(screen.getByText("会议 1 条 · 材料 1 份")).toBeInTheDocument();
    const sections = screen.getAllByRole("region").map((section) => section.getAttribute("aria-label"));
    expect(sections).toEqual(["材料里的", "意思相近的"]);
    const section = screen.getByRole("region", { name: "材料里的" });
    expect(within(section).getByText("正文命中")).toBeInTheDocument();
    expect(within(section).getByText("第 3 页")).toBeInTheDocument();
    expect(within(section).getByText("数理协会", { selector: "mark" })).toBeInTheDocument();
    expect(within(section).getByText(/另有 2 处/)).toBeInTheDocument();
    expect(within(section).getByText(/在 3 场会上被提到/)).toBeInTheDocument();
  });

  it("没有材料时只写会议那半句；［预览］和录音的 ▶ 打开预览抽屉", () => {
    const { onOpenMaterial } = renderPage({
      mode: "hybrid",
      items: [hit()],
      materials: [
        material({
          ext: "m4a",
          name: "访谈.m4a",
          playable: true,
          hits: [{ kind: "media", loc: null, start_ms: 92_000, text: "数理协会下周开会", matched: "数理协会" }],
        }),
      ],
      material_state: EMPTY_STATE,
    });
    fireEvent.click(screen.getByRole("button", { name: "从 01:32 放 访谈.m4a" }));
    expect(onOpenMaterial).toHaveBeenLastCalledWith(7, 92_000);
    fireEvent.click(screen.getByRole("button", { name: "预览" }));
    expect(onOpenMaterial).toHaveBeenLastCalledWith(7);
    expect(screen.getByText("录音文字命中")).toBeInTheDocument();
  });

  it("只按文件名命中又读不了的写原因；盘不在写资料盘未连接", () => {
    renderPage({
      mode: "hybrid",
      items: [],
      materials: [
        material({ file_id: 1, name: "数理协会.pdf", name_hit: true, hits: [], more_hits: 0, state_text: "读不了：要密码" }),
        material({ file_id: 2, root_online: false, state_text: "资料盘未连接" }),
      ],
      material_state: EMPTY_STATE,
    });
    expect(screen.getByText("文件名命中 · 读不了：要密码")).toBeInTheDocument();
    expect(screen.getByText("正文命中 · 资料盘未连接")).toBeInTheDocument();
  });

  it("会议里没有、材料里有：会议那句换成「会议里没有包含」，不写没搜到", () => {
    renderPage({ mode: "hybrid", items: [], similar: [], materials: [material()], material_state: EMPTY_STATE });
    expect(screen.getByText("会议里没有包含「数理协会」的内容")).toBeInTheDocument();
    expect(screen.queryByText(/没搜到/)).toBeNull();
    expect(screen.getByText("会议 0 条 · 材料 1 份")).toBeInTheDocument();
  });

  it("范围选「没归项目的会」时不显示材料", () => {
    renderPage(
      { mode: "hybrid", items: [hit()], materials: [material()], material_state: { ...EMPTY_STATE, pending: 3 } },
      { scope: "none" },
    );
    expect(screen.queryByRole("region", { name: "材料里的" })).toBeNull();
    expect(screen.getByText("会议 1 条")).toBeInTheDocument();
  });

  it("只有材料意思相近时「意思相近的」也出现，标「材料」", () => {
    renderPage({
      mode: "hybrid",
      items: [],
      similar: [],
      materials: [],
      material_similar: [material({ name: "交付计划.docx", score: 0.7 })],
      material_state: EMPTY_STATE,
    });
    const section = screen.getByRole("region", { name: "意思相近的" });
    expect(within(section).getByText("交付计划.docx")).toBeInTheDocument();
    expect(within(section).getByText("材料")).toBeInTheDocument();
    expect(within(section).queryByText("数理协会", { selector: "mark" })).toBeNull();
  });

  it("四种灰字；没有材料命中时灰字照样显示在「材料里的」下面", () => {
    renderPage(
      {
        mode: "hybrid",
        items: [],
        similar: [],
        materials: [],
        material_state: { pending: 1210, rebuilding: true, partial: true },
      },
      { query: "随访" },
    );
    const section = screen.getByRole("region", { name: "材料里的" });
    expect(within(section).getByText("还有 1,210 个材料没读完，结果可能不全")).toBeInTheDocument();
    expect(within(section).getByText("材料的全文索引在重建，结果可能不全")).toBeInTheDocument();
    expect(within(section).getByText("材料结果可能不全")).toBeInTheDocument();
    expect(within(section).getByText("两个字的词只搜了文件名，选个项目能搜正文")).toBeInTheDocument();
    expect(screen.queryByText(/没搜到/)).toBeNull();
  });

  it("选了项目时两个字的词不写只搜了文件名；什么都没有时写没搜到", () => {
    renderPage(
      { mode: "hybrid", items: [], similar: [], materials: [], material_state: EMPTY_STATE },
      { query: "随访", scope: "p-yt" },
    );
    expect(screen.queryByText("两个字的词只搜了文件名，选个项目能搜正文")).toBeNull();
    expect(screen.getByText(/没搜到/)).toBeInTheDocument();
  });
});
