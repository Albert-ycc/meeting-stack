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

  it("跳进会议时跟到的头一句滚到框的正中，之后播放推进只挪到刚好看得见（R02-10）", () => {
    const original = Element.prototype.scrollIntoView;
    const scrollIntoView = vi.fn();
    Element.prototype.scrollIntoView = scrollIntoView;
    try {
      // 从原话时间锚 00:00:05 打开：这一句滚到正中
      const { rerender } = render(
        <TranscriptPanel currentTimeMs={5_000} editable={false} onSeek={vi.fn()} segments={segments} />,
      );
      expect(scrollIntoView).toHaveBeenCalledTimes(1);
      expect(scrollIntoView).toHaveBeenLastCalledWith({ block: "center", behavior: "smooth" });
      expect(scrollIntoView.mock.contexts[0]).toBe(screen.getByTestId("segment-seg-b"));

      // 播放回到前一句（往回拖了进度）：只挪到刚好看得见
      rerender(<TranscriptPanel currentTimeMs={1_000} editable={false} onSeek={vi.fn()} segments={segments} />);
      expect(scrollIntoView).toHaveBeenCalledTimes(2);
      expect(scrollIntoView).toHaveBeenLastCalledWith({ block: "nearest", behavior: "smooth" });
    } finally {
      Element.prototype.scrollIntoView = original;
    }
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

  it("查找过滤后跨着藏起来的句子选：不挨着的地方补「……」，挨着的照样直接接上（第二轮审查一般-1）", async () => {
    const onCreateRequirement = vi.fn();
    render(
      <TranscriptPanel currentTimeMs={0} editable={false} onCreateRequirement={onCreateRequirement} onSeek={vi.fn()} segments={CVM_SEGMENTS} />,
    );
    // 查「导出」：第 2 句和第 4 句看得见，中间的「是没有办法，」藏起来了
    fireEvent.change(screen.getByLabelText("在本次逐字稿中搜索"), { target: { value: "导出" } });
    expect(screen.queryByTestId("segment-seg-581000")).not.toBeInTheDocument();

    selectAcross("576900", 0, "582060", 14);
    fireEvent.click(screen.getByRole("button", { name: "建成需求" }));

    expect(onCreateRequirement).toHaveBeenLastCalledWith({
      quote: "那我有办法导出 excel 吗？……我看到导出是一个 OKOK。",
      anchorMs: 576900,
    });

    // 不过滤时挨着的两句直接接上
    fireEvent.change(screen.getByLabelText("在本次逐字稿中搜索"), { target: { value: "" } });
    selectAcross("576900", 0, "581000", 6);
    fireEvent.click(screen.getByRole("button", { name: "建成需求" }));
    expect(onCreateRequirement).toHaveBeenLastCalledWith({ quote: "那我有办法导出 excel 吗？是没有办法，", anchorMs: 576900 });
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

  it("逐字稿有没保存的修改时，浮条只说原因、不给建（选中的是没存下来的字）", () => {
    const onCreateRequirement = vi.fn();
    render(
      <TranscriptPanel
        currentTimeMs={0}
        editable={false}
        onCreateRequirement={onCreateRequirement}
        onSeek={vi.fn()}
        pickBlockedReason="逐字稿有没保存的修改，先保存或放弃再选句"
        segments={CVM_SEGMENTS}
      />,
    );

    selectAcross("576900", 0, "581000", 6);

    expect(screen.getByRole("toolbar", { name: "选中的原话" })).toHaveTextContent("逐字稿有没保存的修改，先保存或放弃再选句");
    expect(screen.queryByRole("button", { name: "建成需求" })).not.toBeInTheDocument();
  });

  it("只选中了标点不给建，提示换一句", () => {
    render(
      <TranscriptPanel currentTimeMs={0} editable={false} onCreateRequirement={vi.fn()} onSeek={vi.fn()} segments={CVM_SEGMENTS} />,
    );

    // 「那我有办法导出 excel 吗？」里只选句末的问号
    selectAcross("576900", 15, "576900", 16);

    expect(screen.getByRole("toolbar", { name: "选中的原话" })).toHaveTextContent("选中的只有标点，换一句");
    expect(screen.queryByRole("button", { name: "建成需求" })).not.toBeInTheDocument();
  });

  it("在逐字稿里按下、拖到框外才松开，照样浮出［建成需求］", () => {
    const onCreateRequirement = vi.fn();
    render(
      <TranscriptPanel currentTimeMs={0} editable={false} onCreateRequirement={onCreateRequirement} onSeek={vi.fn()} segments={CVM_SEGMENTS} />,
    );
    fireEvent.mouseDown(screen.getByTestId("segment-seg-576900"));
    const textOf = (id: string) => screen.getByTestId(`segment-seg-${id}`).querySelector(".segment-text")!.firstChild!;
    const range = document.createRange();
    range.setStart(textOf("576900"), 0);
    range.setEnd(textOf("581000"), 6);
    window.getSelection()!.removeAllRanges();
    window.getSelection()!.addRange(range);

    fireEvent.mouseUp(document.body);

    fireEvent.click(screen.getByRole("button", { name: "建成需求" }));
    expect(onCreateRequirement).toHaveBeenCalledWith({ quote: "那我有办法导出 excel 吗？是没有办法，", anchorMs: 576900 });
  });

  it("浮条贴在选区最后一行下面，夹在滚动框看得见的范围里", () => {
    const { container } = render(
      <TranscriptPanel currentTimeMs={0} editable={false} onCreateRequirement={vi.fn()} onSeek={vi.fn()} segments={CVM_SEGMENTS} />,
    );
    const box = container.querySelector<HTMLElement>(".transcript-scroll")!;
    // 滚动框在视口 y=100 处、高 400、宽 800，已经往下滚了 500
    Object.defineProperty(box, "scrollTop", { configurable: true, value: 500, writable: true });
    Object.defineProperty(box, "clientHeight", { configurable: true, value: 400 });
    Object.defineProperty(box, "clientWidth", { configurable: true, value: 800 });
    vi.spyOn(box, "getBoundingClientRect").mockReturnValue({
      top: 100, left: 50, right: 850, bottom: 500, width: 800, height: 400, x: 50, y: 100, toJSON: () => ({}),
    });
    const lines = (lastBottom: number) => [
      { top: -880, bottom: -860, left: 60, right: 300 },
      { top: lastBottom - 20, bottom: lastBottom, left: 60, right: 450 },
    ];
    const original = Range.prototype.getClientRects;
    try {
      // 选了好几屏：第一行早滚出视野，最后一行在可见范围里 → 贴在最后一行下面
      Range.prototype.getClientRects = vi.fn(() => lines(320)) as unknown as typeof original;
      selectAcross("576900", 0, "581000", 6);
      expect(screen.getByRole("toolbar", { name: "选中的原话" })).toHaveStyle({ top: "726px", left: "280px" });

      // 松手时最后一行还在框外（拖出了框）→ 夹到可见范围底边
      Range.prototype.getClientRects = vi.fn(() => lines(2000)) as unknown as typeof original;
      selectAcross("576900", 0, "581000", 6);
      expect(screen.getByRole("toolbar", { name: "选中的原话" })).toHaveStyle({ top: "856px" });
    } finally {
      Range.prototype.getClientRects = original;
    }
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
