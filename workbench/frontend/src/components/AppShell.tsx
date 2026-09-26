import type { ReactNode } from "react";

import { setThemePreference, useTheme, type ThemePreference } from "../theme";

export type AppView =
  | "overview"
  | "library"
  | "requirements"
  | "requirementDetail"
  | "tasks"
  | "glossary"
  | "jobs"
  | "projects"
  | "projectDetail";

interface AppShellProps {
  activeView: AppView;
  children: ReactNode;
  health: "healthy" | "degraded" | "failed" | "unknown";
  isMobile: boolean;
  navigationLocked?: boolean;
  onNavigate: (view: AppView) => void;
  searchSlot: ReactNode;
  taskBadge?: number;
  glossaryBadge?: number;
}

/* 侧栏图标。原先用 ◫ ▤ ☰ ⌁ ↻ 这些字符，在深色底上会糊成一团，
 * 且不同字体渲染出的字面大小不一致，对不齐。换成同一套 15×15 线性 SVG。 */
const icons: Record<AppView, ReactNode> = {
  overview: (
    <svg viewBox="0 0 15 15" fill="none" stroke="currentColor" strokeWidth="1.5">
      <rect x="1" y="1" width="13" height="13" rx="2" />
      <path d="M5.6 1v13" />
    </svg>
  ),
  library: (
    <svg viewBox="0 0 15 15" fill="none" stroke="currentColor" strokeWidth="1.5">
      <path d="M1.6 3h11.8M1.6 6.2h11.8M1.6 9.4h11.8M1.6 12.6h7" />
    </svg>
  ),
  requirements: (
    <svg viewBox="0 0 15 15" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
      <path d="M3.2 13V2" />
      <path d="M3.2 2.6c1.3-.8 2.5-.8 3.8 0s2.5.8 3.8 0v5.6c-1.3.8-2.5.8-3.8 0s-2.5-.8-3.8 0" />
    </svg>
  ),
  requirementDetail: (
    <svg viewBox="0 0 15 15" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
      <path d="M3.2 13V2" />
      <path d="M3.2 2.6c1.3-.8 2.5-.8 3.8 0s2.5.8 3.8 0v5.6c-1.3.8-2.5.8-3.8 0s-2.5-.8-3.8 0" />
    </svg>
  ),
  tasks: (
    <svg viewBox="0 0 15 15" fill="none" stroke="currentColor" strokeWidth="1.5">
      <path d="M1.6 4h11.8M1.6 7.5h11.8M1.6 11h11.8" />
    </svg>
  ),
  projects: (
    <svg viewBox="0 0 15 15" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round">
      <path d="M1.5 10.5c3-6 6.5-6 9.5-3" />
      <path d="M9 4.6l2.4 2.6-2.9 1.9" />
    </svg>
  ),
  glossary: (
    <svg viewBox="0 0 15 15" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
      <path d="M4.2 2.2h8.4v9.6a1.6 1.6 0 0 1-1.6 1.6H4.2" />
      <path d="M2.4 12.2V2.2a1.2 1.2 0 0 1 1.2-1.2 1.2 1.2 0 0 1 1.2 1.2" />
      <path d="M6.4 5.4h4.6M6.4 7.9h4.6" />
    </svg>
  ),
  projectDetail: (
    <svg viewBox="0 0 15 15" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round">
      <path d="M1.5 10.5c3-6 6.5-6 9.5-3" />
      <path d="M9 4.6l2.4 2.6-2.9 1.9" />
    </svg>
  ),
  jobs: (
    <svg viewBox="0 0 15 15" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round">
      <path d="M13 7.5a5.5 5.5 0 1 1-1.9-4.1" />
      <path d="M13.2 1.4v3.3H10" />
    </svg>
  ),
};

const themeOptions: Array<{ value: ThemePreference; label: string; icon: ReactNode }> = [
  {
    value: "dark",
    label: "深色",
    icon: (
      <svg viewBox="0 0 15 15" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinejoin="round">
        <path d="M12.6 9.3A5.6 5.6 0 0 1 5.7 2.4a5.6 5.6 0 1 0 6.9 6.9Z" />
      </svg>
    ),
  },
  {
    value: "light",
    label: "浅色",
    icon: (
      <svg viewBox="0 0 15 15" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round">
        <circle cx="7.5" cy="7.5" r="2.7" />
        <path d="M7.5 1v1.4M7.5 12.6V14M1 7.5h1.4M12.6 7.5H14M2.9 2.9l1 1M11.1 11.1l1 1M2.9 12.1l1-1M11.1 3.9l1-1" />
      </svg>
    ),
  },
  {
    value: "system",
    label: "跟随系统",
    icon: (
      <svg viewBox="0 0 15 15" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinejoin="round">
        <rect x="1.4" y="2.2" width="12.2" height="8.4" rx="1.4" />
        <path d="M5.2 13.2h4.6M7.5 10.6v2.6" strokeLinecap="round" />
      </svg>
    ),
  },
];

/** 主题三选一：深色 / 浅色 / 跟随系统。偏好存在本机浏览器里，不进服务端。 */
function ThemeSwitch() {
  const { preference } = useTheme();
  return (
    <div className="theme-switch" role="radiogroup" aria-label="界面主题">
      {themeOptions.map((option) => (
        <button
          aria-checked={preference === option.value}
          aria-label={option.label}
          key={option.value}
          onClick={() => setThemePreference(option.value)}
          role="radio"
          title={option.label}
          type="button"
        >
          {option.icon}
        </button>
      ))}
    </div>
  );
}

const navItems: Array<{ view: AppView; label: string; desktopOnly?: boolean }> = [
  { view: "overview", label: "工作台" },
  { view: "library", label: "录音档案" },
  { view: "requirements", label: "需求池" },
  { view: "tasks", label: "任务池" },
  { view: "glossary", label: "词典" },
  { view: "projects", label: "项目管理" },
  { view: "jobs", label: "转写录音", desktopOnly: true },
];

/** 需求详情页没有自己的侧栏入口，跟需求池共用高亮（同 projectDetail 挂在 projects 下的思路，但这里要求显式高亮）。 */
function isNavItemActive(itemView: AppView, activeView: AppView): boolean {
  if (itemView === activeView) return true;
  return itemView === "requirements" && activeView === "requirementDetail";
}

export function AppShell({
  activeView,
  children,
  health,
  isMobile,
  navigationLocked = false,
  onNavigate,
  searchSlot,
  taskBadge = 0,
  glossaryBadge = 0,
}: AppShellProps) {
  return (
    <div className={`app-frame ${isMobile ? "is-mobile" : ""}`}>
      <aside className="rail" aria-label="主导航">
        <button className="brand" disabled={navigationLocked} onClick={() => onNavigate("overview")} type="button">
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
            .map((item) => {
              const badge =
                item.view === "tasks"
                  ? taskBadge
                  : item.view === "glossary"
                    ? glossaryBadge
                    : 0;
              return (
                <button
                  aria-current={isNavItemActive(item.view, activeView) ? "page" : undefined}
                  className={`rail-link${badge > 0 ? " rail-link--badge" : ""}`}
                  disabled={navigationLocked}
                  key={item.view}
                  onClick={() => onNavigate(item.view)}
                  type="button"
                >
                  <span className="rail-icon" aria-hidden="true">
                    {icons[item.view]}
                  </span>
                  {item.label}
                  {badge > 0 && (
                    <em className="rail-badge">{badge > 99 ? "99+" : badge}</em>
                  )}
                </button>
              );
            })}
        </nav>

        <div className="rail-foot">
          {isMobile ? (
            <span className="read-only-chip">只读访问</span>
          ) : (
            <span className="local-chip">LOCAL</span>
          )}
          <p>音频不离开这台 Mac</p>
        </div>
      </aside>

      <div className="workspace">
        <header className="topbar">
          {searchSlot}
          <ThemeSwitch />
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
