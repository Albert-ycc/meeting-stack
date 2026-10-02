import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";

import type { Segment } from "../types";
import { TranscriptPanel } from "./TranscriptPanel";

// 每一行渲染都会调一次 formatTime(start_ms)（编辑态给 textarea 的 aria-label 再调一次）；
// 播放器、选区浮条调的是带第二个参数的写法。这里只数单参数的，也就是「哪些行被重新渲染了」。
const rowFormats = vi.hoisted(() => [] as number[]);
vi.mock("../format", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../format")>();
  return {
    ...actual,
    formatTime: (...args: Parameters<typeof actual.formatTime>) => {
      if (args.length === 1) rowFormats.push(args[0] as number);
      return actual.formatTime(...args);
    },
  };
});

const renderedRows = () => [...new Set(rowFormats)].sort((left, right) => left - right);

// 每句 10 秒，300 句：第 150 句从 25:00 起
const segments: Segment[] = Array.from({ length: 300 }, (_, index) => ({
  id: `seg-${index}`,
  ordinal: index,
  start_ms: index * 10_000,
  end_ms: index * 10_000 + 9_000,
  speaker_label: `SPEAKER_0${index % 3}`,
  speaker_name: null,
  text: `第${index}句的内容。`,
}));
const AT_150 = 150 * 10_000;

describe("TranscriptPanel 只重画变了的行", () => {
  it("编辑时敲一个字：只有这一行重新渲染，别的 299 行不动", async () => {
    function Host() {
      const [value, setValue] = useState(segments);
      return <TranscriptPanel currentTimeMs={0} editable onChange={setValue} onSeek={vi.fn()} segments={value} />;
    }
    render(<Host />);
    const box = screen.getByLabelText("25:00 逐字稿") as HTMLTextAreaElement;

    rowFormats.length = 0;
    await userEvent.type(box, "字");

    expect(box).toHaveValue("第150句的内容。字");
    expect(box).toHaveFocus();
    expect(renderedRows()).toEqual([AT_150]);
  });

  it("连着敲几个字，每一下都只动这一行；换到另一行接着敲也是", async () => {
    function Host() {
      const [value, setValue] = useState(segments);
      return <TranscriptPanel currentTimeMs={0} editable onChange={setValue} onSeek={vi.fn()} segments={value} />;
    }
    render(<Host />);

    rowFormats.length = 0;
    await userEvent.type(screen.getByLabelText("25:00 逐字稿"), "一二三");
    expect(renderedRows()).toEqual([AT_150]);

    rowFormats.length = 0;
    await userEvent.type(screen.getByLabelText("00:10 逐字稿"), "四");
    expect(renderedRows()).toEqual([10_000]);
    expect(screen.getByLabelText("25:00 逐字稿")).toHaveValue("第150句的内容。一二三");
    expect(screen.getByLabelText("00:10 逐字稿")).toHaveValue("第1句的内容。四");
  });

  it("播放推进：同一句里推进不重画任何行，跨到下一句只重画换下和换上的两行", () => {
    const { rerender } = render(
      <TranscriptPanel currentTimeMs={AT_150 + 1_000} editable={false} onSeek={vi.fn()} segments={segments} />,
    );
    expect(screen.getByTestId("segment-seg-150")).toHaveAttribute("aria-current", "true");

    rowFormats.length = 0;
    rerender(<TranscriptPanel currentTimeMs={AT_150 + 5_000} editable={false} onSeek={vi.fn()} segments={segments} />);
    expect(renderedRows()).toEqual([]);

    rerender(<TranscriptPanel currentTimeMs={AT_150 + 11_000} editable={false} onSeek={vi.fn()} segments={segments} />);
    expect(renderedRows()).toEqual([AT_150, AT_150 + 10_000]);
    expect(screen.getByTestId("segment-seg-151")).toHaveAttribute("aria-current", "true");
    expect(screen.getByTestId("segment-seg-150")).not.toHaveAttribute("aria-current");
  });

  it("父组件每次渲染都换新的回调（拆分、合并、改字、跳转）也不让所有行重画", async () => {
    function Host() {
      const [value, setValue] = useState(segments);
      const [, setTick] = useState(0);
      return (
        <>
          <button onClick={() => setTick((count) => count + 1)} type="button">
            父组件重画
          </button>
          <TranscriptPanel
            currentTimeMs={0}
            editable
            onChange={(next) => setValue(next)}
            onMerge={() => undefined}
            onSeek={() => undefined}
            onSplit={() => undefined}
            segments={value}
          />
        </>
      );
    }
    render(<Host />);

    rowFormats.length = 0;
    await userEvent.click(screen.getByRole("button", { name: "父组件重画" }));

    expect(renderedRows()).toEqual([]);
  });

  it("只改一行的说话人名：只重画这一行", () => {
    const props = { currentTimeMs: 0, editable: true, onSeek: vi.fn() };
    const { rerender } = render(<TranscriptPanel {...props} segments={segments} />);

    rowFormats.length = 0;
    const renamed = segments.map((segment) => (segment.id === "seg-150" ? { ...segment, speaker_name: "甲" } : segment));
    rerender(<TranscriptPanel {...props} segments={renamed} />);

    expect(renderedRows()).toEqual([AT_150]);
    expect(screen.getByTestId("segment-seg-150")).toHaveTextContent("甲");
  });

  it("「与上一段合并」带整份逐字稿里的上一段，「从光标拆分」带这一段", async () => {
    const onSplit = vi.fn();
    const onMerge = vi.fn();
    render(<TranscriptPanel currentTimeMs={0} editable onMerge={onMerge} onSeek={vi.fn()} onSplit={onSplit} segments={segments} />);

    await userEvent.click(screen.getAllByRole("button", { name: "与上一段合并" })[149]);
    expect(onMerge).toHaveBeenLastCalledWith("seg-149", "seg-150");
    // 第一段没有上一段，也就没有这个按钮
    expect(screen.getAllByRole("button", { name: "与上一段合并" })).toHaveLength(299);

    const box = screen.getByLabelText("25:00 逐字稿") as HTMLTextAreaElement;
    await userEvent.click(box);
    box.setSelectionRange(3, 3);
    await userEvent.keyboard("{ArrowRight}");
    await userEvent.click(screen.getAllByRole("button", { name: "从光标拆分" })[150]);
    expect(onSplit).toHaveBeenCalledWith("seg-150", 4);
  });

  it("查找过滤后合并带的仍是整份逐字稿里的上一段（它可能被藏起来了），data-index 是在整份里的位置", async () => {
    const onMerge = vi.fn();
    render(<TranscriptPanel currentTimeMs={0} editable onMerge={onMerge} onSeek={vi.fn()} segments={segments} />);

    await userEvent.type(screen.getByLabelText("在本次逐字稿中搜索"), "第150句");

    expect(screen.getByTestId("segment-seg-150")).toHaveAttribute("data-index", "150");
    expect(screen.queryByTestId("segment-seg-149")).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "与上一段合并" }));
    expect(onMerge).toHaveBeenCalledWith("seg-149", "seg-150");
  });
});
