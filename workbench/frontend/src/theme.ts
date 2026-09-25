import { useSyncExternalStore } from "react";

/** 用户可选的主题偏好；"system" 跟随操作系统的浅色/深色设置。 */
export type ThemePreference = "dark" | "light" | "system";
export type ResolvedTheme = "dark" | "light";

// 与 public/theme-init.js 共用同一个键，首帧脚本和运行时读到的偏好必须一致。
export const THEME_STORAGE_KEY = "meeting-workbench:theme";
const LIGHT_QUERY = "(prefers-color-scheme: light)";
const THEME_COLORS: Record<ResolvedTheme, string> = { dark: "#121212", light: "#f4f4f2" };

function readStoredPreference(): ThemePreference {
  try {
    const stored = window.localStorage.getItem(THEME_STORAGE_KEY);
    if (stored === "dark" || stored === "light" || stored === "system") return stored;
  } catch {
    // 读不到存储（隐私模式等）就按跟随系统处理。
  }
  return "system";
}

function systemPrefersLight(): boolean {
  return typeof window.matchMedia === "function" && window.matchMedia(LIGHT_QUERY).matches;
}

function resolve(preference: ThemePreference): ResolvedTheme {
  if (preference === "system") return systemPrefersLight() ? "light" : "dark";
  return preference;
}

interface ThemeState {
  preference: ThemePreference;
  resolved: ResolvedTheme;
}

let state: ThemeState = { preference: "system", resolved: "dark" };
let initialized = false;
const listeners = new Set<() => void>();

function apply(resolved: ResolvedTheme) {
  const root = document.documentElement;
  root.dataset.theme = resolved;
  document.querySelector('meta[name="theme-color"]')?.setAttribute("content", THEME_COLORS[resolved]);
}

function commit(preference: ThemePreference) {
  const resolved = resolve(preference);
  if (preference === state.preference && resolved === state.resolved) return;
  state = { preference, resolved };
  apply(resolved);
  listeners.forEach((listener) => listener());
}

function ensureInitialized() {
  if (initialized || typeof window === "undefined") return;
  initialized = true;
  const preference = readStoredPreference();
  state = { preference, resolved: resolve(preference) };
  apply(state.resolved);
  if (typeof window.matchMedia === "function") {
    const media = window.matchMedia(LIGHT_QUERY);
    const onSystemChange = () => {
      if (state.preference === "system") commit("system");
    };
    media.addEventListener?.("change", onSystemChange);
  }
  // 另一个标签页改了偏好时同步过来。
  window.addEventListener("storage", (event) => {
    if (event.key === THEME_STORAGE_KEY) commit(readStoredPreference());
  });
}

export function setThemePreference(preference: ThemePreference) {
  ensureInitialized();
  try {
    window.localStorage.setItem(THEME_STORAGE_KEY, preference);
  } catch {
    // 存不下也照样切换，只是刷新后回到默认。
  }
  commit(preference);
}

function subscribe(listener: () => void) {
  ensureInitialized();
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

function getSnapshot(): ThemeState {
  ensureInitialized();
  return state;
}

/** 当前主题偏好与实际生效的深浅。canvas 类组件把 resolved 放进依赖，换主题时重绘。 */
export function useTheme(): ThemeState {
  return useSyncExternalStore(subscribe, getSnapshot, getSnapshot);
}
