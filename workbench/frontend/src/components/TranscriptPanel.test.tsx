import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { rowAtLine, TranscriptPanel } from "./TranscriptPanel";

const segments = [
  {
    id: "seg-a",
    ordinal: 0,
    start_ms: 0,
    end_ms: 5_000,
    speaker_label: "speaker_0",
    speaker_name: "甲",
    text: "先确认范围。",
  },
  {
    id: "seg-b",
    ordinal: 1,
    start_ms: 5_000,
    end_ms: 12_000,
    speaker_label: "speaker_1",
    speaker_name: "乙",
    text: "范围已经确认。",
  },
];

describe("TranscriptPanel", () => {
  it("highlights the segment containing the playback time", () => {
    render(
      <TranscriptPanel
        currentTimeMs={7_200}
        editable={false}
        onSeek={vi.fn()}
        segments={segments}
      />,
    );

    expect(screen.getByTestId("segment-seg-b")).toHaveAttribute("aria-current", "true");
    expect(screen.getByTestId("segment-seg-a")).not.toHaveAttribute("aria-current");
  });

  it("captures the textarea cursor before updating split state", () => {
    const onSplit = vi.fn();
    render(
      <TranscriptPanel
        currentTimeMs={0}
        editable
        onSeek={vi.fn()}
        onSplit={onSplit}
        segments={segments}
      />,
    );
    const textarea = screen.getByLabelText("00:00 逐字稿") as HTMLTextAreaElement;
    textarea.setSelectionRange(3, 3);
    fireEvent.keyUp(textarea, { key: "ArrowRight" });

    const split = screen.getAllByRole("button", { name: "从光标拆分" })[0];
    expect(split).toBeEnabled();
    fireEvent.click(split);
    expect(onSplit).toHaveBeenCalledWith("seg-a", 3);
  });

  it("每行带 data-start-ms（4d）", () => {
    render(<TranscriptPanel currentTimeMs={0} editable={false} onSeek={vi.fn()} segments={segments} />);
    expect(screen.getByTestId("segment-seg-b")).toHaveAttribute("data-start-ms", "5000");
  });

  it("用户自己滚动时上报阅读线下那一行，自动跟随时上报 null（4d）", () => {
    const onReadingTimeChange = vi.fn();
    const raf = vi.spyOn(window, "requestAnimationFrame").mockImplementation((callback) => {
      callback(0);
      return 1;
    });
    const scrollIntoView = vi.fn();
    Element.prototype.scrollIntoView = scrollIntoView;
    const { container, rerender } = render(
      <TranscriptPanel currentTimeMs={0} editable={false} onReadingTimeChange={onReadingTimeChange} onSeek={vi.fn()} segments={segments} />,
    );
    // 自动跟随滚到播放行：上报 null
    expect(onReadingTimeChange).toHaveBeenLastCalledWith(null);
    const box = container.querySelector(".transcript-scroll") as HTMLDivElement;
    Object.defineProperty(box, "clientHeight", { configurable: true, value: 300 });
    Object.defineProperty(box, "scrollTop", { configurable: true, value: 100, writable: true });
    vi.spyOn(box, "getBoundingClientRect").mockReturnValue({ top: 0 } as DOMRect);
    vi.spyOn(screen.getByTestId("segment-seg-a"), "getBoundingClientRect").mockReturnValue({ top: -100 } as DOMRect);
    vi.spyOn(screen.getByTestId("segment-seg-b"), "getBoundingClientRect").mockReturnValue({ top: 50 } as DOMRect);
    // 没有手动滚（程序滚）的 scroll 不上报
    onReadingTimeChange.mockClear();
    fireEvent.scroll(box);
    expect(onReadingTimeChange).not.toHaveBeenCalled();
    // 滚轮之后的 scroll：阅读线在 100 + 300/3 = 200，seg-b 的上沿 150 在线上
    fireEvent.wheel(box);
    fireEvent.scroll(box);
    expect(onReadingTimeChange).toHaveBeenLastCalledWith(5_000);
    // 翻页键也算
    onReadingTimeChange.mockClear();
    rerender(
      <TranscriptPanel currentTimeMs={0} editable={false} onReadingTimeChange={onReadingTimeChange} onSeek={vi.fn()} segments={segments} />,
    );
    fireEvent.keyDown(box, { key: "PageDown" });
    fireEvent.scroll(box);
    expect(onReadingTimeChange).toHaveBeenLastCalledWith(5_000);
    raf.mockRestore();
  });

  it("rowAtLine：上沿不超过阅读线的最后一行", () => {
    const rows = [
      { startMs: 0, top: 0 },
      { startMs: 5_000, top: 120 },
      { startMs: 9_000, top: 260 },
    ];
    expect(rowAtLine(rows, 0, 300)).toBe(0);
    expect(rowAtLine(rows, 30, 300)).toBe(5_000);
    expect(rowAtLine(rows, 200, 300)).toBe(9_000);
    expect(rowAtLine([], 0, 300)).toBeNull();
  });
});

// 云课堂那场会的逐字稿（照生产库，见后端 requirement_pool_world）
const CVM_SEGMENTS = [
  [568390, "SPEAKER_03", "预约审核查看。"],
  [576900, "SPEAKER_01", "那我有办法导出 excel 吗？"],
  [581000, "SPEAKER_01", "是没有办法，"],
  [582060, "SPEAKER_01", "我看到导出是一个 OKOK。"],
].map(([start, speaker, text], ordinal) => ({
  id: `seg-${start}`,
  ordinal,
  start_ms: start as number,
  end_ms: (start as number) + 1000,
  speaker_label: speaker as string,
  text: text as string,
}));

/** 在两句正文之间拉一个选区（第一句从 fromOffset 起，最后一句到 toOffset 止），然后松开鼠标 */
function selectAcross(fromId: string, fromOffset: number, toId: string, toOffset: number) {
  const textOf = (id: string) => screen.getByTestId(`segment-seg-${id}`).querySelector(".segment-text")!.firstChild!;
  const range = document.createRange();
  range.setStart(textOf(fromId), fromOffset);
  range.setEnd(textOf(toId), toOffset);
  const selection = window.getSelection()!;
  selection.removeAllRanges();
  selection.addRange(range);
  fireEvent.mouseUp(screen.getByTestId(`segment-seg-${toId}`));
}

describe("TranscriptPanel 选中一段建成需求（R01-10）", () => {
  it("跨了两句：原话取选中的文字（不带时间和说话人），时间锚取第一句的开始", () => {
    const onCreateRequirement = vi.fn();
    render(
      <TranscriptPanel
        currentTimeMs={0}
        editable={false}
        onCreateRequirement={onCreateRequirement}
        onSeek={vi.fn()}
        segments={CVM_SEGMENTS}
      />,
    );

    selectAcross("576900", 0, "581000", 6);
    const bar = screen.getByRole("toolbar", { name: "选中的原话" });
    expect(bar).toHaveTextContent("00:09:36");
    fireEvent.click(screen.getByRole("button", { name: "建成需求" }));

    expect(onCreateRequirement).toHaveBeenCalledWith({ quote: "那我有办法导出 excel 吗？是没有办法，", anchorMs: 576900 });
    expect(screen.queryByRole("toolbar", { name: "选中的原话" })).not.toBeInTheDocument();
  });

  it("从句子中间选起，原话只取选中的那几个字", () => {
    const onCreateRequirement = vi.fn();
    render(
      <TranscriptPanel currentTimeMs={0} editable={false} onCreateRequirement={onCreateRequirement} onSeek={vi.fn()} segments={CVM_SEGMENTS} />,
    );

    selectAcross("576900", 3, "576900", 15);
    fireEvent.click(screen.getByRole("button", { name: "建成需求" }));
    expect(onCreateRequirement).toHaveBeenCalledWith({ quote: "办法导出 excel 吗", anchorMs: 576900 });
  });

  it("编辑逐字稿时、没有建需求的入口时、点一下没选字时都不浮", () => {
    const { rerender } = render(
      <TranscriptPanel currentTimeMs={0} editable={false} onSeek={vi.fn()} segments={CVM_SEGMENTS} />,
    );
    selectAcross("576900", 0, "581000", 6);
    expect(screen.queryByRole("toolbar")).not.toBeInTheDocument();

    rerender(
      <TranscriptPanel currentTimeMs={0} editable={false} onCreateRequirement={vi.fn()} onSeek={vi.fn()} segments={CVM_SEGMENTS} />,
    );
    window.getSelection()!.removeAllRanges();
    fireEvent.mouseUp(screen.getByTestId("segment-seg-576900"));
    expect(screen.queryByRole("toolbar")).not.toBeInTheDocument();
    selectAcross("576900", 0, "576900", 4);
    expect(screen.getByRole("toolbar", { name: "选中的原话" })).toBeInTheDocument();

    rerender(
      <TranscriptPanel currentTimeMs={0} editable onCreateRequirement={vi.fn()} onSeek={vi.fn()} segments={CVM_SEGMENTS} />,
    );
    expect(screen.queryByRole("toolbar")).not.toBeInTheDocument();
  });

  it("选中的超过 1000 字不给建，提示少选几句", () => {
    const long = [{ ...CVM_SEGMENTS[0], text: "预".repeat(1001) }];
    render(
      <TranscriptPanel currentTimeMs={0} editable={false} onCreateRequirement={vi.fn()} onSeek={vi.fn()} segments={long} />,
    );
    selectAcross("568390", 0, "568390", 1001);
    expect(screen.getByRole("toolbar", { name: "选中的原话" })).toHaveTextContent("选中的超过 1000 字，少选几句");
    expect(screen.queryByRole("button", { name: "建成需求" })).not.toBeInTheDocument();
  });
});
