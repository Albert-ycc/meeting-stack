import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { LibraryPage } from "./LibraryPage";

describe("LibraryPage pagination", () => {
  it("shows the total and moves between fixed 50-item pages", async () => {
    const onPageChange = vi.fn();
    render(
      <LibraryPage
        filters={{}}
        limit={50}
        meetings={[
          {
            id: "vm-51",
            title: "第二页会议",
            status: "published",
            tags: [],
          },
        ]}
        offset={50}
        onFilter={vi.fn()}
        onOpen={vi.fn()}
        onPageChange={onPageChange}
        projects={[]}
        state="ready"
        tags={[]}
        total={120}
      />,
    );

    expect(screen.getByText("120")).toBeInTheDocument();
    expect(screen.getByText("第 2 / 3 页")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "上一页" }));
    await userEvent.click(screen.getByRole("button", { name: "下一页" }));
    expect(onPageChange).toHaveBeenNthCalledWith(1, 0);
    expect(onPageChange).toHaveBeenNthCalledWith(2, 100);
  });

  it("copies the canonical meeting folder without opening the meeting", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText },
    });
    const onOpen = vi.fn();
    const archivedMeeting = {
      id: "vm-1",
      title: "路径测试会议",
      status: "published",
      tags: [],
      canonical_dir: "/Volumes/资料盘/会议纪要与录音/260713 路径测试会议",
    };

    render(
      <LibraryPage
        filters={{}}
        limit={50}
        meetings={[archivedMeeting]}
        offset={0}
        onFilter={vi.fn()}
        onOpen={onOpen}
        onPageChange={vi.fn()}
        projects={[]}
        state="ready"
        tags={[]}
        total={1}
      />,
    );

    await userEvent.click(
      screen.getByRole("button", { name: "复制文件夹路径" }),
    );

    expect(writeText).toHaveBeenCalledWith(archivedMeeting.canonical_dir);
    expect(onOpen).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "文件夹路径已复制" })).toHaveAccessibleDescription(
      "路径测试会议",
    );
  });

  it("keeps folder copying as a quiet icon action instead of a permanent list column", () => {
    render(
      <LibraryPage
        filters={{}}
        limit={50}
        meetings={[
          {
            id: "vm-quiet-copy",
            title: "安静的路径操作",
            status: "published",
            tags: [],
            canonical_dir: "/Volumes/资料盘/会议纪要与录音/安静的路径操作",
          },
        ]}
        offset={0}
        onFilter={vi.fn()}
        onOpen={vi.fn()}
        onPageChange={vi.fn()}
        projects={[]}
        state="ready"
        tags={[]}
        total={1}
      />,
    );

    const copyButton = screen.getByRole("button", { name: "复制文件夹路径" });
    expect(screen.queryByText("文件夹")).not.toBeInTheDocument();
    expect(copyButton.textContent).toBe("");
    expect(copyButton.querySelector("svg")).toBeInTheDocument();
  });

  it("marks an AI-matched project chip with a title and an AI mark", () => {
    render(
      <LibraryPage
        filters={{}}
        limit={50}
        meetings={[
          {
            id: "vm-ai-origin",
            title: "自动归属会议",
            status: "published",
            tags: [],
            project_id: "project-a",
            project_name: "自动匹配项目",
            project_color: "#f0783b",
            project_origin: "ai",
          },
        ]}
        offset={0}
        onFilter={vi.fn()}
        onOpen={vi.fn()}
        onPageChange={vi.fn()}
        projects={[]}
        state="ready"
        tags={[]}
        total={1}
      />,
    );

    const chip = screen.getByText("自动匹配项目").closest(".project-mark");
    expect(chip).toHaveAttribute("title", "AI 自动归属");
    expect(chip).toHaveTextContent("AI");
  });

  it("does not expose an empty participant filter without voice identification", () => {
    render(
      <LibraryPage
        filters={{}}
        limit={50}
        meetings={[]}
        offset={0}
        onFilter={vi.fn()}
        onOpen={vi.fn()}
        onPageChange={vi.fn()}
        projects={[]}
        state="empty"
        tags={[]}
        total={0}
      />,
    );

    expect(screen.queryByText("参与人")).not.toBeInTheDocument();
  });
});

describe("LibraryPage day grouping", () => {
  it("groups meetings under their recording day and counts each day", () => {
    const { container } = render(
      <LibraryPage
        filters={{}}
        limit={50}
        meetings={[
          {
            id: "vm-a",
            title: "上午的会",
            recording_date: "2026-03-11T09:00:00",
            duration_ms: 1_800_000,
            status: "completed_unreviewed",
            tags: [],
          },
          {
            id: "vm-b",
            title: "下午的会",
            recording_date: "2026-03-11T15:00:00",
            duration_ms: 1_800_000,
            status: "published",
            tags: [],
          },
          {
            id: "vm-c",
            title: "vm-c",
            recording_date: "2026-03-10T10:00:00",
            duration_ms: 600_000,
            status: "completed_unreviewed",
            tags: [],
          },
        ]}
        offset={0}
        onFilter={vi.fn()}
        onOpen={vi.fn()}
        onPageChange={vi.fn()}
        projects={[]}
        state="ready"
        tags={[]}
        total={3}
      />,
    );

    const days = container.querySelectorAll(".archive-day");
    expect(days).toHaveLength(2);
    expect(days[0].querySelectorAll(".archive-row")).toHaveLength(2);
    expect(days[0].textContent).toContain("1 小时");
    expect(days[1].querySelectorAll(".archive-row")).toHaveLength(1);
    expect(container.querySelectorAll(".untitled-chip")).toHaveLength(1);
  });

  it("offers only the two states the workflow actually uses", () => {
    const onFilter = vi.fn();
    render(
      <LibraryPage
        filters={{}}
        limit={50}
        meetings={[]}
        offset={0}
        onFilter={onFilter}
        onOpen={vi.fn()}
        onPageChange={vi.fn()}
        projects={[]}
        state="empty"
        tags={[]}
        total={0}
      />,
    );

    const options = Array.from(
      screen.getByLabelText("筛选状态").querySelectorAll("option"),
    ).map((option) => option.textContent);
    expect(options).toEqual(["全部状态", "已完成", "失败"]);
  });
});
