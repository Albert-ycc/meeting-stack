import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { AskSource } from "../../types";
import { AnswerText, splitAnswer } from "./AnswerText";

const T1: AskSource = {
  id: "T1",
  kind: "meeting",
  meeting_id: "m-1",
  title: "周会",
  date: `${new Date().getFullYear()}-09-14`,
  start_ms: 310_000,
  audio_url: null,
  text: "原话",
  quote: "原话",
};
const handlers = { onOpenMeeting: vi.fn(), onOpenPreview: vi.fn(), player: { play: vi.fn() }, highlight: [] };

describe("AnswerText", () => {
  it("切分：文字和标记各成一块，相邻的标记、开头的标记都分开", () => {
    expect(splitAnswer("[T1]开头，中间[D1][M2]结尾")).toEqual([
      { kind: "cite", id: "T1" },
      { kind: "text", text: "开头，中间" },
      { kind: "cite", id: "D1" },
      { kind: "cite", id: "M2" },
      { kind: "text", text: "结尾" },
    ]);
    expect(splitAnswer("没有标记")).toEqual([{ kind: "text", text: "没有标记" }]);
    // 小写、三位数和别的字母都不是标记
    expect(splitAnswer("[t1][T123][X1]")).toEqual([{ kind: "text", text: "[t1][T123][X1]" }]);
  });

  it("<img src=x onerror=…> 当文字显示，不出元素", () => {
    const { container } = render(
      <AnswerText handlers={handlers} sources={[T1]} text={'<img src=x onerror="alert(1)">[T1]'} />,
    );
    expect(container.querySelector("img")).toBeNull();
    expect(screen.getByText('<img src=x onerror="alert(1)">')).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "出处：9/14 周会 05:10" })).toBeInTheDocument();
  });

  it("不在来源里的标记照原样当文字", () => {
    const { container } = render(<AnswerText handlers={handlers} sources={[T1]} text="见[D4]" />);
    expect(container).toHaveTextContent("见[D4]");
    expect(container.querySelector("button")).toBeNull();
  });
});
