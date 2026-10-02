import { act, fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ComponentProps } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { ApiClient } from "../api";
import type { MeetingAttribution, MeetingDetail, Segment } from "../types";
import { LinksFlagsContext } from "./links/LinksFlagsContext";
import { MeetingDetailPage } from "./MeetingDetailPage";

// 点时间 = 跳到那里并开始放；带时间锚打开（检索结果、词典「听到」）也一样。浏览器不让放时安静地停在那个位置。
const segments: Segment[] = [
  { id: "s0", ordinal: 0, start_ms: 0, end_ms: 60_000, speaker_name: "甲", text: "开场" },
  { id: "s1", ordinal: 1, start_ms: 60_000, end_ms: 120_000, speaker_name: "乙", text: "一分钟处的这句" },
  { id: "s2", ordinal: 2, start_ms: 120_000, end_ms: 180_000, speaker_name: "甲", text: "两分钟处的这句" },
];

function detail(extra: Partial<MeetingDetail> = {}): MeetingDetail {
  return {
    id: "vm-play",
    title: "录音测试会",
    status: "completed_unreviewed",
    tags: [],
    artifacts: [{ id: 42, meeting_id: "vm-play", kind: "audio", role: "source", source_root: "archive", path: "/x/a.m4a" }],
    duration_ms: 180_000,
    segments,
    speakers: [],
    events: [],
    current_transcript_version_id: "tv-1",
    transcript_versions: [
      { id: "tv-1", meeting_id: "vm-play", version_no: 1, kind: "funasr", published: 0, created_at: "2026-07-10T00:00:00Z" },
    ],
    minutes_versions: [],
    ...extra,
  };
}

/** play 被调用的那一刻 currentTime 是多少：先跳再放，不能放了再跳 */
function spyPlay(result: () => Promise<void> = () => Promise.resolve()) {
  const startedAt: number[] = [];
  const play = vi.spyOn(HTMLMediaElement.prototype, "play").mockImplementation(function (this: HTMLMediaElement) {
    startedAt.push(this.currentTime);
    return result();
  });
  return { play, startedAt };
}

function renderPage(props: Partial<ComponentProps<typeof MeetingDetailPage>> = {}, apiClient: Partial<ApiClient> = {}) {
  // 有录音时播放器会去取波形；让它一直等着，用例里不真建播放器
  vi.stubGlobal("fetch", vi.fn(() => new Promise(() => {})));
  const view = render(
    <MeetingDetailPage
      apiClient={{ asrGoldSamples: vi.fn().mockResolvedValue({ items: [] }), ...apiClient } as unknown as ApiClient}
      initialSeekMs={0}
      isMobile={false}
      meeting={detail()}
      onBack={vi.fn()}
      onReload={vi.fn().mockResolvedValue(undefined)}
      projects={[]}
      tags={[]}
      {...props}
    />,
  );
  const audio = view.container.querySelector("audio") as HTMLAudioElement;
  Object.defineProperty(audio, "currentTime", { configurable: true, value: 0, writable: true });
  return { ...view, audio };
}

/** 录音读到了元数据（jsdom 不会自己读） */
function loaded(audio: HTMLAudioElement) {
  Object.defineProperty(audio, "readyState", { configurable: true, value: HTMLMediaElement.HAVE_METADATA });
  fireEvent(audio, new Event("loadedmetadata"));
}

/** 浏览器不让放：play() 的 Promise 拒绝。拒绝是调用时才造的（早造了没人接，Node 会当成没接住的拒绝）；记下有没有人接 */
function blockedPlay() {
  const catches: Array<ReturnType<typeof vi.spyOn>> = [];
  const result = () => {
    const rejected = Promise.reject<void>(new DOMException("play() failed because the user didn't interact", "NotAllowedError"));
    catches.push(vi.spyOn(rejected, "catch"));
    return rejected;
  };
  return { result, catches };
}

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("MeetingDetailPage 点时间就放", () => {
  it("点逐字稿里的时间：先跳到那一句，再开始放", async () => {
    const { play, startedAt } = spyPlay();
    const { audio } = renderPage();
    loaded(audio);

    await userEvent.click(screen.getByRole("button", { name: "01:00" }));

    expect(audio.currentTime).toBe(60);
    expect(play).toHaveBeenCalledTimes(1);
    expect(startedAt).toEqual([60]);
  });

  it("录音还没读到就点：读到之后先跳到那里再放，只放一次", async () => {
    const { play, startedAt } = spyPlay();
    const { audio } = renderPage();

    await userEvent.click(screen.getByRole("button", { name: "01:00" }));
    expect(play).not.toHaveBeenCalled();
    loaded(audio);

    expect(audio.currentTime).toBe(60);
    expect(startedAt).toEqual([60]);
  });

  it("已经在放时点另一句：跳过去，接着放", async () => {
    const { play, startedAt } = spyPlay();
    const { audio } = renderPage();
    loaded(audio);
    fireEvent.play(audio);

    await userEvent.click(screen.getByRole("button", { name: "02:00" }));

    expect(audio.currentTime).toBe(120);
    expect(play).toHaveBeenCalledTimes(1);
    expect(startedAt).toEqual([120]);
  });

  it("点的时候浏览器不让放：停在那个位置，没有错误提示，拒绝也被接住了", async () => {
    const { result, catches } = blockedPlay();
    spyPlay(result);
    const { audio } = renderPage();
    loaded(audio);

    await userEvent.click(screen.getByRole("button", { name: "01:00" }));
    await act(async () => {
      await Promise.resolve();
    });

    expect(audio.currentTime).toBe(60);
    expect(catches).toHaveLength(1);
    expect(catches[0]).toHaveBeenCalled();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.queryByText(/播放|自动|拒绝|失败/)).not.toBeInTheDocument();
  });

  it("带时间锚打开（检索结果、词典「听到」）：读到录音就跳到那里放，不用再点", () => {
    const { play, startedAt } = spyPlay();
    const { audio } = renderPage({ initialSeekMs: 60_000 });
    expect(play).not.toHaveBeenCalled();

    loaded(audio);

    expect(audio.currentTime).toBe(60);
    expect(play).toHaveBeenCalledTimes(1);
    expect(startedAt).toEqual([60]);
  });

  it("带时间锚打开但浏览器不让自动放（Safari）：停在那个位置，没有任何提示", async () => {
    const { result, catches } = blockedPlay();
    spyPlay(result);
    const { audio } = renderPage({ initialSeekMs: 60_000 });

    loaded(audio);
    await act(async () => {
      await Promise.resolve();
    });

    expect(audio.currentTime).toBe(60);
    expect(catches).toHaveLength(1);
    expect(catches[0]).toHaveBeenCalled();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.queryByText(/播放|自动|拒绝|失败/)).not.toBeInTheDocument();
  });

  it("没带时间锚打开（从录音档案点进来）：不放", () => {
    const { play } = spyPlay();
    const { audio } = renderPage({ initialSeekMs: 0 });

    loaded(audio);

    expect(play).not.toHaveBeenCalled();
    expect(audio.currentTime).toBe(0);
  });

  it("纪要里点「跳转到证据」：跳到那一秒并放", async () => {
    const evidence = {
      coverage: { total_items: 1, included_items: 1, omitted_items: 0 },
      topics: [
        {
          topic_id: "T01",
          title: "议题",
          start_sec: 0,
          end_sec: 20,
          items: [
            { item_id: "D01", kind: "decision" as const, text: "决定", status: "included" as const, source_start_sec: 12, source_end_sec: 14, minutes_anchor: "[00:00:12]" },
          ],
        },
      ],
      anchors: [{ item_id: "D01", minutes_anchor: "[00:00:12]", source_start_sec: 12, source_end_sec: 14 }],
    };
    const { play, startedAt } = spyPlay();
    const { audio } = renderPage({}, { minutesEvidence: vi.fn().mockResolvedValue(evidence) } as unknown as Partial<ApiClient>);
    loaded(audio);
    await userEvent.click(screen.getByRole("tab", { name: /会议纪要/ }));

    await userEvent.click(await screen.findByRole("button", { name: "跳转到证据 00:12" }));

    expect(audio.currentTime).toBe(12);
    expect(play).toHaveBeenCalledTimes(1);
    expect(startedAt).toEqual([12]);
  });

  it("归属条里点线索的时间：跳到那一秒并放", async () => {
    const attribution: MeetingAttribution = {
      state: "auto",
      project_id: "p-a",
      origin: "ai",
      method: "llm_high",
      evidence: [
        {
          kind: "literal",
          project_id: "p-a",
          project_name: "云图AI",
          cue: "初审规则",
          source: "term",
          count: 6,
          anchors_ms: [75_000],
          where: { title: 0, transcript: 5, minutes: 1 },
        },
      ],
      candidates: [],
      reason: "",
      new_project_name: null,
      reassigned_from: null,
      ai_configured: true,
    };
    const { play, startedAt } = spyPlay();
    const { audio } = renderPage({
      meeting: detail({ attribution, project_id: "p-a", project_name: "云图AI", project_color: "#376f68" }),
      projects: [{ id: "p-a", name: "云图AI", color: "#376f68" }],
    });
    loaded(audio);
    await userEvent.click(screen.getByRole("button", { name: "提到「初审规则」6 次" }));

    await userEvent.click(screen.getByRole("button", { name: "跳到 01:15" }));

    expect(audio.currentTime).toBe(75);
    expect(play).toHaveBeenCalledTimes(1);
    expect(startedAt).toEqual([75]);
  });

  it("相关材料里点「从 … 播放会上这段」：跳过去并放", async () => {
    const relatedMaterials = vi.fn().mockResolvedValue({
      state: { kind: "ok", text: null, action: null },
      files: {
        "q2:3f6c0000000000000000": { file_id: 812, name: "接口文档.docx", ext: "docx", root_online: true, playable: false, can_open: false, state_text: "" },
      },
      copies: [],
      windows: [
        {
          start_ms: 0,
          end_ms: 90_000,
          items: [
            { content_key: "q2:3f6c0000000000000000", ordinal: 14, loc: "第三节", start_ms: null, text: "字段命名统一用小驼峰", words: ["字段命名"], at_ms: 2_000 },
          ],
        },
      ],
      rejected: 0,
    });
    const { play, startedAt } = spyPlay();
    vi.stubGlobal("fetch", vi.fn(() => new Promise(() => {})));
    const view = render(
      <LinksFlagsContext.Provider value={{ linksEnabled: true, semanticEnabled: true, llmConfigured: false }}>
        <MeetingDetailPage
          apiClient={{ relatedMaterials, asrGoldSamples: vi.fn().mockResolvedValue({ items: [] }) } as unknown as ApiClient}
          canWriteTasks
          initialSeekMs={0}
          isMobile={false}
          meeting={detail()}
          onBack={vi.fn()}
          onReload={vi.fn().mockResolvedValue(undefined)}
          projects={[]}
          tags={[]}
        />
      </LinksFlagsContext.Provider>,
    );
    const audio = view.container.querySelector("audio") as HTMLAudioElement;
    Object.defineProperty(audio, "currentTime", { configurable: true, value: 0, writable: true });
    loaded(audio);

    await userEvent.click(await screen.findByRole("button", { name: "从 00:02 播放会上这段" }));

    expect(audio.currentTime).toBe(2);
    expect(play).toHaveBeenCalledTimes(1);
    expect(startedAt).toEqual([2]);
  });

  it("Whisper 对照稿里点时间、对照行里点时间：跳过去并放", async () => {
    const whisper = { id: "tv-whisper", meeting_id: "vm-play", version_no: 2, kind: "whisper_reference", published: 0, created_at: "2026-07-10T00:01:00Z" };
    const transcriptVersionSegments = vi.fn().mockResolvedValue({
      version: whisper,
      items: [{ id: "w1", ordinal: 0, start_ms: 90_000, end_ms: 100_000, speaker_name: "丙", text: "对照稿的这句" }],
    });
    const { play, startedAt } = spyPlay();
    const { audio } = renderPage(
      { meeting: detail({ transcript_versions: [...detail().transcript_versions, whisper] }) },
      { transcriptVersionSegments } as unknown as Partial<ApiClient>,
    );
    loaded(audio);
    await userEvent.click(screen.getByRole("button", { name: "Whisper 对照" }));

    await userEvent.click(await screen.findByRole("button", { name: "01:30" }));

    expect(audio.currentTime).toBe(90);
    expect(play).toHaveBeenCalledTimes(1);
    expect(startedAt).toEqual([90]);
  });

  it("对照行（风险对照）里点时间：跳过去并放", async () => {
    const whisper = { id: "tv-whisper", meeting_id: "vm-play", version_no: 2, kind: "whisper_reference", published: 0, created_at: "2026-07-10T00:01:00Z" };
    const transcriptComparison = vi.fn().mockResolvedValue({
      primary_version_id: "tv-1",
      candidate_version_id: "tv-whisper",
      candidate_kind: "whisper_reference",
      items: [
        {
          primary_segment_id: "s1",
          candidate_segment_ids: ["w1"],
          start_ms: 60_000,
          end_ms: 120_000,
          primary_text: "一分钟处的这句",
          candidate_text: "一分钟处这句",
          risk_kinds: ["text"],
          similarity: 0.8,
        },
      ],
    });
    const { play, startedAt } = spyPlay();
    const { audio } = renderPage(
      { meeting: detail({ transcript_versions: [...detail().transcript_versions, whisper] }) },
      { transcriptComparison } as unknown as Partial<ApiClient>,
    );
    loaded(audio);
    await userEvent.click(screen.getByRole("button", { name: "Whisper 对照" }));

    await userEvent.click(await screen.findByRole("button", { name: "对照第 1 段，跳转到 01:00" }));

    expect(audio.currentTime).toBe(60);
    expect(play).toHaveBeenCalledTimes(1);
    expect(startedAt).toEqual([60]);
  });
});
