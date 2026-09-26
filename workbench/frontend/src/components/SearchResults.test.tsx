import userEvent from "@testing-library/user-event";
import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { SearchResults } from "./SearchResults";

describe("SearchResults", () => {
  it("opens a meeting at the result time anchor", async () => {
    const onOpen = vi.fn();
    render(
      <SearchResults
        items={[
          {
            segment_id: "seg-1",
            meeting_id: "vm-20260710",
            title: "产品周会",
            start_ms: 92_000,
            end_ms: 101_000,
            speaker_name: "张三",
            speaker_label: "speaker_0",
            text: "把发布验收放到今天下午。",
          },
        ]}
        highlight
        onOpen={onOpen}
        query="发布"
      />,
    );

    await userEvent.click(screen.getByRole("button", { name: /01:32/ }));
    expect(onOpen).toHaveBeenCalledWith("vm-20260710", 92_000);
  });

  it("labels title and segment matches and only highlights exact matched text", () => {
    render(
      <SearchResults
        items={[
          {
            segment_id: "seg-title",
            meeting_id: "vm-title",
            title: "发布评审会",
            start_ms: 0,
            end_ms: 1_000,
            text: "讨论本周工作",
            match_kind: "title",
          },
          {
            segment_id: "seg-body",
            meeting_id: "vm-body",
            title: "产品周会",
            start_ms: 1_000,
            end_ms: 2_000,
            text: "今天确认发布范围",
            match_kind: "segment",
          },
        ]}
        highlight
        onOpen={vi.fn()}
        query="发布"
      />,
    );

    expect(screen.getByText("标题命中")).toBeInTheDocument();
    expect(screen.getByText("逐字稿命中")).toBeInTheDocument();
    expect(screen.getAllByText("发布", { selector: "mark" })).toHaveLength(2);
  });

  it("copies the meeting folder path straight from a search hit", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText },
    });
    const onOpen = vi.fn();
    render(
      <SearchResults
        items={[
          {
            segment_id: "seg-1",
            meeting_id: "vm-20260729",
            title: "ACME 内容征集流程走查",
            canonical_dir: "/Volumes/资料盘/会议纪要与录音/260729 ACME内容征集流程走查",
            recording_date: "2026-07-29T02:09:03+00:00",
            start_ms: 92_000,
            end_ms: 101_000,
            text: "又是签署授权时长书是两份材料，",
            match_kind: "segment",
          },
        ]}
        highlight
        onOpen={onOpen}
        query="材料"
      />,
    );

    await userEvent.click(screen.getByRole("button", { name: "复制文件夹路径" }));

    expect(writeText).toHaveBeenCalledWith(
      "/Volumes/资料盘/会议纪要与录音/260729 ACME内容征集流程走查",
    );
    expect(onOpen).not.toHaveBeenCalled();
    expect(await screen.findByRole("button", { name: "文件夹路径已复制" })).toBeInTheDocument();
  });

  it("disables the copy control when a hit has no folder yet", () => {
    render(
      <SearchResults
        items={[
          {
            segment_id: "seg-2",
            meeting_id: "vm-no-dir",
            title: "尚未归档",
            start_ms: 0,
            end_ms: 1_000,
            text: "临时录音",
            match_kind: "segment",
          },
        ]}
        highlight
        onOpen={vi.fn()}
        query="录音"
      />,
    );

    expect(screen.getByRole("button", { name: "暂无文件夹路径" })).toBeDisabled();
  });

  it("labels minutes hits, highlights the spelling that matched and opens the minutes tab", async () => {
    const onOpen = vi.fn();
    render(
      <SearchResults
        highlight
        items={[
          {
            segment_id: null,
            meeting_id: "vm-minutes",
            title: "初审规则沟通",
            start_ms: 754_000,
            end_ms: null,
            text: "会上决定成立树立协会",
            match_kind: "minutes",
            matched: "树立协会",
            project_name: "云图AI",
          },
          {
            segment_id: null,
            meeting_id: "vm-minutes",
            title: "初审规则沟通",
            start_ms: null,
            end_ms: null,
            text: "树立协会由岳总牵头",
            match_kind: "minutes",
            matched: "树立协会",
          },
        ]}
        onOpen={onOpen}
        query="数理协会"
      />,
    );

    expect(screen.getAllByText("纪要命中")).toHaveLength(2);
    expect(screen.getAllByText("树立协会", { selector: "mark" })).toHaveLength(2);
    expect(screen.queryByText("未标记说话人")).toBeNull();
    expect(screen.getByText("云图AI")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: /12:34/ }));
    expect(onOpen).toHaveBeenLastCalledWith("vm-minutes", 754_000, "minutes");
    await userEvent.click(screen.getByRole("button", { name: "打开纪要" }));
    expect(onOpen).toHaveBeenLastCalledWith("vm-minutes", 0, "minutes");
  });

  it("does not fabricate highlights for semantic results", () => {
    render(
      <SearchResults
        items={[
          {
            segment_id: "seg-semantic",
            meeting_id: "vm-semantic",
            title: "产品周会",
            start_ms: 1_000,
            end_ms: 2_000,
            text: "今天确认上线范围",
            match_kind: "segment",
          },
        ]}
        highlight={false}
        onOpen={vi.fn()}
        query="发布"
      />,
    );

    expect(document.querySelector("mark")).toBeNull();
  });
});
