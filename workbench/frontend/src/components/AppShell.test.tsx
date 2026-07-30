import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { AppShell } from "./AppShell";

describe("AppShell responsive permissions", () => {
  it("completely hides editing and job control navigation on mobile", () => {
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

    expect(screen.queryByRole("button", { name: /任务/ })).not.toBeInTheDocument();
    expect(screen.queryByText("编辑与控制仅限桌面端")).not.toBeInTheDocument();
    expect(screen.getByText("只读访问")).toBeInTheDocument();
  });

  it("shows task controls on desktop", () => {
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
  });
});
