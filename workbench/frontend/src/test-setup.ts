import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

afterEach(() => cleanup());

Object.defineProperty(window, "ResizeObserver", {
  configurable: true,
  value: class ResizeObserver {
    observe() {}
    unobserve() {}
    disconnect() {}
  },
});

// jsdom 没有 IntersectionObserver，framer-motion 的 whileInView 会让元素停在 initial 态。
// 这里让它立即上报「已进入视口」，测试才看得到最终内容。
Object.defineProperty(window, "IntersectionObserver", {
  configurable: true,
  value: class IntersectionObserver {
    constructor(private cb: IntersectionObserverCallback) {}
    observe(target: Element) {
      this.cb(
        [{ isIntersecting: true, target, intersectionRatio: 1 } as IntersectionObserverEntry],
        this as unknown as globalThis.IntersectionObserver,
      );
    }
    unobserve() {}
    disconnect() {}
    takeRecords() {
      return [];
    }
  },
});

// jsdom 没有 canvas 实现，抖动图表调 getContext 会刷一堆 "Not implemented" 噪音。
// 组件本身对 ctx 为空有防御，这里直接返回 null，让它安静地跳过绘制。
HTMLCanvasElement.prototype.getContext = (() => null) as unknown as typeof HTMLCanvasElement.prototype.getContext;
