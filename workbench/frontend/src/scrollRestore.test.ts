import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { recordScrollNow, restoreScrollFromHistory } from "./scrollRestore";

// jsdom 不排版：scrollTop 记值，scrollHeight 由用例控制（模拟数据分批到、页面变高）
let scrollTop = 0;
let scrollHeight = 300;

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "requestAnimationFrame", "cancelAnimationFrame", "performance"] });
  scrollTop = 0;
  scrollHeight = 300;
  Object.defineProperty(document.documentElement, "scrollTop", {
    configurable: true,
    get: () => scrollTop,
    set: (value: number) => {
      scrollTop = Math.min(value, Math.max(0, scrollHeight - document.documentElement.clientHeight));
    },
  });
  Object.defineProperty(document.documentElement, "scrollHeight", { configurable: true, get: () => scrollHeight });
});

afterEach(() => {
  vi.useRealTimers();
  delete (document.documentElement as { scrollTop?: number }).scrollTop;
  delete (document.documentElement as { scrollHeight?: number }).scrollHeight;
  window.history.replaceState(null, "", "/");
});

const frames = (ms: number) => vi.advanceTimersByTime(ms);

describe("浏览器前进后退的整页滚动恢复", () => {
  it("跳走前记下的位置：回来时等页面长够高再滚过去，内容再变高、被挤开就再放回去，停稳以后不再管", () => {
    scrollHeight = 2000;
    scrollTop = 400;
    recordScrollNow();
    expect((window.history.state as { scroll?: number }).scroll).toBe(400);

    // 回到这一条：页面还在读取，只有 300 高
    scrollTop = 0;
    scrollHeight = 300;
    restoreScrollFromHistory();
    expect(window.history.scrollRestoration).toBe("manual");
    frames(200);
    expect(scrollTop).toBe(0);

    // 数据到了
    scrollHeight = 1400;
    frames(50);
    expect(scrollTop).toBe(400);
    // 上面又插进一块（浏览器的滚动锚定把位置挤到 469），跟着再放回去
    scrollHeight = 1470;
    scrollTop = 469;
    frames(50);
    expect(scrollTop).toBe(400);

    // 停稳以后用户自己滚，不再被拉回
    frames(1000);
    scrollTop = 120;
    scrollHeight = 1500;
    frames(100);
    expect(scrollTop).toBe(120);
    expect(window.history.scrollRestoration).toBe("auto");
  });

  it("用户自己动了滚轮就不再替他放回去", () => {
    window.history.replaceState({ scroll: 400 }, "");
    restoreScrollFromHistory();
    window.dispatchEvent(new WheelEvent("wheel"));
    scrollHeight = 2000;
    frames(200);
    expect(scrollTop).toBe(0);
  });

  it("这一条没记过位置（或记的是 0）：不接管，交给浏览器", () => {
    window.history.replaceState({ app: true }, "");
    restoreScrollFromHistory();
    expect(window.history.scrollRestoration).toBe("auto");
  });
});
