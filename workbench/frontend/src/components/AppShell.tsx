import type { ReactNode } from "react";

export type AppView = "overview" | "library" | "jobs" | "projects";

interface AppShellProps {
  activeView: AppView;
  children: ReactNode;
  health: "healthy" | "degraded" | "failed" | "unknown";
  isMobile: boolean;
  navigationLocked?: boolean;
  onNavigate: (view: AppView) => void;
  searchSlot: ReactNode;
}

const navItems: Array<{ view: AppView; label: string; mark: string; desktopOnly?: boolean }> = [
  { view: "library", label: "资料库", mark: "▤" },
  { view: "overview", label: "最近", mark: "◫" },
  { view: "jobs", label: "任务", mark: "↻", desktopOnly: true },
  { view: "projects", label: "项目", mark: "⌁" },
];

export function AppShell({
  activeView,
  children,
  health,
  isMobile,
  navigationLocked = false,
  onNavigate,
  searchSlot,
}: AppShellProps) {
  return (
    <div className={`app-frame ${isMobile ? "is-mobile" : ""}`}>
      <aside className="rail" aria-label="主导航">
        <button className="brand" disabled={navigationLocked} onClick={() => onNavigate("library")} type="button">
          <span className="brand-signal" aria-hidden="true">
            <i />
            <i />
            <i />
            <i />
          </span>
          <span className="brand-copy">
            <strong>声档</strong>
            <small>MEETING ARCHIVE</small>
          </span>
        </button>

        <nav className="rail-nav">
          {navItems
            .filter((item) => !(isMobile && item.desktopOnly))
            .map((item) => (
              <button
                aria-current={activeView === item.view ? "page" : undefined}
                className="rail-link"
                disabled={navigationLocked}
                key={item.view}
                onClick={() => onNavigate(item.view)}
                type="button"
              >
                <span aria-hidden="true">{item.mark}</span>
                {item.label}
              </button>
            ))}
        </nav>

        <div className="rail-foot">
          {isMobile ? (
            <span className="read-only-chip">只读访问</span>
          ) : (
            <span className="local-chip">LOCAL · 8765</span>
          )}
          <p>音频不离开这台 Mac</p>
        </div>
      </aside>

      <div className="workspace">
        <header className="topbar">
          {searchSlot}
          <div className={`health-pill health-pill--${health}`} aria-label={`服务状态：${health}`}>
            <span />
            {health === "healthy" ? "服务正常" : health === "degraded" ? "部分降级" : health === "failed" ? "服务异常" : "连接中"}
          </div>
        </header>
        <main className="main-stage">{children}</main>
      </div>
    </div>
  );
}
