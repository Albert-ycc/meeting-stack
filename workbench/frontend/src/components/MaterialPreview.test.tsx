import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { ApiClient } from "../api";
import type { MaterialFilePreview, MaterialPreviewContent } from "../types";
import type { MiniPlayerHandle } from "./graph/MiniPlayer";
import { MaterialPreviewDrawer, PICTURE_FALLBACK, PreviewBlock, UNPLAYABLE_TEXT } from "./MaterialPreview";
import { claimSound } from "./soundFocus";

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
