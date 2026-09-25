import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// theme.ts 持有模块级状态，每个用例重新加载一份，互不串味。
async function loadTheme() {
  vi.resetModules();
  return import("./theme");
}

function mockSystemTheme(light: boolean) {
  const listeners = new Set<() => void>();
  const media = {
    matches: light,
    addEventListener: (_: string, listener: () => void) => listeners.add(listener),
    removeEventListener: (_: string, listener: () => void) => listeners.delete(listener),
  };
  Object.defineProperty(window, "matchMedia", { configurable: true, value: vi.fn(() => media) });
  return {
    change(nextLight: boolean) {
      media.matches = nextLight;
      listeners.forEach((listener) => listener());
    },
  };
}

describe("theme preference", () => {
  beforeEach(() => {
    window.localStorage.clear();
    delete document.documentElement.dataset.theme;
  });

  afterEach(() => {
    delete (window as { matchMedia?: unknown }).matchMedia;
  });

  it("defaults to following the system and tracks later system changes", async () => {
    const system = mockSystemTheme(true);
    const { useTheme } = await loadTheme();
    function Probe() {
      const { preference, resolved } = useTheme();
      return <p>{`${preference}:${resolved}`}</p>;
    }
    render(<Probe />);

    expect(screen.getByText("system:light")).toBeInTheDocument();
    expect(document.documentElement.dataset.theme).toBe("light");

    act(() => system.change(false));
    expect(screen.getByText("system:dark")).toBeInTheDocument();
    expect(document.documentElement.dataset.theme).toBe("dark");
  });

  it("persists an explicit choice and ignores system changes while it holds", async () => {
    const system = mockSystemTheme(false);
    const { setThemePreference, THEME_STORAGE_KEY } = await loadTheme();

    setThemePreference("light");
    expect(window.localStorage.getItem(THEME_STORAGE_KEY)).toBe("light");
    expect(document.documentElement.dataset.theme).toBe("light");

    system.change(false);
    expect(document.documentElement.dataset.theme).toBe("light");
  });

  it("restores the stored choice on load", async () => {
    mockSystemTheme(true);
    window.localStorage.setItem("meeting-workbench:theme", "dark");
    const { useTheme } = await loadTheme();
    function Probe() {
      return <p>{useTheme().resolved}</p>;
    }
    render(<Probe />);
    expect(screen.getByText("dark")).toBeInTheDocument();
  });

  it("switches from the top bar radio group", async () => {
    mockSystemTheme(false);
    await loadTheme();
    const { AppShell } = await import("./components/AppShell");
    render(
      <AppShell activeView="library" health="healthy" isMobile={false} onNavigate={vi.fn()} searchSlot={null}>
        <div />
      </AppShell>,
    );

    const group = screen.getByRole("radiogroup", { name: "界面主题" });
    expect(screen.getByRole("radio", { name: "跟随系统" })).toHaveAttribute("aria-checked", "true");

    await userEvent.click(screen.getByRole("radio", { name: "浅色" }));
    expect(screen.getByRole("radio", { name: "浅色" })).toHaveAttribute("aria-checked", "true");
    expect(screen.getByRole("radio", { name: "跟随系统" })).toHaveAttribute("aria-checked", "false");
    expect(document.documentElement.dataset.theme).toBe("light");
    expect(group).toBeInTheDocument();
  });
});
