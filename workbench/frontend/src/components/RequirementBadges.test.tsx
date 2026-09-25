import { act, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { PriorityBadge, RequirementStatusBadge } from "./RequirementBadges";
import { useToast } from "./Toast";

describe("PriorityBadge", () => {
  it("没挂需求的任务显示「—」", () => {
    render(<PriorityBadge priority={null} />);
    expect(screen.getByText("—")).toHaveClass("priority-badge--none");
  });

  it("按优先级套对应色阶", () => {
    render(<PriorityBadge priority="P0" />);
    expect(screen.getByText("P0")).toHaveClass("priority-badge--p0");
  });
});

describe("RequirementStatusBadge", () => {
  it("状态显示中文", () => {
    render(<RequirementStatusBadge status="shelved" />);
    expect(screen.getByText("已搁置")).toHaveClass("requirement-status--shelved");
  });
});

function ToastHarness() {
  const { toastNode, showToast } = useToast(1000);
  return (
    <>
      <button onClick={() => showToast("已复制路径")} type="button">copy</button>
      {toastNode}
    </>
  );
}

describe("useToast", () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it("提示出现后到时自动消失", () => {
    vi.useFakeTimers();
    render(<ToastHarness />);
    act(() => screen.getByRole("button", { name: "copy" }).click());
    expect(screen.getByRole("status")).toHaveTextContent("已复制路径");
    act(() => {
      vi.advanceTimersByTime(1000);
    });
    expect(screen.queryByRole("status")).toBeNull();
  });
});
