import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "../../api";
import { MentionedBadge } from "./MentionedBadge";
import { clearMentionedCounts, useMentionedCounts } from "./useMentionedCounts";

function List({ api, ids, version = "1" }: { api: { mentionedCounts?: (ids: number[]) => Promise<{ counts: Record<string, number> }> }; ids: number[]; version?: string }) {
  const counts = useMentionedCounts(api, ids, version);
  if (!counts) return <p>没有小签</p>;
  return (
    <ul>
      {ids.map((id) => (
        <li key={id}>
          <MentionedBadge count={counts.get(id)} />
        </li>
      ))}
    </ul>
  );
}

afterEach(() => clearMentionedCounts());

describe("MentionedBadge", () => {
  it("小签「3 场会提到」，悬停「在 3 场会上被提到」；0 时不画；有 onClick 才能点", async () => {
    const onClick = vi.fn();
    const { rerender } = render(<MentionedBadge count={3} onClick={onClick} />);
    const badge = screen.getByRole("button", { name: "3 场会提到" });
    expect(badge).toHaveAttribute("title", "在 3 场会上被提到");
    await userEvent.click(badge);
    expect(onClick).toHaveBeenCalled();
    rerender(<MentionedBadge count={3} />);
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
    expect(screen.getByText("3 场会提到")).toBeInTheDocument();
    rerender(<MentionedBadge count={0} />);
    expect(screen.queryByText(/场会提到/)).not.toBeInTheDocument();
  });

  it("一个列表一次请求；版本变了清缓存", async () => {
    const mentionedCounts = vi.fn(async (ids: number[]) => ({ counts: Object.fromEntries(ids.map((id) => [String(id), id % 5])) }));
    const { rerender } = render(<List api={{ mentionedCounts }} ids={[812, 813, 814]} />);
    await waitFor(() => expect(screen.getByText("2 场会提到")).toBeInTheDocument());
    expect(mentionedCounts).toHaveBeenCalledTimes(1);
    expect(mentionedCounts).toHaveBeenCalledWith([812, 813, 814]);
    rerender(<List api={{ mentionedCounts }} ids={[812, 813, 814]} />);
    expect(mentionedCounts).toHaveBeenCalledTimes(1);
    rerender(<List api={{ mentionedCounts }} ids={[812, 813, 814]} version="2" />);
    await waitFor(() => expect(mentionedCounts).toHaveBeenCalledTimes(2));
  });

  it("接口 404（旧后台）时不显示小签", async () => {
    const mentionedCounts = vi.fn(async () => {
      throw new ApiError("Not Found", 404, { detail: "Not Found" });
    });
    render(<List api={{ mentionedCounts }} ids={[812]} />);
    await waitFor(() => expect(screen.getByText("没有小签")).toBeInTheDocument());
  });
});
