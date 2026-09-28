import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";

import { ApiError, type ApiClient } from "../api";
import type { MaterialPair, MaterialWord } from "../types";
import { LinksFlagsContext } from "./links/LinksFlagsContext";
import { MATERIAL_WORDS_FOOTNOTE, MaterialWords, shortName } from "./MaterialWords";

const FLAGS = { linksEnabled: true, semanticEnabled: true, llmConfigured: true };

const PAIRED: MaterialWord = {
  key: "司美格鲁肽",
  term: "司美格鲁肽",
  existing_term: null,
  wrongs: [
    { text: "司美格鲁太", meetings: 2 },
    { text: "司美格鲁泰", meetings: 1 },
  ],
  files: 30,
  spoken: 0,
  heard: [
    {
      meeting: { id: "m-1", title: "周会", date: "2026-09-26" },
      start_ms: 754_000,
      quote: "这次司美格鲁太的剂量先按",
      audio_url: "/api/media/1",
    },
  ],
  file_names: [
    { file_id: 1, name: "入组标准.pdf" },
    { file_id: 2, name: "方案v1.docx" },
  ],
  file_quote: null,
};

const SPOKEN: MaterialWord = {
  key: "驻场服务",
  term: "驻场服务",
  existing_term: null,
  wrongs: [],
  files: 15,
  spoken: 238,
  heard: [
    { meeting: { id: "m-2", title: "例会", date: "2026-09-20" }, start_ms: 60_000, quote: "驻场服务按月结", audio_url: null },
  ],
  file_names: [{ file_id: 3, name: "合同.docx" }],
  file_quote: null,
};

const UNSPOKEN: MaterialWord = {
  key: "甲状腺髓样癌",
  term: "甲状腺髓样癌",
  existing_term: { id: "gt-1", term: "甲状腺髓样癌" },
  wrongs: [],
  files: 3,
  spoken: 0,
  heard: [],
  file_names: [{ file_id: 1, name: "入组标准.pdf" }],
  file_quote: { file_id: 1, quote: "按甲状腺髓样癌病史排除" },
};

function later(ms: number) {
  return new Date(Date.now() + ms).toISOString();
}

function makeClient(overrides: Record<string, unknown> = {}) {
  return {
    acceptGlossaryCandidate: vi.fn(async () => ({
      term: { id: "gt-9", term: "司美格鲁肽", aliases: ["司美格鲁太"], is_cue: false },
      created: true,
      added_aliases: ["司美格鲁太"],
      skipped_aliases: [],
      already: false,
      text: "已记入『司美格鲁肽』，错写：司美格鲁太",
      undo_until: later(600_000),
    })),
    rejectGlossaryCandidate: vi.fn(async () => ({ text: "以后不再提『驻场服务』", undo_until: later(600_000) })),
    undoGlossaryCandidate: vi.fn(async () => ({ status: "pending", text: "已撤销，『驻场服务』回到这里" })),
    ...overrides,
  } as unknown as ApiClient;
}

function withFlags(children: ReactNode, flags: typeof FLAGS | null = FLAGS) {
  return <LinksFlagsContext.Provider value={flags}>{children}</LinksFlagsContext.Provider>;
}

function renderBoard(props: Partial<Parameters<typeof MaterialWords>[0]> = {}, flags: typeof FLAGS | null = FLAGS) {
  const apiClient = props.apiClient ?? makeClient();
  const onOpenMeeting = vi.fn();
  const onMore = vi.fn();
  const onAnswered = vi.fn();
  render(
    withFlags(
      <MaterialWords
        apiClient={apiClient}
        canWrite
        items={[PAIRED, SPOKEN, UNSPOKEN]}
        onAnswered={onAnswered}
        onMore={onMore}
        onOpenMeeting={onOpenMeeting}
        projectId="p-yt"
        projectName="云图AI"
        total={9}
        variant="board"
        {...props}
      />,
      flags,
    ),
  );
  return { apiClient, onOpenMeeting, onMore, onAnswered };
}

describe("MaterialWords", () => {
  it("renders each kind of item without percentages", async () => {
    const { onOpenMeeting, onMore } = renderBoard();
    expect(screen.getByText("从材料里找到的词")).toBeInTheDocument();
    const items = screen.getAllByRole("listitem");
    expect(within(items[0]).getByText("司美格鲁肽")).toBeInTheDocument();
    expect(within(items[0]).getByText("会上可能听成了：")).toBeInTheDocument();
    expect(within(items[0]).getByTitle("2 场会里听到")).toHaveTextContent("『司美格鲁太』");
    expect(items[0]).toHaveTextContent("『这次司美格鲁太的剂量先按』 · 周会 9/26 00:12:34");
    expect(items[0]).toHaveTextContent("在 30 个文件里：入组标准.pdf、方案v1.docx 等");
    expect(items[1]).toHaveTextContent("会上说过 238 次 · 『驻场服务按月结』 · 例会 9/20");
    expect(items[1]).toHaveTextContent("在 15 个文件里：合同.docx 等");
    expect(items[2]).toHaveTextContent("『…按甲状腺髓样癌病史排除…』 · 入组标准.pdf");
    expect(within(items[2]).getByRole("button", { name: "记到『甲状腺髓样癌』" })).toBeInTheDocument();
    expect(screen.getByText(MATERIAL_WORDS_FOOTNOTE)).toBeInTheDocument();
    expect(document.body.textContent).not.toContain("%");

    await userEvent.click(within(items[0]).getByRole("button", { name: "00:12:34" }));
    expect(onOpenMeeting).toHaveBeenCalledWith("m-1", 754_000);
    await userEvent.click(screen.getByRole("button", { name: "还有 6 个" }));
    expect(onMore).toHaveBeenCalled();
  });

  it("sends the removed chips as not_wrong and shows the undo hint", async () => {
    const { apiClient, onAnswered } = renderBoard();
    const first = screen.getAllByRole("listitem")[0];
    await userEvent.click(within(first).getByRole("button", { name: "不是听错：司美格鲁泰" }));
    expect(within(first).queryByText("『司美格鲁泰』")).not.toBeInTheDocument();
    await userEvent.click(within(first).getByRole("button", { name: "记入 云图AI" }));
    expect(apiClient.acceptGlossaryCandidate).toHaveBeenCalledWith("p-yt", {
      key: "司美格鲁肽",
      not_wrong: ["司美格鲁泰"],
    });
    expect(await screen.findByText("已记入『司美格鲁肽』，错写：司美格鲁太")).toBeInTheDocument();
    expect(onAnswered).toHaveBeenCalled();
    expect(screen.queryByText("司美格鲁肽", { selector: "strong" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "撤销" })).toBeInTheDocument();
  });

  it("rejects and undoes", async () => {
    const { apiClient } = renderBoard();
    const second = screen.getAllByRole("listitem")[1];
    await userEvent.click(within(second).getByRole("button", { name: "不是" }));
    expect(apiClient.rejectGlossaryCandidate).toHaveBeenCalledWith("p-yt", { key: "驻场服务" });
    expect(await screen.findByText("以后不再提『驻场服务』")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "撤销" }));
    expect(apiClient.undoGlossaryCandidate).toHaveBeenCalledWith("p-yt", { key: "驻场服务" });
    expect(await screen.findByText("已撤销，『驻场服务』回到这里")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "撤销" })).not.toBeInTheDocument();
  });

  it("shows server sentences for 404 and 409", async () => {
    const apiClient = makeClient({
      acceptGlossaryCandidate: vi.fn(async () => {
        throw new ApiError("这个词已经不在了", 404, { detail: "这个词已经不在了" });
      }),
      rejectGlossaryCandidate: vi.fn(async () => {
        throw new ApiError("这个词已经处理过了", 409, { detail: "这个词已经处理过了" });
      }),
    });
    renderBoard({ apiClient });
    await userEvent.click(within(screen.getAllByRole("listitem")[0]).getByRole("button", { name: "记入 云图AI" }));
    expect(await screen.findByText("这个词已经不在了")).toBeInTheDocument();
    expect(screen.getAllByRole("listitem")).toHaveLength(2);
    await userEvent.click(within(screen.getAllByRole("listitem")[0]).getByRole("button", { name: "不是" }));
    expect(await screen.findByText("这个词已经处理过了")).toBeInTheDocument();
  });

  it("is not shown without write access, on an old board, or with an old backend", () => {
    const { container } = render(
      withFlags(
        <MaterialWords apiClient={makeClient()} canWrite={false} items={[PAIRED]} projectId="p" projectName="云图AI" variant="board" />,
      ),
    );
    expect(container).toBeEmptyDOMElement();
    const oldBoard = render(
      withFlags(<MaterialWords apiClient={makeClient()} canWrite items={undefined} projectId="p" projectName="云图AI" variant="board" />),
    );
    expect(oldBoard.container).toBeEmptyDOMElement();
    const oldBackend = render(
      withFlags(<MaterialWords apiClient={makeClient()} canWrite items={[PAIRED]} projectId="p" projectName="云图AI" variant="board" />, null),
    );
    expect(oldBackend.container).toBeEmptyDOMElement();
    const empty = render(
      withFlags(<MaterialWords apiClient={makeClient()} canWrite items={[]} projectId="p" projectName="云图AI" variant="board" />),
    );
    expect(empty.container).toBeEmptyDOMElement();
  });

  it("truncates long project names and titles the page block", () => {
    expect(shortName("一二三四五六七八九十")).toBe("一二三四五六七八…");
    render(
      withFlags(
        <MaterialWords
          apiClient={makeClient()}
          canWrite
          items={[SPOKEN]}
          projectId="p"
          projectName="一二三四五六七八九十"
          variant="page"
        />,
      ),
    );
    expect(screen.getByText("从一二三四五六七八九十的材料里找到的词")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "记入 一二三四五六七八…" })).toBeInTheDocument();
  });

  it("meeting rows: no chips, no footnote", async () => {
    const pair: MaterialPair = {
      key: "司美格鲁肽",
      term: "司美格鲁肽",
      wrong: "司美格鲁太",
      start_ms: 754_000,
      quote: "这次司美格鲁太的剂量先按",
      project: { id: "p-yt", name: "云图AI" },
    };
    const apiClient = makeClient();
    const onAnswered = vi.fn();
    render(
      withFlags(
        <MaterialWords
          apiClient={apiClient}
          canWrite
          onAnswered={onAnswered}
          pairs={[pair]}
          projectId="p-yt"
          projectName="云图AI"
          variant="meeting"
        />,
      ),
    );
    expect(screen.getByText("材料里写作『司美格鲁肽』，这场会听成了『司美格鲁太』")).toBeInTheDocument();
    expect(screen.queryByText(MATERIAL_WORDS_FOOTNOTE)).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "记入 云图AI" }));
    await waitFor(() => expect(onAnswered).toHaveBeenCalled());
  });
  it("meeting rows: accept records only the shown wrong; rows of one word keep apart", async () => {
    const base = { key: "司美格鲁肽", term: "司美格鲁肽", start_ms: 0, quote: "", project: { id: "p-yt", name: "云图AI" } };
    const pairs: MaterialPair[] = [
      { ...base, wrong: "司美格鲁太" },
      { ...base, wrong: "司美格鲁泰" },
    ];
    const apiClient = makeClient();
    render(
      withFlags(
        <MaterialWords apiClient={apiClient} canWrite pairs={pairs} projectId="p-yt" projectName="云图AI" variant="meeting" />,
      ),
    );
    const rows = screen.getAllByText(/这场会听成了/);
    expect(rows).toHaveLength(2);
    await userEvent.click(screen.getAllByRole("button", { name: "记入 云图AI" })[0]);
    expect(apiClient.acceptGlossaryCandidate).toHaveBeenCalledWith("p-yt", { key: "司美格鲁肽", only_wrong: "司美格鲁太" });
    await screen.findByText("已记入『司美格鲁肽』，错写：司美格鲁太");
    expect(screen.queryByText("材料里写作『司美格鲁肽』，这场会听成了『司美格鲁太』")).not.toBeInTheDocument();
    expect(screen.getByText("材料里写作『司美格鲁肽』，这场会听成了『司美格鲁泰』")).toBeInTheDocument();
  });

  it("an existing term with every chip removed cannot be accepted", async () => {
    const onto: MaterialWord = { ...PAIRED, key: "能耗看板", term: "能耗看板", existing_term: { id: "gt-1", term: "能耗看板" } };
    const { apiClient } = renderBoard({ items: [onto] });
    const item = screen.getAllByRole("listitem")[0];
    const button = within(item).getByRole("button", { name: "记到『能耗看板』" });
    await userEvent.click(within(item).getByRole("button", { name: "不是听错：司美格鲁太" }));
    expect(button).toBeEnabled();
    await userEvent.click(within(item).getByRole("button", { name: "不是听错：司美格鲁泰" }));
    expect(button).toBeDisabled();
    await userEvent.click(button);
    expect(apiClient.acceptGlossaryCandidate).not.toHaveBeenCalled();
    expect(within(item).getByRole("button", { name: "不是" })).toBeEnabled();
  });

  it("keeps the first item greyed while a second answer is in flight", async () => {
    let finishAccept: (value: unknown) => void = () => undefined;
    let finishReject: (value: unknown) => void = () => undefined;
    const apiClient = makeClient({
      acceptGlossaryCandidate: vi.fn(() => new Promise((resolve) => { finishAccept = resolve; })),
      rejectGlossaryCandidate: vi.fn(() => new Promise((resolve) => { finishReject = resolve; })),
    });
    renderBoard({ apiClient });
    const [first, second] = screen.getAllByRole("listitem");
    const acceptFirst = within(first).getByRole("button", { name: "记入 云图AI" });
    await userEvent.click(acceptFirst);
    await userEvent.click(within(second).getByRole("button", { name: "不是" }));
    expect(acceptFirst).toBeDisabled();
    expect(within(second).getByRole("button", { name: "不是" })).toBeDisabled();
    finishReject({ text: "以后不再提『驻场服务』", undo_until: later(600_000) });
    await screen.findByText("以后不再提『驻场服务』");
    expect(acceptFirst).toBeDisabled();
    finishAccept({
      term: { id: "gt-9", term: "司美格鲁肽", aliases: [], is_cue: false }, created: true, added_aliases: [],
      skipped_aliases: [], already: false, text: "已记入『司美格鲁肽』", undo_until: later(600_000),
    });
    await screen.findByText("已记入『司美格鲁肽』");
  });
});
