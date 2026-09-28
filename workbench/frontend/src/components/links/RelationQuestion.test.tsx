import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState, type ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";

import { ApiError, type ApiClient, type RelationQuestion as Question } from "../../api";
import { LinksFlagsContext } from "./LinksFlagsContext";
import { RelationQuestion } from "./RelationQuestion";
import { OLD_BACKEND_TEXT, RecentAnswersContext, createRecentAnswerStore, type RecentAnswerStore } from "./useRelationAnswer";

const FLAGS = { linksEnabled: true, semanticEnabled: true, llmConfigured: true };

const AFFECTS: Question = {
  relation_id: 57,
  kind: "affects",
  text: "可能过时：9/21 决议『总价下调 5%』",
  decision: {
    id: "dec-3f2a9c0b1d4e5f60",
    text: "总价下调 5%",
    date: "2026-09-21",
    meeting_id: "m-81",
    meeting_title: "报价沟通",
    start_ms: 754_000,
    audio_url: "/api/media/412",
  },
  passage: { loc: "第 2 页", text: "…总价在原基础上下调 3%，含税…" },
  file: { id: 812, name: "报价单 v3.xlsx" },
  answers: ["updated", "no"],
};

const PRODUCED: Question = {
  relation_id: 61,
  kind: "produced",
  text: "会后 3 天新增在『能耗看板/』",
  ask: "是任务『写一版方案』的交付物吗？",
  task: { id: "t-19", title: "写一版方案", status: "in_progress" },
  file: { id: 930, name: "能耗看板方案.key", folder: "能耗看板/" },
  words: ["能耗看板"],
  answers: ["yes", "no"],
};

function later(ms: number) {
  return new Date(Date.now() + ms).toISOString();
}

function makeClient(overrides: Record<string, unknown> = {}) {
  return {
    answerRelation: vi.fn(async () => ({ relation: {}, undo_until: later(600_000), deliverable_id: null })),
    undoRelation: vi.fn(async () => ({ relation: {}, removed_deliverable_id: null })),
    ...overrides,
  } as unknown as Pick<ApiClient, "answerRelation" | "undoRelation">;
}

function Providers({ children, store }: { children: ReactNode; store?: RecentAnswerStore }) {
  const [fallback] = useState(createRecentAnswerStore);
  return (
    <LinksFlagsContext.Provider value={FLAGS}>
      <RecentAnswersContext.Provider value={store ?? fallback}>{children}</RecentAnswersContext.Provider>
    </LinksFlagsContext.Provider>
  );
}

describe("RelationQuestion", () => {
  it("第一行是 text 带 ▶，第二行是位置加片段或 ask；按钮按 answers 映射", async () => {
    const onPlay = vi.fn();
    render(
      <Providers>
        <RelationQuestion
          apiClient={makeClient()}
          canWrite
          onNotice={vi.fn()}
          onPlay={onPlay}
          questions={[AFFECTS, PRODUCED]}
          scope={{ fileId: 812 }}
        />
      </Providers>,
    );
    const affects = screen.getByRole("group", { name: AFFECTS.text });
    expect(within(affects).getByText("第 2 页：『…总价在原基础上下调 3%，含税…』")).toBeInTheDocument();
    expect(within(affects).getAllByRole("button").map((button) => button.textContent)).toEqual(["▶", "已更新", "不相关"]);
    await userEvent.click(within(affects).getByRole("button", { name: "从 12:34 播放" }));
    expect(onPlay).toHaveBeenCalledWith("/api/media/412", 754_000, "报价沟通");

    const produced = screen.getByRole("group", { name: PRODUCED.text });
    expect(within(produced).getByText("是任务『写一版方案』的交付物吗？")).toBeInTheDocument();
    expect(within(produced).getAllByRole("button").map((button) => button.textContent)).toEqual(["是", "不是"]);
  });

  it("回答：发送时按钮变灰，推一条带服务端撤销期的提示，这一块收成一行灰字加［撤销］", async () => {
    const until = later(600_000);
    let finish: (value: unknown) => void = () => {};
    const apiClient = makeClient({
      answerRelation: vi.fn(() => new Promise((resolve) => (finish = resolve))),
    });
    const onNotice = vi.fn();
    const onChanged = vi.fn();
    render(
      <Providers>
        <RelationQuestion
          apiClient={apiClient}
          canWrite
          onChanged={onChanged}
          onNotice={onNotice}
          questions={[PRODUCED]}
          scope={{ taskId: "t-19" }}
        />
      </Providers>,
    );
    await userEvent.click(screen.getByRole("button", { name: "是" }));
    expect(apiClient.answerRelation).toHaveBeenCalledWith(61, { answer: "yes" });
    expect(screen.getByRole("button", { name: "是" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "不是" })).toBeDisabled();

    finish({ relation: {}, undo_until: until, deliverable_id: 17 });
    const line = await screen.findByText("已登记为『写一版方案』的交付物");
    expect(onNotice).toHaveBeenCalledWith("已登记为『写一版方案』的交付物", {
      kind: "relation",
      relationId: 61,
      label: "已登记为『写一版方案』的交付物",
      until,
    });
    expect(onChanged).toHaveBeenCalled();
    expect(screen.queryByRole("group")).not.toBeInTheDocument();
    expect(within(line.closest("p")!).getByRole("button", { name: "撤销" })).toBeInTheDocument();
  });

  it("409「这条已经处理过了」整块消失，原样提示服务端那句（warning）；别的块照旧", async () => {
    const apiClient = makeClient({
      answerRelation: vi.fn(async () => {
        throw new ApiError("这条已经处理过了", 409, { detail: "这条已经处理过了" });
      }),
    });
    const onNotice = vi.fn();
    render(
      <Providers>
        <RelationQuestion apiClient={apiClient} canWrite onNotice={onNotice} questions={[AFFECTS, PRODUCED]} scope={{ fileId: 812 }} />
      </Providers>,
    );
    await userEvent.click(screen.getByRole("button", { name: "已更新" }));
    await waitFor(() => expect(screen.queryByRole("group", { name: AFFECTS.text })).not.toBeInTheDocument());
    expect(onNotice).toHaveBeenCalledWith("这条已经处理过了", undefined, "warning");
    expect(screen.getByRole("group", { name: PRODUCED.text })).toBeInTheDocument();
  });

  it("422 原样提示、块留着；canWrite 为假时只显示，没有按钮", async () => {
    const apiClient = makeClient({
      answerRelation: vi.fn(async () => {
        throw new ApiError("这份文件已经不在了", 422, { detail: "这份文件已经不在了" });
      }),
    });
    const onNotice = vi.fn();
    const { rerender } = render(
      <Providers>
        <RelationQuestion apiClient={apiClient} canWrite onNotice={onNotice} questions={[PRODUCED]} scope={{ fileId: 930 }} />
      </Providers>,
    );
    await userEvent.click(screen.getByRole("button", { name: "是" }));
    await waitFor(() => expect(onNotice).toHaveBeenCalledWith("这份文件已经不在了", undefined, "warning"));
    expect(screen.getByRole("group", { name: PRODUCED.text })).toBeInTheDocument();

    rerender(
      <Providers>
        <RelationQuestion apiClient={apiClient} canWrite={false} onNotice={onNotice} questions={[PRODUCED]} scope={{ fileId: 930 }} />
      </Providers>,
    );
    const block = screen.getByRole("group", { name: PRODUCED.text });
    expect(within(block).getByText("是任务『写一版方案』的交付物吗？")).toBeInTheDocument();
    expect(within(block).queryByRole("button")).not.toBeInTheDocument();
  });

  it("旧后台不显示：没包 LinksFlagsContext、没有 answerRelation、数据里没有 questions", () => {
    const { container, rerender } = render(
      <RelationQuestion apiClient={makeClient()} canWrite onNotice={vi.fn()} questions={[AFFECTS]} scope={{ fileId: 812 }} />,
    );
    expect(container).toBeEmptyDOMElement();

    rerender(
      <Providers>
        <RelationQuestion apiClient={{}} canWrite onNotice={vi.fn()} questions={[AFFECTS]} scope={{ fileId: 812 }} />
      </Providers>,
    );
    expect(screen.queryByRole("group")).not.toBeInTheDocument();

    rerender(
      <Providers>
        <RelationQuestion apiClient={makeClient()} canWrite onNotice={vi.fn()} questions={undefined} scope={{ fileId: 812 }} />
      </Providers>,
    );
    expect(screen.queryByRole("group")).not.toBeInTheDocument();
  });

  it("回答撞上旧后台（404 Not Found）：写旧后台那句，整块不再画；中文 404 是正式回答", async () => {
    const onNotice = vi.fn();
    const oldClient = makeClient({
      answerRelation: vi.fn(async () => {
        throw new ApiError("Not Found", 404, { detail: "Not Found" });
      }),
    });
    const { unmount } = render(
      <Providers>
        <RelationQuestion apiClient={oldClient} canWrite onNotice={onNotice} questions={[AFFECTS, PRODUCED]} scope={{ fileId: 812 }} />
      </Providers>,
    );
    await userEvent.click(screen.getByRole("button", { name: "已更新" }));
    await waitFor(() => expect(onNotice).toHaveBeenCalledWith(OLD_BACKEND_TEXT, undefined, "warning"));
    expect(screen.queryByRole("group")).not.toBeInTheDocument();
    unmount();

    const goneClient = makeClient({
      answerRelation: vi.fn(async () => {
        throw new ApiError("这条关联已经不在了", 404, { detail: "这条关联已经不在了" });
      }),
    });
    render(
      <Providers>
        <RelationQuestion apiClient={goneClient} canWrite onNotice={onNotice} questions={[AFFECTS, PRODUCED]} scope={{ fileId: 812 }} />
      </Providers>,
    );
    await userEvent.click(screen.getByRole("button", { name: "已更新" }));
    await waitFor(() => expect(onNotice).toHaveBeenCalledWith("这条关联已经不在了", undefined, "warning"));
    expect(screen.queryByRole("group", { name: AFFECTS.text })).not.toBeInTheDocument();
    expect(screen.getByRole("group", { name: PRODUCED.text })).toBeInTheDocument();
  });
});

describe("useRelationAnswer 的撤销期", () => {
  function Host({
    apiClient,
    questions,
    onNotice,
  }: {
    apiClient: Pick<ApiClient, "answerRelation" | "undoRelation">;
    questions: Question[];
    onNotice: () => void;
  }) {
    return (
      <RelationQuestion
        apiClient={apiClient}
        canWrite
        onNotice={onNotice}
        questions={questions}
        scope={{ fileId: 812 }}
      />
    );
  }

  it("宿主重取后问题没了，这一行接着画；宿主卸载再挂上时还在；［撤销］调 undoRelation", async () => {
    const store = createRecentAnswerStore();
    const apiClient = makeClient();
    const onNotice = vi.fn();
    const { rerender } = render(
      <Providers store={store}>
        <Host apiClient={apiClient} onNotice={onNotice} questions={[AFFECTS]} />
      </Providers>,
    );
    await userEvent.click(screen.getByRole("button", { name: "不相关" }));
    expect(await screen.findByText("已记下：和这条决议不相关")).toBeInTheDocument();

    // 宿主重取：问题从数据里没了
    rerender(
      <Providers store={store}>
        <Host apiClient={apiClient} onNotice={onNotice} questions={[]} />
      </Providers>,
    );
    expect(screen.getByText("已记下：和这条决议不相关")).toBeInTheDocument();

    // 宿主关掉再打开
    rerender(<Providers store={store}>{null}</Providers>);
    expect(screen.queryByText("已记下：和这条决议不相关")).not.toBeInTheDocument();
    rerender(
      <Providers store={store}>
        <Host apiClient={apiClient} onNotice={onNotice} questions={[]} />
      </Providers>,
    );
    const line = screen.getByText("已记下：和这条决议不相关").closest("p")!;

    // 别的宿主（别的文件）看不到这一行
    rerender(
      <Providers store={store}>
        <RelationQuestion apiClient={apiClient} canWrite onNotice={onNotice} questions={[]} scope={{ fileId: 930 }} />
      </Providers>,
    );
    expect(screen.queryByText("已记下：和这条决议不相关")).not.toBeInTheDocument();
    rerender(
      <Providers store={store}>
        <Host apiClient={apiClient} onNotice={onNotice} questions={[]} />
      </Providers>,
    );

    await userEvent.click(within(screen.getByText("已记下：和这条决议不相关").closest("p")!).getByRole("button", { name: "撤销" }));
    expect(apiClient.undoRelation).toHaveBeenCalledWith(57);
    await waitFor(() => expect(screen.queryByText("已记下：和这条决议不相关")).not.toBeInTheDocument());
    expect(onNotice).toHaveBeenLastCalledWith("已撤销");
    expect(line).not.toBeInTheDocument();
  });

  it("这一行留到服务端给的 undo_until，过期就收起", async () => {
    const apiClient = makeClient({
      answerRelation: vi.fn(async () => ({ relation: {}, undo_until: later(300), deliverable_id: null })),
    });
    render(
      <Providers>
        <Host apiClient={apiClient} onNotice={vi.fn()} questions={[AFFECTS]} />
      </Providers>,
    );
    await userEvent.click(screen.getByRole("button", { name: "已更新" }));
    expect(await screen.findByText("已标为更新过")).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByText("已标为更新过")).not.toBeInTheDocument(), { timeout: 2_000 });
    // 过期后问题块回来（数据还没重取），可以再答
    expect(screen.getByRole("group", { name: AFFECTS.text })).toBeInTheDocument();
  });

  it("撤销回 409（过了撤销期、已经撤销过）：原样提示，这一行收起", async () => {
    const apiClient = makeClient({
      undoRelation: vi.fn(async () => {
        throw new ApiError("已经撤销过了", 409, { detail: "已经撤销过了" });
      }),
    });
    const onNotice = vi.fn();
    render(
      <Providers>
        <Host apiClient={apiClient} onNotice={onNotice} questions={[AFFECTS]} />
      </Providers>,
    );
    await userEvent.click(screen.getByRole("button", { name: "已更新" }));
    await userEvent.click(await screen.findByRole("button", { name: "撤销" }));
    await waitFor(() => expect(onNotice).toHaveBeenLastCalledWith("已经撤销过了", undefined, "warning"));
    expect(screen.queryByText("已标为更新过")).not.toBeInTheDocument();
  });
});

describe("RelationQuestion 在 4e 宿主里的样子", () => {
  it("产出：第一行是问法、第二行是证据；任务抽屉换成「是这条任务的交付物吗？」，文件名能点", async () => {
    const onOpenFile = vi.fn();
    render(
      <Providers>
        <RelationQuestion
          apiClient={makeClient()}
          ask="是这条任务的交付物吗？"
          canWrite
          onNotice={vi.fn()}
          onOpenFile={onOpenFile}
          questions={[PRODUCED]}
          scope={{ taskId: "t-19" }}
        />
      </Providers>,
    );
    const block = screen.getByRole("group", { name: PRODUCED.text });
    const lines = block.querySelectorAll("p");
    expect(lines[0]).toHaveTextContent("是这条任务的交付物吗？");
    expect(lines[1]).toHaveTextContent("能耗看板方案.key · 会后 3 天新增在『能耗看板/』");
    await userEvent.click(within(block).getByRole("button", { name: "能耗看板方案.key" }));
    expect(onOpenFile).toHaveBeenCalledWith(930);
  });

  it("需求卡：只画一行（不列片段），那一句点了打开文件；共用的回答状态只接自己这条决议的行", async () => {
    const onOpenFile = vi.fn();
    const store = createRecentAnswerStore();
    store.put({
      relationId: 99, kind: "affects", text: "已标为更新过", until: later(600_000), fileId: 1, taskId: null,
      decisionId: "dec-other",
    });
    render(
      <Providers store={store}>
        <RelationQuestion
          apiClient={makeClient()}
          canWrite
          compact
          onNotice={vi.fn()}
          onOpenFile={onOpenFile}
          questions={[{ ...AFFECTS, text: "报价单 v3 之后没改过，可能过时" }]}
          scope={{ decisionIds: ["dec-3f2a9c0b1d4e5f60"] }}
        />
      </Providers>,
    );
    const block = screen.getByRole("group", { name: "报价单 v3 之后没改过，可能过时" });
    expect(within(block).queryByText(/第 2 页/)).toBeNull();
    await userEvent.click(within(block).getByRole("button", { name: "报价单 v3 之后没改过，可能过时" }));
    expect(onOpenFile).toHaveBeenCalledWith(812);
    // 别的决议收成的那一行不画在这里
    expect(screen.queryByText("已标为更新过")).toBeNull();
  });
});
