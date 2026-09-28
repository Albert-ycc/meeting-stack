import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { ApiClient, RelationQuestion } from "../api";
import type { MaterialFilePreview, MaterialPreviewContent } from "../types";
import type { MiniPlayerHandle } from "./graph/MiniPlayer";
import { MaterialPreviewDrawer, PICTURE_FALLBACK, PreviewBlock, UNPLAYABLE_TEXT } from "./MaterialPreview";
import { claimSound } from "./soundFocus";
import { LinksFlagsContext } from "./links/LinksFlagsContext";

const EMPTY_PREVIEW: MaterialPreviewContent = {
  kind: "none",
  lines: [],
  more: false,
  rows: [],
  sheet: null,
  image_url: null,
  page_url: null,
  media_url: null,
  playable: false,
  duration_ms: null,
  transcript: [],
};

function previewData(
  preview: Partial<MaterialPreviewContent> = {},
  overrides: Partial<MaterialFilePreview> = {},
): MaterialFilePreview {
  return {
    file: {
      id: 7,
      name: "报价单.xlsx",
      ext: "xlsx",
      rel_path: "报价/报价单.xlsx",
      root_id: 1,
      folder_path: "/Volumes/资料盘/云图/报价",
      path: "/Volumes/资料盘/云图/报价/报价单.xlsx",
      size: 20_480,
      modified_at: "2026-09-20T08:00:00Z",
      project_id: "project-1",
      project_name: "云图",
      root_online: true,
      gone: false,
    },
    state: { kind: "done", reason: null, note: null, what: null, paused: null, meeting: null, text: "" },
    preview: { ...EMPTY_PREVIEW, ...preview },
    mentions: [],
    deliverables: [],
    ...overrides,
  };
}

function fakePlayer() {
  const play = vi.fn<MiniPlayerHandle["play"]>();
  return { play } satisfies MiniPlayerHandle;
}

describe("PreviewBlock 六种预览", () => {
  it("文字：前 40 行，超出写一句", () => {
    render(<PreviewBlock data={previewData({ kind: "text", lines: ["第一行", "第二行"], more: true })} player={fakePlayer()} />);
    expect(screen.getByText(/第一行\s+第二行/)).toHaveClass("material-preview__lines");
    expect(screen.getByText("……只显示前 40 行")).toBeInTheDocument();
  });

  it("表格：表名、前 5 行，超出写一句", () => {
    render(
      <PreviewBlock
        data={previewData({ kind: "table", sheet: "报价", rows: [["品名", "单价"], ["探头", "1200"]], more: true })}
        player={fakePlayer()}
      />,
    );
    expect(screen.getByText("报价")).toBeInTheDocument();
    expect(screen.getByRole("table")).toBeInTheDocument();
    expect(screen.getByText("1200")).toBeInTheDocument();
    expect(screen.getByText("……只显示前 5 行")).toBeInTheDocument();
  });

  it("图片：缩略图加认出的字；读不出来换成一行灰字，不显示破图", () => {
    render(
      <PreviewBlock
        data={previewData({ kind: "image", image_url: "/api/materials/files/7/thumb", lines: ["白板上的字"] })}
        player={fakePlayer()}
      />,
    );
    const img = screen.getByRole("img");
    expect(img).toHaveAttribute("src", "/api/materials/files/7/thumb");
    expect(screen.getByText("白板上的字")).toBeInTheDocument();
    fireEvent.error(img);
    expect(screen.queryByRole("img")).toBeNull();
    expect(screen.getByText(PICTURE_FALLBACK)).toBeInTheDocument();
  });

  it("PDF：第一页的图，下面是第一页的字；盘不在时没有图也写那句灰字", () => {
    const { rerender } = render(
      <PreviewBlock
        data={previewData({ kind: "pdf", page_url: "/api/materials/files/7/page1", lines: ["第一页的字"] })}
        player={fakePlayer()}
      />,
    );
    expect(screen.getByRole("img", { name: "报价单.xlsx 第一页" })).toBeInTheDocument();
    expect(screen.getByText("第一页的字")).toBeInTheDocument();

    const offline = previewData({ kind: "pdf", lines: ["第一页的字"] });
    offline.file.root_online = false;
    rerender(<PreviewBlock data={offline} player={fakePlayer()} />);
    expect(screen.queryByRole("img")).toBeNull();
    expect(screen.getByText(PICTURE_FALLBACK)).toBeInTheDocument();
  });

  it("音视频：从头放，每句前面 ▶ 时间，点了从那里一直放（不截 18 秒）", async () => {
    const user = userEvent.setup();
    const player = fakePlayer();
    render(
      <PreviewBlock
        data={previewData({
          kind: "media",
          media_url: "/api/materials/files/7/media",
          playable: true,
          duration_ms: 125_000,
          transcript: [
            { start_ms: 0, end_ms: 4_000, text: "大家好" },
            { start_ms: 92_000, end_ms: 96_000, text: "报价下周给" },
          ],
        })}
        player={player}
      />,
    );
    await user.click(screen.getByRole("button", { name: "▶ 从头放（02:05）" }));
    expect(player.play).toHaveBeenLastCalledWith("/api/materials/files/7/media", 0, "报价单.xlsx", { clip: false });
    await user.click(screen.getByRole("button", { name: "从 01:32 放" }));
    expect(player.play).toHaveBeenLastCalledWith("/api/materials/files/7/media", 92_000, "报价单.xlsx", { clip: false });
    expect(screen.getByText("报价下周给")).toBeInTheDocument();
  });

  it("浏览器放不了的格式写那一句，不给播放按钮", () => {
    render(
      <PreviewBlock
        data={previewData({ kind: "media", media_url: null, playable: false, transcript: [{ start_ms: 0, end_ms: 1, text: "一句" }] })}
        player={fakePlayer()}
      />,
    );
    expect(screen.getByText(UNPLAYABLE_TEXT)).toBeInTheDocument();
    expect(screen.queryByRole("button")).toBeNull();
    expect(screen.getByText("00:00")).toBeInTheDocument();
  });

  it("没有预览时只写状态那一句；是会议录音时带［打开会议］", async () => {
    const user = userEvent.setup();
    const onOpenMeeting = vi.fn();
    render(
      <PreviewBlock
        data={previewData(
          {},
          {
            state: {
              kind: "done",
              reason: null,
              note: "meeting_audio",
              what: null,
              paused: null,
              meeting: { id: "m-1", title: "云图周会" },
              text: "这是会议『云图周会』的录音，内容看会议",
            },
          },
        )}
        onOpenMeeting={onOpenMeeting}
        player={fakePlayer()}
      />,
    );
    const sentence = screen.getByText(/这是会议『云图周会』的录音/);
    expect(sentence).not.toHaveAttribute("role", "alert");
    await user.click(screen.getByRole("button", { name: "打开会议" }));
    expect(onOpenMeeting).toHaveBeenCalledWith("m-1");
  });
});

describe("同一时刻只放一个声音", () => {
  it("新的开始放时，之前在放的那个暂停", () => {
    const first = document.createElement("audio");
    const second = document.createElement("audio");
    const pauseFirst = vi.fn();
    Object.defineProperty(first, "paused", { configurable: true, get: () => false });
    first.pause = pauseFirst;
    claimSound(first);
    claimSound(second);
    expect(pauseFirst).toHaveBeenCalledTimes(1);
    // 同一个元素再认领不暂停自己
    claimSound(second);
    expect(pauseFirst).toHaveBeenCalledTimes(1);
  });
});

describe("MaterialPreviewDrawer", () => {
  let playResult: () => Promise<void>;

  beforeEach(() => {
    playResult = () => Promise.resolve();
    vi.spyOn(HTMLMediaElement.prototype, "play").mockImplementation(function (this: HTMLMediaElement) {
      const result = playResult();
      void result.then(
        () => this.dispatchEvent(new Event("play")),
        () => undefined,
      );
      return result;
    });
    vi.spyOn(HTMLMediaElement.prototype, "pause").mockImplementation(function (this: HTMLMediaElement) {
      this.dispatchEvent(new Event("pause"));
    });
    vi.spyOn(HTMLMediaElement.prototype, "load").mockImplementation(() => undefined);
  });

  afterEach(() => {
    // 先卸载（抽屉关时会 pause），再还原 jsdom 没实现的播放方法
    cleanup();
    vi.restoreAllMocks();
  });

  function apiClient(data: MaterialFilePreview | Promise<MaterialFilePreview>) {
    return {
      getMaterialPreview: vi.fn().mockReturnValue(Promise.resolve(data)),
      revealMaterial: vi.fn().mockResolvedValue({ ok: true }),
    } as unknown as ApiClient & {
      getMaterialPreview: ReturnType<typeof vi.fn>;
      revealMaterial: ReturnType<typeof vi.fn>;
    };
  }

  const drawerData = previewData(
    { kind: "text", lines: ["一行字"] },
    {
      mentions: [
        {
          meeting_id: "m-1",
          title: "云图周会",
          date: "2026-09-21",
          count: 2,
          first_ms: 61_000,
          quote: "报价单下周给客户",
          audio_url: "/api/media/a-1",
        },
      ],
      deliverables: [{ deliverable_id: 3, task_id: "t-9", title: "整理报价单", status: "open" }],
    },
  );

  function renderDrawer(overrides: Partial<Parameters<typeof MaterialPreviewDrawer>[0]> = {}) {
    const props = {
      apiClient: apiClient(drawerData),
      fileId: 7,
      canReveal: true,
      isMobile: false,
      onClose: vi.fn(),
      onOpenMeeting: vi.fn(),
      onOpenTask: vi.fn(),
      onOpenInGraph: vi.fn(),
      ...overrides,
    };
    render(<MaterialPreviewDrawer {...props} />);
    return props;
  }

  it("文件名、扩展名小标、所在文件夹，会上被提到、交付物，底部三个按钮", async () => {
    const user = userEvent.setup();
    const props = renderDrawer();
    const dialog = await screen.findByRole("dialog", { name: "材料预览" });
    expect(await within(dialog).findByRole("heading", { name: "报价单.xlsx" })).toBeInTheDocument();
    expect(within(dialog).getByText("XLSX")).toBeInTheDocument();
    expect(within(dialog).getByText("/Volumes/资料盘/云图/报价")).toBeInTheDocument();
    expect(within(dialog).getByText("一行字")).toBeInTheDocument();
    expect(within(dialog).getByText("在 1 场会上被提到")).toBeInTheDocument();

    await user.click(within(dialog).getByRole("button", { name: "云图周会" }));
    expect(props.onOpenMeeting).toHaveBeenCalledWith("m-1", 61_000);
    await user.click(within(dialog).getByRole("button", { name: "整理报价单" }));
    expect(props.onOpenTask).toHaveBeenCalledWith("t-9");
    await user.click(within(dialog).getByRole("button", { name: "在访达中显示" }));
    expect(props.apiClient.revealMaterial).toHaveBeenCalledWith("/Volumes/资料盘/云图/报价/报价单.xlsx");
    await user.click(within(dialog).getByRole("button", { name: "在关系图里看" }));
    expect(props.onOpenInGraph).toHaveBeenCalledWith("project-1", 7);
    expect(within(dialog).getByRole("button", { name: "复制路径" })).toBeInTheDocument();
  });

  it("被提到的场数取 mentioned_meetings（列表最多 40 条），旧后台没有时用列表长度", async () => {
    renderDrawer({ apiClient: apiClient({ ...drawerData, mentioned_meetings: 41 }) });
    const dialog = await screen.findByRole("dialog", { name: "材料预览" });
    expect(await within(dialog).findByText("在 41 场会上被提到")).toBeInTheDocument();
  });

  it("放宽的提到（4b）写「说的是『…』」代替「N 次」，没有按钮；旧后台照「N 次」", async () => {
    const loose = {
      meeting_id: "m-2",
      title: "周会",
      date: "2026-09-22",
      count: 1,
      first_ms: 754_000,
      quote: "上周那版报价单再看一下",
      audio_url: null,
      relation_id: 11,
      phrase: "上周那版报价单",
      via: "time_hint" as const,
    };
    renderDrawer({
      apiClient: apiClient({ ...drawerData, mentions: [...(drawerData.mentions ?? []), loose], mentioned_meetings: 2 }),
    });
    const dialog = await screen.findByRole("dialog", { name: "材料预览" });
    expect(await within(dialog).findByText("2026-09-22 · 说的是『上周那版报价单』")).toBeInTheDocument();
    expect(within(dialog).getByText("2026-09-21 · 2 次")).toBeInTheDocument();
    expect(within(dialog).queryByRole("button", { name: "不是这份文件" })).toBeNull();
  });

  it("不在本机时没有［在访达中显示］，手机上没有［在关系图里看］", async () => {
    renderDrawer({ canReveal: false, isMobile: true });
    expect(await screen.findByRole("heading", { name: "报价单.xlsx" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "在访达中显示" })).toBeNull();
    expect(screen.queryByRole("button", { name: "在关系图里看" })).toBeNull();
    expect(screen.getByRole("button", { name: "复制路径" })).toBeInTheDocument();
  });

  it("Esc 和点遮罩都关抽屉", async () => {
    const props = renderDrawer();
    await screen.findByRole("heading", { name: "报价单.xlsx" });
    fireEvent.keyDown(window, { key: "Escape" });
    expect(props.onClose).toHaveBeenCalledTimes(1);
    fireEvent.click(document.querySelector(".material-drawer__scrim")!);
    expect(props.onClose).toHaveBeenCalledTimes(2);
  });

  it("从搜索的 ▶ 进来：不等预览数据，直接从那个时间放", async () => {
    const never = new Promise<MaterialFilePreview>(() => undefined);
    renderDrawer({ apiClient: apiClient(never), startMs: 92_000 });
    const audio = document.querySelector("audio")!;
    expect(audio.getAttribute("src")).toBe("/api/materials/files/7/media");
    await act(async () => {
      audio.dispatchEvent(new Event("loadedmetadata"));
    });
    expect(HTMLMediaElement.prototype.play).toHaveBeenCalled();
    expect(audio.currentTime).toBe(92);
    // 预览数据还没回来
    expect(screen.queryByRole("heading")).toBeNull();
  });

  it("手机上浏览器拦了自动播放：显示「▶ 从 01:32 放」，点了再放", async () => {
    playResult = () => Promise.reject(new Error("NotAllowedError"));
    renderDrawer({ startMs: 92_000 });
    const audio = document.querySelector("audio")!;
    await act(async () => {
      audio.dispatchEvent(new Event("loadedmetadata"));
    });
    const retry = await screen.findByRole("button", { name: "▶ 从 01:32 放" });
    playResult = () => Promise.resolve();
    fireEvent.click(retry);
    await waitFor(() => expect(screen.queryByRole("button", { name: "▶ 从 01:32 放" })).toBeNull());
  });

  it("点会上原话的 ▶ 只放那一小段（前 3 秒到后 15 秒）", async () => {
    const user = userEvent.setup();
    renderDrawer();
    await user.click(await screen.findByRole("button", { name: "从 01:01 播放原话" }));
    const audio = document.querySelector("audio")!;
    expect(audio.getAttribute("src")).toBe("/api/media/a-1");
    await act(async () => {
      audio.dispatchEvent(new Event("loadedmetadata"));
    });
    expect(audio.currentTime).toBe(58);
    expect(await screen.findByText(/云图周会 · /)).toBeInTheDocument();
  });

  it("预览读不出来时给［重试］", async () => {
    const user = userEvent.setup();
    const client = apiClient(drawerData);
    client.getMaterialPreview.mockReturnValueOnce(Promise.reject(new Error("boom")));
    renderDrawer({ apiClient: client });
    await user.click(await screen.findByRole("button", { name: "重试" }));
    expect(await screen.findByRole("heading", { name: "报价单.xlsx" })).toBeInTheDocument();
    expect(client.getMaterialPreview).toHaveBeenCalledTimes(2);
  });
});

describe("MaterialPreviewDrawer 的定位和「内容相关的会」（4d）", () => {
  beforeEach(() => {
    vi.spyOn(HTMLMediaElement.prototype, "play").mockImplementation(() => Promise.resolve());
    vi.spyOn(HTMLMediaElement.prototype, "pause").mockImplementation(() => undefined);
    vi.spyOn(HTMLMediaElement.prototype, "load").mockImplementation(() => undefined);
  });

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  const related = [
    {
      meeting_id: "m-3",
      title: "周会",
      date: "2026-09-21",
      at_ms: 754_000,
      quote: "驻场那部分报价单里要单列",
      words: ["报价单", "驻场"],
      audio_url: "/api/media/a-3",
      relation_id: 41,
    },
  ];

  function client(data: MaterialFilePreview) {
    return {
      getMaterialPreview: vi.fn().mockResolvedValue(data),
      revealMaterial: vi.fn(),
      answerRelation: vi.fn().mockResolvedValue({ relation: { id: 41 }, undo_until: "2099-01-01T00:00:00Z" }),
      undoRelation: vi.fn().mockResolvedValue({ relation: { id: 41 }, removed_deliverable_id: null }),
      openMaterialFile: vi.fn().mockResolvedValue({ ok: true }),
    } as unknown as ApiClient & Record<string, ReturnType<typeof vi.fn>>;
  }

  function open(data: MaterialFilePreview, overrides: Partial<Parameters<typeof MaterialPreviewDrawer>[0]> = {}) {
    const props = {
      apiClient: client(data),
      fileId: 7,
      canReveal: true,
      canWrite: true,
      isMobile: false,
      onClose: vi.fn(),
      onOpenMeeting: vi.fn(),
      ...overrides,
    };
    const view = render(
      <LinksFlagsContext.Provider value={{ linksEnabled: true, semanticEnabled: true, llmConfigured: true }}>
        <MaterialPreviewDrawer {...props} />
      </LinksFlagsContext.Provider>,
    );
    return { props, view };
  }

  const passageTarget = { contentKey: "q2:3f6c0000000000000000", ordinal: 14, from: "related" as const, words: ["驻场"] };

  it("定位块三种情况：新的、改过的、找不到的（旧后台没有 passage 字段也按找不到）", async () => {
    const fresh = previewData({ kind: "text", lines: ["一行字"] }, { passage: { loc: "第 3 节", start_ms: null, text: "驻场那部分单列", stale: false } });
    const { props, view } = open(fresh, { passage: passageTarget });
    expect(await screen.findByText("和会上相关的这段")).toBeInTheDocument();
    expect(screen.getByText("驻场").tagName).toBe("MARK");
    expect(props.apiClient.getMaterialPreview).toHaveBeenCalledWith(7, undefined, { contentKey: passageTarget.contentKey, ordinal: 14 });
    view.unmount();

    const stale = previewData({}, { passage: { loc: null, start_ms: null, text: "旧的那段", stale: true } });
    const second = open(stale, { passage: { ...passageTarget, from: "search" } });
    expect(await screen.findByText("搜到的这段")).toBeInTheDocument();
    expect(screen.getByText("文件后来改过，这是改之前读到的那段")).toBeInTheDocument();
    second.view.unmount();

    open(previewData(), { passage: { ...passageTarget, from: "answer" } });
    expect(await screen.findByText("回答引用的这段")).toBeInTheDocument();
    expect(screen.getByText("这段在文件里找不到了（文件可能改过）")).toBeInTheDocument();
  });

  it("没有定位时不请求段落；［用本机应用打开］只在 can_open 时出现", async () => {
    const data = previewData({}, {});
    const { props, view } = open({ ...data, file: { ...data.file, can_open: true } });
    await userEvent.click(await screen.findByRole("button", { name: "用本机应用打开" }));
    expect(props.apiClient.openMaterialFile).toHaveBeenCalledWith(7);
    expect(props.apiClient.getMaterialPreview).toHaveBeenCalledWith(7);
    view.unmount();
    open(data);
    await screen.findByRole("heading", { name: "报价单.xlsx" });
    expect(screen.queryByRole("button", { name: "用本机应用打开" })).not.toBeInTheDocument();
  });

  it("「内容相关的会」：［不相关］以后收成带［撤销］的一行，点了调 undoRelation", async () => {
    const { props } = open(previewData({}, { related_meetings: related }));
    expect(await screen.findByText("内容相关的会")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "周会 · 9月21日" })).toBeInTheDocument();
    expect(screen.getByText("共同词：报价单、驻场")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "不相关" }));
    expect(props.apiClient.answerRelation).toHaveBeenCalledWith(41, { answer: "no" });
    const line = await screen.findByText("已记下：『报价单.xlsx』和这场会不相关", { selector: ".relation-recent span" });
    await userEvent.click(within(line.closest("p")!).getByRole("button", { name: "撤销" }));
    expect(props.apiClient.undoRelation).toHaveBeenCalledWith(41);
  });

  it("canWrite 为假时没有［不相关］", async () => {
    open(previewData({}, { related_meetings: related }), { canWrite: false });
    expect(await screen.findByText("内容相关的会")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "不相关" })).not.toBeInTheDocument();
  });
});

describe("MaterialPreviewDrawer 的问题块（4e）", () => {
  afterEach(() => {
    cleanup();
  });

  const AFFECTS: RelationQuestion = {
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
    file: { id: 7, name: "报价单.xlsx" },
    answers: ["updated", "no"],
  };

  function open(data: MaterialFilePreview, canWrite = true) {
    const apiClient = {
      getMaterialPreview: vi.fn().mockResolvedValue(data),
      revealMaterial: vi.fn(),
      answerRelation: vi.fn().mockResolvedValue({ relation: {}, undo_until: new Date(Date.now() + 600_000).toISOString() }),
      undoRelation: vi.fn().mockResolvedValue({ relation: {}, removed_deliverable_id: null }),
    } as unknown as ApiClient & Record<string, ReturnType<typeof vi.fn>>;
    render(
      <LinksFlagsContext.Provider value={{ linksEnabled: true, semanticEnabled: true, llmConfigured: true }}>
        <MaterialPreviewDrawer
          apiClient={apiClient}
          canReveal
          canWrite={canWrite}
          fileId={7}
          isMobile={false}
          onClose={vi.fn()}
          onOpenMeeting={vi.fn()}
        />
      </LinksFlagsContext.Provider>,
    );
    return apiClient;
  }

  it("问题在位置那一行之后、预览之前；［已更新］以后提示带［撤销］，点了调 undoRelation", async () => {
    const apiClient = open(previewData({ kind: "text", lines: ["表格第一行"] }, { questions: [AFFECTS] }));
    const block = await screen.findByRole("group", { name: AFFECTS.text });
    const where = screen.getByText(/修改于/).closest("p")!;
    const previewLine = screen.getByText("表格第一行");
    expect(where.compareDocumentPosition(block) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(block.compareDocumentPosition(previewLine) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(within(block).getByText("第 2 页：『…总价在原基础上下调 3%，含税…』")).toBeInTheDocument();

    await userEvent.click(within(block).getByRole("button", { name: "已更新" }));
    expect(apiClient.answerRelation).toHaveBeenCalledWith(57, { answer: "updated" });
    const notice = await screen.findByRole("status");
    expect(notice).toHaveTextContent("已标为更新过");
    await userEvent.click(within(notice).getByRole("button", { name: "撤销" }));
    expect(apiClient.undoRelation).toHaveBeenCalledWith(57);
  });

  it("canWrite 为假（手机）只显示问题，不给按钮", async () => {
    open(previewData({}, { questions: [AFFECTS] }), false);
    const block = await screen.findByRole("group", { name: AFFECTS.text });
    expect(within(block).queryByRole("button", { name: "已更新" })).toBeNull();
    expect(within(block).queryByRole("button", { name: "不相关" })).toBeNull();
  });
});
