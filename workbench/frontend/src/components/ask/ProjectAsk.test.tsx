import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi, type Mock } from "vitest";

import { ApiError } from "../../api";
import type { AskJob, AskPlan, AskSource } from "../../types";
import { forgetAskStore, readTurns, setDraft } from "./askStore";
import { ProjectAsk, type ProjectAskProps } from "./ProjectAsk";

const YEAR = new Date().getFullYear();

const D1: AskSource = {
  id: "D1",
  kind: "decision",
  decision_id: "dec-1",
  meeting_id: "m-1",
  title: "初审规则沟通",
  date: `${YEAR}-09-21`,
  start_ms: 754_000,
  audio_url: "/api/media/12",
  text: "总价下调五个点",
  quote: "总价下调五个点",
  later_changed: { date: `${YEAR}-09-28`, decision_id: "dec-2" },
};
const N1: AskSource = {
  id: "N1",
  kind: "minutes",
  meeting_id: "m-2",
  title: "周会",
  date: `${YEAR}-09-14`,
  start_ms: null,
  audio_url: "/api/media/9",
  text: "驻场报价单列",
  quote: "驻场报价单列",
};
const T1: AskSource = {
  id: "T1",
  kind: "meeting",
  meeting_id: "m-1",
  title: "初审规则沟通",
  date: `${YEAR}-09-21`,
  start_ms: 754_000,
  end_ms: 790_000,
  audio_url: "/api/media/12",
  speaker: "张三",
  text: "驻场那部分报价单里要单列",
  quote: "驻场那部分报价单里要单列",
};
const M1: AskSource = {
  id: "M1",
  kind: "material",
  file_id: 812,
  name: "报价单 v3.xlsx",
  content_key: "q2:abc",
  ordinal: 14,
  loc: "表『预算』",
  start_ms: null,
  playable: false,
  root_online: true,
  text: "预算表里的总价",
  quote: "预算表里的总价",
};

function plan(overrides: Partial<AskPlan> = {}): AskPlan {
  return {
    plan_id: "plan-1",
    expires_in: 600,
    question: "报价最后定了多少？",
    counts: { meetings: 3, materials: 1 },
    confirm: { text: "将发送 1 段材料原文给 api.deepseek.com", host: "api.deepseek.com" },
    local_model: false,
    llm: "ok",
    highlight: ["报价单"],
    sources: [D1, N1, T1, M1],
    notes: [],
    unattributed_meetings: 0,
    ...overrides,
  };
}

function done(text: string, overrides: Partial<Extract<AskJob, { state: "done" }>> = {}): AskJob {
  return {
    state: "done",
    answer: { text, cited: ["D1", "M1"], found: true, no_evidence: false, truncated: false },
    sent: { meetings: 3, materials: 1 },
    sources: [D1, N1, T1, M1].map((source) => ({ ...source, sent: true })),
    notes: [],
    local_model: false,
    ...overrides,
  };
}

function client(
  prepared: AskPlan = plan(),
  job: AskJob = done("报价最后定为下调五个点[D1]，报价单里也是这个数[M1]。"),
): { askPrepare: Mock; ask: Mock; askJob: Mock } {
  return {
    askPrepare: vi.fn().mockResolvedValue(prepared),
    ask: vi.fn().mockResolvedValue({ job_id: "job-1", state: "waiting", text: "在等 AI 回答" }),
    askJob: vi.fn().mockResolvedValue(job),
  };
}

function renderAsk(apiClient: ProjectAskProps["apiClient"], props: Partial<ProjectAskProps> = {}) {
  const handlers = {
    onOpenMeeting: vi.fn(),
    onOpenPreview: vi.fn(),
    player: { play: vi.fn() },
  };
  const view = render(
    <ProjectAsk
      apiClient={apiClient}
      projectId="p1"
      projectName="云图AI"
      variant="card"
      {...handlers}
      {...props}
    />,
  );
  return { ...handlers, view };
}

async function askQuestion(question = "报价最后定了多少？") {
  const input = screen.getByRole("textbox", { name: "问题" });
  fireEvent.change(input, { target: { value: question } });
  fireEvent.submit(input.closest("form")!);
}

beforeEach(() => {
  forgetAskStore();
});

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("ProjectAsk", () => {
  it("没有 askPrepare 时什么都不画", () => {
    const { view } = renderAsk({} as never);
    expect(view.container).toBeEmptyDOMElement();
  });

  it("卡片三行：标题、输入框、还没问过时的说明；最多 300 字", () => {
    renderAsk(client());
    expect(screen.getByRole("heading", { name: "问这个项目" })).toBeInTheDocument();
    const input = screen.getByRole("textbox", { name: "问题" });
    expect(input).toHaveAttribute("placeholder", "比如：报价最后定的是多少？");
    expect(input).toHaveAttribute("maxLength", "300");
    expect(screen.getByText("只在『云图AI』的会和材料里找；要把材料原文发出去时会先告诉你")).toBeInTheDocument();
  });

  it("只有会议段落时 prepare 之后直接发，没有「将发送」", async () => {
    const apiClient = client(plan({ counts: { meetings: 3, materials: 0 }, confirm: null, sources: [D1, N1, T1] }));
    renderAsk(apiClient);
    await askQuestion();
    await waitFor(() => expect(apiClient.ask).toHaveBeenCalledWith("p1", "plan-1", false));
    expect(apiClient.askPrepare).toHaveBeenCalledWith("p1", "报价最后定了多少？");
    expect(screen.queryByText(/将发送/)).toBeNull();
    expect(screen.getByText("在等 AI 回答")).toBeInTheDocument();
  });

  it("有材料时［发送］正上方写那一行，点［发送］（或回车）之前不发", async () => {
    const apiClient = client();
    renderAsk(apiClient);
    await askQuestion();
    expect(await screen.findByText("找到会议里的 3 段、材料里的 1 段")).toBeInTheDocument();
    const line = screen.getByText("将发送 1 段材料原文给 api.deepseek.com");
    const send = screen.getByRole("button", { name: "发送" });
    expect(line.compareDocumentPosition(send) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(send).toHaveFocus();
    expect(apiClient.ask).not.toHaveBeenCalled();
    // 「看看是哪几段」展开会发出去的原文
    await userEvent.click(screen.getByRole("button", { name: /看看是哪几段/ }));
    expect(screen.getByText("预算表里的总价")).toBeInTheDocument();
    send.focus();
    await userEvent.keyboard("{Enter}");
    await waitFor(() => expect(apiClient.ask).toHaveBeenCalledWith("p1", "plan-1", true));
  });

  it("［只用会议回答］发 withMaterials false；会议段落为 0 时没有这个按钮", async () => {
    const apiClient = client();
    renderAsk(apiClient);
    await askQuestion();
    await userEvent.click(await screen.findByRole("button", { name: "只用会议回答" }));
    expect(apiClient.ask).toHaveBeenCalledWith("p1", "plan-1", false);
  });

  it("会议段落为 0 时没有［只用会议回答］", async () => {
    renderAsk(client(plan({ counts: { meetings: 0, materials: 1 }, sources: [M1] })));
    await askQuestion();
    expect(await screen.findByRole("button", { name: "发送" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "只用会议回答" })).toBeNull();
    expect(screen.getByText("找到材料里的 1 段")).toBeInTheDocument();
  });

  it("本机模型同样出那一行、点了［发送］才发；回答下面写「用本机模型回答」", async () => {
    const local = plan({
      confirm: { text: "将发送 1 段材料原文给 127.0.0.1", host: "127.0.0.1" },
      local_model: true,
    });
    const apiClient = client(local, done("定了[D1]。", { local_model: true }));
    renderAsk(apiClient);
    await askQuestion();
    expect(await screen.findByText("将发送 1 段材料原文给 127.0.0.1")).toBeInTheDocument();
    expect(apiClient.ask).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole("button", { name: "发送" }));
    expect(apiClient.ask).toHaveBeenCalledWith("p1", "plan-1", true);
    expect(await screen.findByText("用本机模型回答", undefined, { timeout: 3_000 })).toBeInTheDocument();
  });

  it("每 1.5 秒轮询，waiting 到 done；页面隐藏时停，回来立刻问一次", async () => {
    const apiClient = client(plan({ confirm: null, counts: { meetings: 3, materials: 0 }, sources: [D1, N1, T1] }));
    apiClient.askJob
      .mockResolvedValueOnce({ state: "waiting", text: "在等 AI 回答" })
      .mockResolvedValue(done("定了[D1]。"));
    vi.useFakeTimers();
    renderAsk(apiClient);
    await askQuestion();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(apiClient.ask).toHaveBeenCalled();
    const hidden = vi.spyOn(document, "hidden", "get");
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1_500);
    });
    expect(apiClient.askJob).toHaveBeenCalledTimes(1);
    hidden.mockReturnValue(true);
    document.dispatchEvent(new Event("visibilitychange"));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10_000);
    });
    expect(apiClient.askJob).toHaveBeenCalledTimes(1);
    hidden.mockReturnValue(false);
    await act(async () => {
      document.dispatchEvent(new Event("visibilitychange"));
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(apiClient.askJob).toHaveBeenCalledTimes(2);
    expect(screen.getByText("问：报价最后定了多少？")).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: "出处：9/21 初审规则沟通 12:34" }).length).toBeGreaterThan(0);
  });
});

describe("ProjectAsk 回答和出处", () => {
  async function answered(text: string, job?: Partial<Extract<AskJob, { state: "done" }>>, props = {}) {
    const apiClient = client(plan(), done(text, job));
    const result = renderAsk(apiClient, props);
    await askQuestion();
    await userEvent.click(await screen.findByRole("button", { name: "发送" }));
    await screen.findByText("问：报价最后定了多少？", undefined, { timeout: 3_000 });
    return { apiClient, ...result };
  }

  it("回答按纯文本画：网址和 **粗** 原样显示，没有链接；aria-live=polite", async () => {
    const { view } = await answered("看 https://evil.example/?q=x 和 **粗**[D1]");
    expect(screen.getByText(/https:\/\/evil\.example\/\?q=x 和 \*\*粗\*\*/)).toBeInTheDocument();
    expect(view.container.querySelector("a")).toBeNull();
    expect(view.container.querySelector("[aria-live='polite']")).not.toBeNull();
    expect(view.container.querySelector("[role='alert']")).toBeNull();
  });

  it("标记变成出处小块，认不出的不变；段数一行、引用和复制", async () => {
    await answered("定了[D1]，也见[M1]和[X9][M7]。");
    const region = screen.getByText("问：报价最后定了多少？").parentElement!;
    const answer = region.querySelector(".ask-answer__text")!;
    expect(within(answer as HTMLElement).getByRole("button", { name: "出处：9/21 初审规则沟通 12:34" })).toBeInTheDocument();
    expect(within(answer as HTMLElement).getByRole("button", { name: "出处：报价单 v3.xlsx 表『预算』" })).toHaveTextContent(
      "报价单 v3.xlsx · 表『预算』",
    );
    expect(answer).toHaveTextContent("[X9]");
    expect(answer).toHaveTextContent("[M7]");
    expect(screen.getByText("只看了最相关的 1 段材料、3 段会议里的原话")).toBeInTheDocument();
    expect(screen.getByText("后来改了 9/28", { selector: ".ask-answer__text *" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "引用" })).toBeInTheDocument();
    const copy = vi.fn().mockResolvedValue(undefined);
    Object.assign(navigator, { clipboard: { writeText: copy } });
    await userEvent.click(screen.getByRole("button", { name: "复制回答" }));
    expect(await screen.findByText("已复制")).toBeInTheDocument();
    expect(copy.mock.calls[0][0]).toContain("[D1] 9/21 初审规则沟通 12:34：总价下调五个点");
  });

  it("会议小块打开会议到那一秒，纪要小块打开纪要，▶ 放，材料小块打开预览到「回答引用的这段」", async () => {
    const { onOpenMeeting, onOpenPreview, player } = await answered("原话[T1]，纪要[N1]，材料[M1]。", {
      answer: { text: "原话[T1]，纪要[N1]，材料[M1]。", cited: ["T1", "N1", "M1"], found: true, no_evidence: false, truncated: false },
    });
    const answer = document.querySelector(".ask-answer__text") as HTMLElement;
    await userEvent.click(within(answer).getByRole("button", { name: "出处：9/21 初审规则沟通 12:34" }));
    expect(onOpenMeeting).toHaveBeenCalledWith("m-1", 754_000, undefined);
    await userEvent.click(within(answer).getByRole("button", { name: "出处：9/14 周会 · 纪要" }));
    expect(onOpenMeeting).toHaveBeenLastCalledWith("m-2", undefined, "minutes");
    await userEvent.click(within(answer).getByRole("button", { name: "从 12:34 播放" }));
    expect(player.play).toHaveBeenCalledWith("/api/media/12", 754_000, "初审规则沟通 12:34");
    await userEvent.click(within(answer).getByRole("button", { name: "出处：报价单 v3.xlsx 表『预算』" }));
    expect(onOpenPreview).toHaveBeenCalledWith({
      fileId: 812,
      startMs: undefined,
      passage: { contentKey: "q2:abc", ordinal: 14, from: "answer", words: ["报价单"] },
    });
  });

  it("资料盘没插时材料小块变灰，悬停写「资料盘未连接」", async () => {
    const offline = { ...M1, root_online: false };
    const apiClient = client(plan({ sources: [D1, offline] }), done("见[M1]。", { sources: [D1, offline] }));
    renderAsk(apiClient);
    await askQuestion();
    await userEvent.click(await screen.findByRole("button", { name: "发送" }));
    const chips = await screen.findAllByRole("button", { name: "出处：报价单 v3.xlsx 表『预算』" }, { timeout: 3_000 });
    expect(chips[0]).toBeDisabled();
    expect(chips[0]).toHaveAttribute("title", "资料盘未连接");
  });

  it("no_evidence 和没找到的说法", async () => {
    await answered("", {
      answer: { text: "", cited: [], found: true, no_evidence: true, truncated: false },
    });
    expect(screen.getByText("AI 的回答没指到原文，没列出来；下面是找到的原话")).toBeInTheDocument();
    expect(screen.getByRole("group", { name: "找到的原话" })).toBeInTheDocument();
  });

  it("没找到：一句话和「看看找到的原话」", async () => {
    await answered("没找到", {
      answer: { text: "没找到", cited: [], found: false, no_evidence: false, truncated: false },
    });
    expect(screen.getByText("会议和材料里没找到能回答这个问题的原话")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /看看找到的原话/ })).toBeInTheDocument();
  });

  it("截断时加一句；最多 2 条说明", async () => {
    await answered("定了[D1]", {
      answer: { text: "定了[D1]", cited: ["D1"], found: true, no_evidence: false, truncated: true },
      notes: [
        { kind: "busy", text: "正在转写，这次只按原词找" },
        { kind: "partial", text: "时间到了，只找了一部分" },
        { kind: "materials_pending", text: "有些材料还没读完，可能找不全" },
      ],
    });
    expect(screen.getByText("回答太长，后面截掉了")).toBeInTheDocument();
    expect(screen.getByText("正在转写，这次只按原词找")).toBeInTheDocument();
    expect(screen.getByText("时间到了，只找了一部分")).toBeInTheDocument();
    expect(screen.queryByText("有些材料还没读完，可能找不全")).toBeNull();
  });
});

describe("ProjectAsk AI 不能用、停了和出错", () => {
  it.each([
    ["no_key", "没配置 AI，先列出找到的原话"],
    ["off", "问答的 AI 回答已关闭，先列出找到的原话"],
    ["capped", "今天问答的次数到上限了，先列出找到的原话"],
  ] as const)("llm=%s：一句话加原文列表，没有［发送］", async (llm, text) => {
    renderAsk(client(plan({ llm, confirm: null })));
    await askQuestion();
    expect(await screen.findByText(text)).toBeInTheDocument();
    expect(screen.getByRole("group", { name: "找到的原话" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "发送" })).toBeNull();
  });

  it("什么都没找到", async () => {
    const apiClient = client(plan({ sources: [], counts: { meetings: 0, materials: 0 }, confirm: null }));
    renderAsk(apiClient);
    await askQuestion();
    expect(await screen.findByText("会议和材料里都没找到和这个问题有关的原话")).toBeInTheDocument();
    expect(screen.getByText("换个说法，或者用文件名、词典里的词问")).toBeInTheDocument();
    expect(apiClient.ask).not.toHaveBeenCalled();
  });

  it("超时有［再问一次］（同一个计划重发），auth 没有；每个状态句最多一个按钮", async () => {
    const stopped = (reason: string, retry: boolean, text: string): AskJob => ({
      state: "stopped",
      reason,
      text,
      retry,
      sources: [D1],
    });
    const apiClient = client(plan(), stopped("timeout", true, "AI 没回（等了 90 秒），先列出找到的原话"));
    renderAsk(apiClient);
    await askQuestion();
    await userEvent.click(await screen.findByRole("button", { name: "发送" }));
    const line = await screen.findByText("AI 没回（等了 90 秒），先列出找到的原话", undefined, { timeout: 3_000 });
    expect(within(line).getAllByRole("button")).toHaveLength(1);
    apiClient.askJob.mockResolvedValue(stopped("auth", false, "AI 的 key 不对，先列出找到的原话"));
    await userEvent.click(within(line).getByRole("button", { name: "再问一次" }));
    expect(apiClient.ask).toHaveBeenCalledTimes(2);
    expect(apiClient.ask).toHaveBeenLastCalledWith("p1", "plan-1", true);
    const auth = await screen.findByText("AI 的 key 不对，先列出找到的原话", undefined, { timeout: 3_000 });
    expect(within(auth).queryByRole("button")).toBeNull();
  });

  it("ask 回 404：「这次找到的原话过期了」，［再问一次］重新找一遍", async () => {
    const apiClient = client();
    apiClient.ask.mockRejectedValueOnce(new ApiError("这次找到的原话过期了，请再问一次", 404, {}));
    renderAsk(apiClient);
    await askQuestion();
    await userEvent.click(await screen.findByRole("button", { name: "发送" }));
    const line = await screen.findByText("这次找到的原话过期了");
    await userEvent.click(within(line).getByRole("button", { name: "再问一次" }));
    await waitFor(() => expect(apiClient.askPrepare).toHaveBeenCalledTimes(2));
  });

  it("ask 回 429、503：那一句加找到的原话列表（计划留着），没有按钮", async () => {
    const apiClient = client();
    apiClient.ask
      .mockRejectedValueOnce(new ApiError("今天问答的次数到上限了，明天再问", 429, {}))
      .mockRejectedValueOnce(new ApiError("没配置 AI，先列出找到的原话", 503, {}));
    renderAsk(apiClient);
    await askQuestion();
    await userEvent.click(await screen.findByRole("button", { name: "发送" }));
    const capped = await screen.findByText("今天问答的次数到上限了，明天再问");
    expect(within(capped).queryByRole("button")).toBeNull();
    expect(screen.getByText("驻场那部分报价单里要单列")).toBeInTheDocument();
    expect(screen.getByText("预算表里的总价")).toBeInTheDocument();
    expect(readTurns("p1")[0].plan?.plan_id).toBe("plan-1");

    await askQuestion("再问一个");
    await userEvent.click(await screen.findByRole("button", { name: "发送" }));
    const noKey = await screen.findByText("没配置 AI，先列出找到的原话");
    expect(within(noKey).queryByRole("button")).toBeNull();
    expect(screen.getByText("驻场那部分报价单里要单列")).toBeInTheDocument();
  });

  it("ask 回 409：「上一个问题还在回答」，没有按钮", async () => {
    const apiClient = client();
    apiClient.ask.mockRejectedValueOnce(new ApiError("这个项目上一个问题还在回答", 409, {}));
    renderAsk(apiClient);
    await askQuestion();
    await userEvent.click(await screen.findByRole("button", { name: "发送" }));
    const line = await screen.findByText("上一个问题还在回答");
    expect(within(line).queryByRole("button")).toBeNull();
  });

  it("prepare 回 405：写旧后台那一句，这次打开期间藏起卡片", async () => {
    const apiClient = client();
    apiClient.askPrepare.mockRejectedValue(new ApiError("Method Not Allowed", 405, { detail: "Method Not Allowed" }));
    renderAsk(apiClient);
    await askQuestion();
    expect(await screen.findByText("后台还是旧版本，重启声档后再试")).toBeInTheDocument();
    expect(screen.queryByRole("textbox", { name: "问题" })).toBeNull();
  });
});

describe("ProjectAsk 只在内存里", () => {
  it("不写 sessionStorage、localStorage；卸载再挂上几轮还在，forgetAskStore 以后没了", async () => {
    const setItem = vi.spyOn(Storage.prototype, "setItem");
    const apiClient = client(plan({ confirm: null, counts: { meetings: 3, materials: 0 }, sources: [D1, N1, T1] }));
    const first = renderAsk(apiClient);
    await askQuestion("第一个问题");
    await screen.findByText("问：第一个问题", undefined, { timeout: 3_000 });
    await askQuestion("第二个问题");
    await screen.findByText("问：第二个问题", undefined, { timeout: 3_000 });
    first.view.unmount();
    expect(readTurns("p1")).toHaveLength(2);

    renderAsk(apiClient);
    expect(screen.getByText("问：第二个问题")).toBeInTheDocument();
    expect(screen.getByText("之前问过")).toBeInTheDocument();
    expect(screen.queryByText(/只在『云图AI』/)).toBeNull();
    expect(setItem).not.toHaveBeenCalled();
    forgetAskStore();
    expect(readTurns("p1")).toEqual([]);
  });

  it("搜索页交过来的问题填进输入框，不自动发", () => {
    const apiClient = client();
    setDraft("p1", "报价最后定了多少？");
    renderAsk(apiClient);
    expect(screen.getByRole("textbox", { name: "问题" })).toHaveValue("报价最后定了多少？");
    expect(apiClient.askPrepare).not.toHaveBeenCalled();
  });

  it("手机上照样出那一行；没传 player 时自己放一个播放器", async () => {
    const { view } = renderAsk(client(), { isMobile: true, player: undefined });
    expect(view.container.querySelector(".ask--mobile")).not.toBeNull();
    expect(view.container.querySelector("audio")).not.toBeNull();
    await askQuestion();
    expect(await screen.findByText("将发送 1 段材料原文给 api.deepseek.com")).toBeInTheDocument();
  });
});
