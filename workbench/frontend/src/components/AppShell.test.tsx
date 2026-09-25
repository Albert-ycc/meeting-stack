import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { AppShell } from "./AppShell";

describe("AppShell responsive permissions", () => {
  it("shows tasks but hides job control navigation on mobile", () => {
    render(
      <AppShell
        activeView="library"
        health="healthy"
        isMobile
        onNavigate={vi.fn()}
        searchSlot={<input aria-label="全局检索" />}
      >
        <div>资料库正文</div>
      </AppShell>,
    );

    // 任务代办对移动端开放；转写流水线仅桌面端
    expect(screen.getByRole("button", { name: /任务/ })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /撰写/ })).not.toBeInTheDocument();
    expect(screen.getByText("只读访问")).toBeInTheDocument();
  });

  it("shows task and transcription controls on desktop", () => {
    render(
      <AppShell
        activeView="library"
        health="healthy"
        isMobile={false}
        onNavigate={vi.fn()}
        searchSlot={<input aria-label="全局检索" />}
      >
        <div />
      </AppShell>,
    );

    expect(screen.getByRole("button", { name: /任务/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /转写/ })).toBeInTheDocument();
  });

  it("highlights 需求池 for both the pool and its detail page", () => {
    const { rerender } = render(
      <AppShell
        activeView="requirements"
        health="healthy"
        isMobile={false}
        onNavigate={vi.fn()}
        searchSlot={<input aria-label="全局检索" />}
      >
        <div />
      </AppShell>,
    );
    expect(screen.getByRole("button", { name: "需求池" })).toHaveAttribute("aria-current", "page");

    rerender(
      <AppShell
        activeView="requirementDetail"
        health="healthy"
        isMobile={false}
        onNavigate={vi.fn()}
        searchSlot={<input aria-label="全局检索" />}
      >
        <div />
      </AppShell>,
    );
    expect(screen.getByRole("button", { name: "需求池" })).toHaveAttribute("aria-current", "page");
  });

  it("shows a pending badge when there are unconfirmed tasks", () => {
    render(
      <AppShell
        activeView="library"
        health="healthy"
        isMobile={false}
        onNavigate={vi.fn()}
        searchSlot={<input aria-label="全局检索" />}
        taskBadge={3}
      >
        <div />
      </AppShell>,
    );

    expect(screen.getByText("3")).toBeInTheDocument();
  });
});
